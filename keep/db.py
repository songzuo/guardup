#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db.py —— PostgreSQL / MySQL 真副本（v2 方案自动化缺口 2）。

现状痛点（G:/v/_failover_live 自述）：
    - 只有 SQLite backup API 覆盖一致性快照；PG/MySQL 有状态档 services 为空，
      备站没有常驻轻量 PG/MySQL，切换要人工 --allow-stale 放行。
    - 换库失败没有统一的健康检查 + 回滚（SQLite 档 gate.py 有，PG/MySQL 档没有）。

本模块把 gate.py 那套「快照 → 校验 → 变化判断 → 换库 → 健康检查 → 回滚」
泛化到 PG/MySQL，并补两件 PG/MySQL 特有的事：

    1. 一致性逻辑转储：pg_dump --single-transaction / mysqldump
       --single-transaction，绝不拷裸数据文件（那样会拿到中间态）。
       命令用 cmd_runner 注入，本机无 DB 客户端时可用桩验证核心逻辑，
       真实部署注入真执行器。
    2. 复制延迟闸门：replica lag 达标才自动放行，把 --allow-stale 人工闸门
       变成「延迟达标自动放行」——这是「用户啥都不做」的关键一环。
    3. PITR 快照目录：时间点快照清单 + 保留策略 + 按恢复点选择。

所有核心逻辑（校验/闸门/回滚/快照管理）都是真实代码、可用真文件测试；
只有外部 DB 命令通过 cmd_runner / apply_fn 注入（真实部署接真命令）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional


@dataclass
class Snapshot:
    path: str
    kind: str                      # postgres | mysql | sqlite
    checksum: str = ""
    size: int = 0
    created_at: float = 0.0
    markers: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    size: int = 0
    markers: List[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


@dataclass
class GateResult:
    """复制延迟闸门结果。"""
    allowed: bool
    reason: str
    lag_seconds: float = 0.0
    max_lag: float = 0.0


@dataclass
class PromoteResult:
    action: str                    # promoted | rolled_back | rejected | unchanged
    detail: str = ""
    health_ok: bool = False


# 真实部署用的转储/恢复命令模板（cmd_runner 执行；{out}/{src}/{dsn} 占位）
DUMP_CMD = {
    "postgres": "pg_dump --single-transaction --no-owner --clean --if-exists -f \"{out}\" \"{dsn}\"",
    "mysql": "mysqldump --single-transaction --quick --master-data=2 \"{dsn}\" > \"{out}\"",
}
RESTORE_CMD = {
    "postgres": "psql \"{dsn}\" -f \"{src}\"",
    "mysql": "mysql \"{dsn}\" < \"{src}\"",
}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_markers(path: str) -> List[str]:
    """从转储文本里识别结构/数据/binlog 位点标记，供校验用。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(400_000)
    except OSError:
        return []
    upper = head.upper()
    markers = []
    if "CREATE TABLE" in upper:
        markers.append("schema")
    if "COPY " in upper or "INSERT INTO" in upper:
        markers.append("data")
    if "CHANGE MASTER TO" in upper or "MASTER_LOG_FILE" in upper:
        markers.append("binlog_pos")
    return markers


def capture(kind: str, out_path: str, dsn: str, *,
            cmd_runner: Optional[Callable[[str], int]] = None,
            now_fn: Callable[[], float] = time.time) -> Snapshot:
    """生成一致性逻辑转储。

    cmd_runner: 接收命令字符串、返回 0 成功的 callable。默认 os.system。
    真实部署传 pg_dump/mysqldump 执行器；本机测试传桩（写一份假转储并返回 0）。
    """
    if kind not in DUMP_CMD:
        raise ValueError(f"不支持的 db_type={kind!r}，可选 {list(DUMP_CMD)}")
    runner = cmd_runner or (lambda cmd: os.system(cmd))
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = DUMP_CMD[kind].format(out=out_path, dsn=dsn)
    rc = runner(cmd)
    if rc != 0:
        raise RuntimeError(f"{kind} 转储命令失败(rc={rc}): {cmd.split()[0]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"转储文件为空或不存在: {out_path}")
    return Snapshot(
        path=out_path, kind=kind, checksum=_sha256(out_path),
        size=os.path.getsize(out_path), created_at=now_fn(),
        markers=detect_markers(out_path),
    )


def verify_dump(path: str, required_markers=("schema",), min_size: int = 64) -> VerifyResult:
    """校验转储：非空 + 必备标记齐全。坏转储绝不覆盖现库。"""
    if not os.path.exists(path):
        return VerifyResult(False, "转储文件不存在")
    size = os.path.getsize(path)
    if size < min_size:
        return VerifyResult(False, f"转储过小({size}B)，疑似中间态", size)
    markers = detect_markers(path)
    missing = [m for m in required_markers if m not in markers]
    if missing:
        return VerifyResult(False, f"转储缺少标记 {missing}", size, markers)
    return VerifyResult(True, "ok", size, markers)


def lag_gate(replica_lag_seconds: float, max_lag: float = 30.0, *,
             force: bool = False) -> GateResult:
    """复制延迟闸门——替代 --allow-stale 人工闸门。

    lag <= max_lag 才自动放行切换；否则 hold（编排层下一轮重试）。
    force 仅供显式演练，生产默认关闭。
    """
    if force:
        return GateResult(True, "force override（演练）", replica_lag_seconds, max_lag)
    if replica_lag_seconds < 0:
        return GateResult(False, "延迟为负（时钟异常/无复制），拒绝切换",
                          replica_lag_seconds, max_lag)
    if replica_lag_seconds <= max_lag:
        return GateResult(True, f"延迟 {replica_lag_seconds:.1f}s ≤ {max_lag}s，自动放行",
                          replica_lag_seconds, max_lag)
    return GateResult(False,
                      f"延迟 {replica_lag_seconds:.1f}s > {max_lag}s，暂缓切换等追平",
                      replica_lag_seconds, max_lag)


def save_manifest(snapshot: Snapshot, snapshot_dir: str) -> str:
    """把快照元信息写成 manifest，供 PITR 目录管理。"""
    os.makedirs(snapshot_dir, exist_ok=True)
    mpath = os.path.join(snapshot_dir,
                         f"pitr-{int(snapshot.created_at)}-{os.path.basename(snapshot.path)}.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(snapshot.to_dict(), fh, ensure_ascii=False, indent=2)
    return mpath


def list_pitr(snapshot_dir: str) -> List[Snapshot]:
    """列出 PITR 目录里的全部快照，按创建时间倒序。"""
    if not os.path.isdir(snapshot_dir):
        return []
    out = []
    for name in os.listdir(snapshot_dir):
        if not (name.startswith("pitr-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(snapshot_dir, name), "r", encoding="utf-8") as fh:
                out.append(Snapshot(**json.load(fh)))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda s: s.created_at, reverse=True)


def prune_pitr(snapshot_dir: str, keep: int = 5) -> int:
    """只保留最近 keep 份 PITR 快照，返回删除数。"""
    snaps = list_pitr(snapshot_dir)
    removed = 0
    for s in snaps[keep:]:
        mpath = os.path.join(snapshot_dir,
                             f"pitr-{int(s.created_at)}-{os.path.basename(s.path)}.json")
        if os.path.exists(mpath):
            os.remove(mpath)
        # 转储本体只删本模块生成的 .dump，绝不误删业务文件
        if s.path.endswith(".dump") and os.path.exists(s.path):
            os.remove(s.path)
        removed += 1
    return removed


def pick_for_point(snapshot_dir: str, target_ts: float) -> Optional[Snapshot]:
    """PITR：选 <= target_ts 的最近一份快照。"""
    candidates = [s for s in list_pitr(snapshot_dir) if s.created_at <= target_ts]
    return candidates[0] if candidates else None


def promote_with_rollback(
    new_snapshot: str,
    apply_fn: Callable[[str], bool],
    healthcheck: Callable[[], bool],
    *,
    verify_markers=("schema",),
    rollback_fn: Optional[Callable[[], bool]] = None,
    health_timeout: float = 60.0,
    health_interval: float = 1.0,
    dry_run: bool = False,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> PromoteResult:
    """把校验通过的转储应用到备库 → 健康检查 → 失败回滚。

    apply_fn(src_path) -> bool: 真实部署接 psql/mysql 恢复命令；测试用桩。
    rollback_fn() -> bool: 恢复切换前状态；None 时跳过（编排层可传 PITR 回滚）。
    """
    vr = verify_dump(new_snapshot, verify_markers)
    if not vr:
        return PromoteResult("rejected", f"转储校验失败，保留现库: {vr.reason}")
    if dry_run:
        return PromoteResult("unchanged", f"[dry-run] 转储校验通过({vr.markers})，未应用")
    ok = apply_fn(new_snapshot)
    if not ok:
        return PromoteResult("rejected", "应用转储失败，保留现库")
    deadline = now_fn() + health_timeout
    healthy = False
    while now_fn() < deadline:
        try:
            if healthcheck():
                healthy = True
                break
        except Exception:
            pass
        sleep_fn(health_interval)
    if healthy:
        return PromoteResult("promoted", f"已应用转储并通过健康检查({vr.markers})", True)
    if rollback_fn is not None:
        try:
            rollback_fn()
        except Exception as e:
            return PromoteResult("rolled_back", f"健康检查失败，回滚也异常: {e}")
        return PromoteResult("rolled_back", "健康检查失败，已回滚到切换前状态")
    return PromoteResult("rolled_back", "健康检查失败（未提供回滚函数，需人工介入）")


DEFAULT_MYSQL_MARKERS = ("schema", "binlog_pos")
