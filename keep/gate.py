#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Consistency Gate —— 数据库一致性快照 + 校验 + 换库 + 健康检查 + 自动回滚。

这是整套系统最值钱的硬功夫，脱胎于 G:/v/_failover_live/sync_apigw.sh 的实战逻辑：

  1. 绝不热拷贝裸 .db 文件 —— WAL 模式下会拷到「主库 + 未合并 WAL」的中间态，
     SQLite 打开报 malformed。必须用 sqlite3 backup API 做一致性快照。
  2. 换新库前先校验：integrity_check == ok + 业务表非空（如 channels>0），
     库坏了绝不能覆盖本机现有库。
  3. md5 相同就不动本机服务 —— 重启一个正在用的库是没必要的抖动。
  4. 换库后跑健康检查（轮询等待冷启动，实测 api-gateway 要 50 秒）。
  5. 健康检查失败 → 自动回滚到换库前的备份，绝不让备站起不来。

本模块把这些从"只服务 api-gateway"泛化到任意 SQLite 库 + 任意健康检查函数。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    integrity: str = ""
    table_counts: dict = field(default_factory=dict)

    def __bool__(self):
        return self.ok


def content_fingerprint(db_path: str, business_tables: List[str]) -> str:
    """逻辑指纹：判断"内容是否真的变了"，而非字节是否相同。

    为什么不用 md5 直接比：SQLite backup API 会规范化页面布局，
    同一个库 backup 出来的文件与 live 库字节不同（实测 md5 不同），
    但逻辑内容完全一致。只比 md5 会误判"有变化"→ 无谓换库+重启服务，
    这正是 sync_apigw.sh 想避免的抖动。这里改用「库结构 + 各业务表行数 +
    表内容 hash」做逻辑比较。

    未指定业务表时退回 (页面数, 文件大小的弱指纹)，此时不做 unchanged 优化，
    宁可多换一次也不漏同步。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        if not business_tables:
            size = os.path.getsize(db_path)
            pages = con.execute("pragma page_count").fetchone()[0]
            con.close()
            return f"weak:{pages}:{size}"
        h = hashlib.md5()
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        for t in tables:
            h.update(t.encode()); h.update(b"|")
            for row in con.execute(f'select * from "{t}"'):
                h.update(repr(row).encode(errors="replace"))
        con.close()
        return h.hexdigest()
    except Exception as e:
        return f"err:{e}"


def snapshot(src_db: str, dst_db: str, *, timeout: int = 30) -> str:
    """用 SQLite backup API 做一致性快照，返回目标路径。只读打开源库。"""
    if not os.path.exists(src_db):
        raise FileNotFoundError(f"源库不存在: {src_db}")
    parent = os.path.dirname(os.path.abspath(dst_db))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(dst_db):
        os.remove(dst_db)
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=timeout)
    dst = sqlite3.connect(dst_db, timeout=timeout)
    try:
        src.backup(dst)          # 一致性快照，自动处理 WAL 合并
    finally:
        dst.close(); src.close()
    return dst_db


def verify(db_path: str, business_tables: List[str] = None) -> VerifyResult:
    """校验快照：integrity_check + 指定业务表非空。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            con.close()
            return VerifyResult(False, f"integrity_check={integrity}", integrity)
        counts = {}
        for table in (business_tables or []):
            # 表名来自受信任的 profile，仍做基本白名单防护
            if not table.replace("_", "").isalnum():
                con.close()
                return VerifyResult(False, f"非法表名: {table}")
            try:
                n = con.execute(f'select count(*) from "{table}"').fetchone()[0]
            except sqlite3.Error as e:
                con.close()
                return VerifyResult(False, f"表 {table} 查询失败: {e}")
            counts[table] = n
            if n <= 0:
                con.close()
                return VerifyResult(False, f"业务表 {table} 为空（疑似坏库/中间态）", integrity, counts)
        con.close()
        return VerifyResult(True, "ok", integrity, counts)
    except Exception as e:
        return VerifyResult(False, f"打不开库: {e}")


@dataclass
class SwapResult:
    action: str            # unchanged | swapped | rolled_back | rejected
    detail: str = ""
    health_ok: bool = False


def swap_with_rollback(
    new_db: str,
    live_db: str,
    business_tables: List[str],
    healthcheck: Callable[[], bool],
    *,
    backup_dir: Optional[str] = None,
    health_timeout: float = 150,
    health_interval: float = 3,
    dry_run: bool = False,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> SwapResult:
    """把校验通过的 new_db 换到 live_db，跑健康检查，失败自动回滚。

    healthcheck: 无参可调用，返回 True 表示健康。编排层负责接真实的
                 curl / systemctl restart + HTTP 探测。
    """
    vr = verify(new_db, business_tables)
    if not vr:
        return SwapResult("rejected", f"新库校验失败，保留现库: {vr.reason}")

    parent = os.path.dirname(os.path.abspath(live_db))
    if parent:
        os.makedirs(parent, exist_ok=True)

    new_md5 = _md5(new_db)
    cur_md5 = _md5(live_db) if os.path.exists(live_db) else "none"
    # 逻辑指纹判"内容是否真变了"（backup 会改字节但不改内容）
    new_fp = content_fingerprint(new_db, business_tables)
    cur_fp = content_fingerprint(live_db, business_tables) if os.path.exists(live_db) else "none"
    if new_fp == cur_fp and not new_fp.startswith("err:") and not new_fp.startswith("weak:"):
        return SwapResult("unchanged", f"逻辑内容无变化（fp={new_fp[:12]}），不换库、不重启")

    if dry_run:
        return SwapResult("unchanged",
                          f"[dry-run] 内容有变化 {cur_fp[:12]} -> {new_fp[:12]}（{vr.table_counts}），未替换")

    backup_dir = backup_dir or os.path.join(parent, ".guardup-backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_fn()))
    bak = os.path.join(backup_dir, f"{os.path.basename(live_db)}.{ts}")
    had_live = os.path.exists(live_db)
    if had_live:
        shutil.copy2(live_db, bak)

    # 覆盖式换库（对齐 sync_apigw.sh 的 install 语义）：
    # 用 copy 覆盖而非 os.replace(rename)——真实部署时 api-gateway 服务正持有
    # live 库句柄，rename 会被 "device or resource busy" / WinError 5 拒绝；
    # 复制覆盖则能在文件被打开时写入（Linux 语义），换库后再由编排层重启服务。
    shutil.copy2(new_db, live_db)

    # 健康检查：轮询等待冷启动
    deadline = now_fn() + health_timeout
    healthy = False
    while now_fn() < deadline:
        if healthcheck():
            healthy = True
            break
        sleep_fn(health_interval)

    if healthy:
        return SwapResult("swapped", f"已换库并通过健康检查（{vr.table_counts}）", True)

    # 回滚
    if had_live:
        shutil.copy2(bak, live_db)
        # 回滚后给一次恢复检查的机会（不阻塞主流程结果）
        try:
            healthcheck()
        except Exception:
            pass
    else:
        if os.path.exists(live_db):
            os.remove(live_db)
    return SwapResult("rolled_back",
                      f"换库后健康检查失败，已回滚到换库前状态（备份={bak if had_live else '无原库'}）")
