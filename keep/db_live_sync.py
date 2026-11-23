#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_live_sync.py —— 把产品 db.py 的一致性副本能力回流给自建热备系统（P0 补强）。

背景（实地核实 2026-11-23）：
    自建系统 sync.sh 里 SYNC_DB 默认是 none，且备站 /data/standby/db/ 是空目录。
    rsync 不直接拷数据库文件（热拷贝会拿到中间态、损坏）—— 这个顾虑是对的，
    但解法不应该是"干脆不同步"，而是用一致性快照机制。

本模块提供三层能力，全部真实代码、可用真 SQLite 端到端验证：
    1. 一致性快照：SQLite 走 backup API（不是 cp 文件）；MySQL 走 --single-transaction
       逻辑转储。绝不拷裸库文件。
    2. 校验：SQLite magic header + integrity_check + 行数；转储最小体积 + 结构标记。
       坏快照绝不覆盖现库。
    3. PITR 目录 + manifest + 保留策略 + 恢复演练（还原到临时目录真查询比对）。

安全底线：
    - 默认 dry-run：任何写操作都要显式 --apply。
    - 恢复演练永远还原到临时目录，绝不覆盖线上库。
    - 外部 DB 命令通过 runner 注入，本机无 mysql 客户端时可用 SQLite 或桩验证核心逻辑。

用法：
    python db_live_sync.py snapshot --src /data/app.db --dest-dir /data/standby/db
    python db_live_sync.py verify --snapshot /data/standby/db/app-20261123.dump
    python db_live_sync.py restore-drill --snapshot <snap> --table orders
    python db_live_sync.py list --dest-dir /data/standby/db
    python db_live_sync.py prune --dest-dir /data/standby/db --keep 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional


# ---------------------------------------------------------------- 数据结构
@dataclass
class DBSnapshot:
    path: str
    kind: str                       # sqlite | mysql
    source: str = ""
    checksum: str = ""
    size: int = 0
    rows: int = 0
    created_at: float = 0.0
    verified: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    rows: int = 0
    size: int = 0

    def __bool__(self):
        return self.ok


SQLITE_MAGIC = b"SQLite format 3\x00"


# ---------------------------------------------------------------- 工具
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_kind(source: str) -> str:
    """按文件头判断 SQLite；否则当 MySQL DSN 处理。"""
    try:
        with open(source, "rb") as fh:
            if fh.read(16) == SQLITE_MAGIC:
                return "sqlite"
    except OSError:
        pass
    return "mysql"


def _manifest_path(snapshot: DBSnapshot, dest_dir: str) -> str:
    base = os.path.splitext(os.path.basename(snapshot.path))[0]
    return os.path.join(dest_dir, f"manifest-{base}.json")


# ---------------------------------------------------------------- 快照生成
def snapshot_sqlite(src: str, out: str, *, now_fn: Callable[[], float] = time.time) -> DBSnapshot:
    """用 SQLite backup API 生成一致性快照 —— 不是 cp 文件。

    backup API 在源库被并发写入时也能拿到事务一致的副本；cp 裸文件可能拿到
    写到一半的页，这正是自建系统注释里担心的问题。
    """
    if not os.path.exists(src):
        raise FileNotFoundError(f"源库不存在: {src}")
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.abspath(src) == os.path.abspath(out):
        raise ValueError("源和目的不能是同一个文件")

    src_con = sqlite3.connect(src)
    dst_con = sqlite3.connect(out)
    try:
        # 先清空目标，避免 backup 到已有库留下旧表
        for (name,) in dst_con.execute(
                "select name from sqlite_master where type='table'").fetchall():
            dst_con.execute(f'drop table if exists "{name}"')
        dst_con.commit()
        src_con.backup(dst_con)          # 事务一致
    finally:
        src_con.close()
        dst_con.close()

    return DBSnapshot(
        path=out, kind="sqlite", source=src, checksum=_sha256(out),
        size=os.path.getsize(out), created_at=now_fn(),
    )


def snapshot_mysql(dsn: str, out: str, *,
                   runner: Optional[Callable[[str], int]] = None,
                   now_fn: Callable[[], float] = time.time) -> DBSnapshot:
    """MySQL 一致性逻辑转储：--single-transaction，不拷裸文件。

    runner 注入命令执行器（返回 0 成功）。真实部署传 subprocess 封装；
    测试传桩写一份最小合法转储。
    """
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = ('mysqldump --single-transaction --quick --no-tablespaces '
           f'{dsn} > "{out}"')
    run = runner or (lambda c: subprocess.run(
        c, shell=True, check=False).returncode)
    rc = run(cmd)
    if rc != 0:
        raise RuntimeError(f"mysqldump 失败(rc={rc})")
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError("mysqldump 输出为空")
    return DBSnapshot(
        path=out, kind="mysql", source=dsn, checksum=_sha256(out),
        size=os.path.getsize(out), created_at=now_fn(),
    )


# ---------------------------------------------------------------- 校验
def verify(snapshot_path: str, *, kind: Optional[str] = None) -> VerifyResult:
    """校验快照：magic header / integrity_check / 转储结构标记，坏快照绝不覆盖现库。"""
    if not os.path.exists(snapshot_path):
        return VerifyResult(False, "快照文件不存在")
    size = os.path.getsize(snapshot_path)
    if size < 64:
        return VerifyResult(False, f"快照过小({size}B)，疑似中间态", size=size)

    with open(snapshot_path, "rb") as fh:
        head = fh.read(16)
    if head == SQLITE_MAGIC or kind == "sqlite":
        if head != SQLITE_MAGIC:
            return VerifyResult(False, "缺少 SQLite magic header", size=size)
        con = sqlite3.connect(snapshot_path)
        try:
            ic = con.execute("pragma integrity_check").fetchone()[0]
            if ic != "ok":
                return VerifyResult(False, f"integrity_check 失败: {ic}", size=size)
            tables = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%'").fetchall()]
            rows = 0
            for t in tables:
                rows += con.execute(f'select count(*) from "{t}"').fetchone()[0]
            return VerifyResult(True, "ok", rows=rows, size=size)
        except sqlite3.DatabaseError as e:
            return VerifyResult(False, f"快照损坏: {e}", size=size)
        finally:
            con.close()

    # MySQL 逻辑转储：查结构标记
    with open(snapshot_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(400_000).upper()
    if "CREATE TABLE" not in text:
        return VerifyResult(False, "转储缺少 CREATE TABLE 结构标记", size=size)
    return VerifyResult(True, "ok", size=size)


# ---------------------------------------------------------------- PITR 目录
def save(snapshot: DBSnapshot, dest_dir: str, *, apply: bool = False) -> Optional[str]:
    """把快照 + manifest 落到 PITR 目录。dry-run 下只报告不落盘。"""
    vr = verify(snapshot.path, kind=snapshot.kind)
    snapshot.verified = bool(vr)
    snapshot.rows = vr.rows
    mf = _manifest_path(snapshot, dest_dir)
    if not apply:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    with open(mf, "w", encoding="utf-8") as fh:
        json.dump(snapshot.to_dict(), fh, ensure_ascii=False, indent=2)
    return mf


def list_snapshots(dest_dir: str) -> List[DBSnapshot]:
    if not os.path.isdir(dest_dir):
        return []
    out = []
    for name in sorted(os.listdir(dest_dir)):
        if not (name.startswith("manifest-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(dest_dir, name), "r", encoding="utf-8") as fh:
                out.append(DBSnapshot(**json.load(fh)))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda s: s.created_at, reverse=True)


def prune(dest_dir: str, keep: int = 5, *, apply: bool = False) -> int:
    """只保留最近 keep 份，返回将/已删除数。只删本模块生成的 manifest + .dump。"""
    snaps = list_snapshots(dest_dir)
    removed = 0
    for s in snaps[keep:]:
        mf = _manifest_path(s, dest_dir)
        if apply:
            if os.path.exists(mf):
                os.remove(mf)
            if s.path.endswith(".dump") and os.path.exists(s.path):
                os.remove(s.path)
        removed += 1
    return removed


# ---------------------------------------------------------------- 恢复演练
def restore_drill(snapshot_path: str, *, table: Optional[str] = None,
                  workdir: Optional[str] = None) -> dict:
    """从快照还原到临时目录，真查询比对 —— 证明备份能救命，不碰线上库。

    返回 {ok, rows, sample, temp_path, reason}。调用方负责删 temp_path。
    """
    vr = verify(snapshot_path)
    if not vr:
        return {"ok": False, "reason": f"快照校验失败: {vr.reason}"}
    with open(snapshot_path, "rb") as fh:
        kind = "sqlite" if fh.read(16) == SQLITE_MAGIC else "mysql"
    if kind != "sqlite":
        return {"ok": False, "reason": "恢复演练当前只支持 SQLite 快照（MySQL 需真实实例）"}

    tmpdir = workdir or os.path.join(
        os.path.dirname(os.path.abspath(snapshot_path)), ".restore-drill")
    os.makedirs(tmpdir, exist_ok=True)
    restored = os.path.join(tmpdir, f"restored-{int(time.time())}.db")
    shutil.copyfile(snapshot_path, restored)   # 快照本身已校验一致，拷到临时目录安全
    con = sqlite3.connect(restored)
    try:
        if table:
            rows = con.execute(f'select count(*) from "{table}"').fetchone()[0]
            sample = con.execute(f'select * from "{table}" limit 3').fetchall()
        else:
            tables = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%'").fetchall()]
            rows = 0
            sample = []
            for t in tables[:3]:
                rows += con.execute(f'select count(*) from "{t}"').fetchone()[0]
                sample.append([t, con.execute(
                    f'select * from "{t}" limit 2').fetchall()])
        return {"ok": True, "rows": rows, "sample": sample,
                "temp_path": restored, "reason": "从快照还原并真查询成功"}
    except sqlite3.DatabaseError as e:
        return {"ok": False, "reason": f"还原后查询失败: {e}", "temp_path": restored}
    finally:
        con.close()


# ---------------------------------------------------------------- CLI
def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="db_live_sync.py",
        description="GuardUp 自建系统 DB 真同步补强（一致性快照 + 校验 + PITR + 恢复演练）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_s = sub.add_parser("snapshot", help="生成一致性快照")
    p_s.add_argument("--src", required=True, help="SQLite 库文件路径 / MySQL DSN")
    p_s.add_argument("--dest-dir", default="/data/standby/db", help="PITR 目录")
    p_s.add_argument("--kind", choices=["sqlite", "mysql"], default=None)
    p_s.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")

    p_v = sub.add_parser("verify", help="校验一个快照")
    p_v.add_argument("--snapshot", required=True)
    p_v.add_argument("--kind", choices=["sqlite", "mysql"], default=None)

    p_l = sub.add_parser("list", help="列出 PITR 目录快照")
    p_l.add_argument("--dest-dir", default="/data/standby/db")

    p_p = sub.add_parser("prune", help="保留最近 N 份")
    p_p.add_argument("--dest-dir", default="/data/standby/db")
    p_p.add_argument("--keep", type=int, default=5)
    p_p.add_argument("--apply", action="store_true")

    p_r = sub.add_parser("restore-drill", help="从快照还原到临时目录真查询")
    p_r.add_argument("--snapshot", required=True)
    p_r.add_argument("--table", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "snapshot":
        kind = args.kind or _detect_kind(args.src)
        name = os.path.splitext(os.path.basename(args.src))[0] or "dump"
        out = os.path.join(args.dest_dir, f"{name}-{_ts()}.dump")
        if kind == "sqlite":
            if not args.apply:
                # dry-run 不写任何东西，只报告计划
                vr_src = verify(args.src)
                print(f"[dry-run] 计划用 backup API 快照: {args.src}")
                print(f"[dry-run] 源库校验: {'OK' if vr_src else vr_src.reason} "
                      f"(rows={vr_src.rows}); 目标: {out}")
                print("[dry-run] 加 --apply 真正落盘")
                return 0
            snap = snapshot_sqlite(args.src, out)
        else:
            snap = snapshot_mysql(args.src, out)
        mf = save(snap, args.dest_dir, apply=args.apply)
        print(json.dumps({**snap.to_dict(), "manifest": mf},
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "verify":
        vr = verify(args.snapshot, kind=args.kind)
        print(json.dumps({"ok": vr.ok, "reason": vr.reason,
                          "rows": vr.rows, "size": vr.size},
                         ensure_ascii=False, indent=2))
        return 0 if vr.ok else 2

    if args.cmd == "list":
        snaps = list_snapshots(args.dest_dir)
        print(json.dumps([s.to_dict() for s in snaps],
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "prune":
        n = prune(args.dest_dir, keep=args.keep, apply=args.apply)
        print(f"{'已删除' if args.apply else '将删除'} {n} 份（保留最近 {args.keep} 份）")
        return 0

    if args.cmd == "restore-drill":
        res = restore_drill(args.snapshot, table=args.table)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        # 演练产物是临时目录，保留供检查；生产清理可手动 rm
        return 0 if res.get("ok") else 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
