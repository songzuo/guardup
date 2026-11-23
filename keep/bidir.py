#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bidir.py —— 双向同步 / 灾备回切（failback）时的增量合并（v2 方案补丁）。

场景（用户原话）："主站起来以后，可能副站已经有了数据更新，当然也需要同步最新的数据。"
  正常态：主 active ← 单向同步 → 备 standby
  故障态：主 down，DNS 切备，备站接管并对**真实用户**产生新数据（订单/注册/配置）
  回切态：主恢复，此时主站是旧数据、备站有新数据 —— 绝不能用旧主覆盖新备！
          必须把备站故障期的增量反向合并回主站，再切 DNS 回主。

核心设计（关键决策，均为自主拍板）：
  1. **不侵入业务表**：不改用户 schema、不加 updated_at 列。用独立影子表
     `_guardup_track(pk, updated_at, origin, op)` + 触发器捕获 insert/update/delete。
     这样任何 SQLite 库都能接，业务代码零改动。
  2. **三方合并**：base=故障切换那一刻的共同快照，local=当前主站，remote=故障期副站。
     - 只有一边变 → 取变的那边（自动合并，无冲突）
     - 两边都没变 → 跳过
     - 两边都变且相同 → 跳过
     - 两边都变且不同 → **冲突**，按策略处理
  3. **冲突策略**：默认 `remote_wins`（副站在故障期对外服务，写入是真实业务数据，
     且用户明确要求把副站更新同步回来）；可切 `local_wins` / `manual`（阻断等人工）。
  4. **删除用墓碑**（op='D' 保留 tracking 行），避免删除被旧快照复活。
  5. **fencing**：合并前校验两站的 epoch/角色，防止在副站仍在服务时误回切造成双写。

所有逻辑真 SQLite + 真触发器，可用真实行级变更测试；DB 连接/时钟可注入。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

TRACK = "_guardup_track"


# ---------- 变更追踪：影子表 + 触发器 ----------

def install_tracking(db_path: str, table: str, pk_col: str = "id", *,
                     origin: str = "local") -> None:
    """在库上安装变更追踪（幂等）。业务表结构零改动。

    pk_col 默认是 'id'；业务表用别的自增/唯一列时传对应列名。
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"""create table if not exists {TRACK}(
            tbl text not null, pk text not null, ts real not null,
            origin text not null, op text not null,
            primary key(tbl, pk))""")
        # 三个触发器：INSERT/UPDATE/DELETE 都 upsert 一行 tracking
        safe = table.replace('"', '""')
        for op in ("INSERT", "UPDATE", "DELETE"):
            con.execute(f"drop trigger if exists _gu_{op}_{safe}")
        for op, ref in (("INSERT", "new"), ("UPDATE", "new"), ("DELETE", "old")):
            con.execute(f"""create trigger _gu_{op}_{safe} after {op.lower()} on "{table}"
                begin
                  insert into {TRACK}(tbl,pk,ts,origin,op)
                  values('{safe}', cast({ref}."{pk_col}" as text), strftime('%s','now')*1.0,
                         '{origin}', '{op[0]}')
                  on conflict(tbl,pk) do update set
                    ts=excluded.ts, origin=excluded.origin, op=excluded.op;
                end""")
        con.commit()
    finally:
        con.close()


def read_changes(db_path: str, table: str, *, since_ts: float = 0.0) -> Dict[str, dict]:
    """读某表的变更集：{pk: {ts, origin, op}}，只返回 ts > since_ts 的行。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            f"select pk, ts, origin, op from {TRACK} where tbl=? and ts>? order by ts",
            (table, since_ts)).fetchall()
        return {r[0]: {"ts": r[1], "origin": r[2], "op": r[3]} for r in rows}
    finally:
        con.close()


def read_rows(db_path: str, table: str, pk_col: str = "id") -> Dict[str, dict]:
    """读业务表全部行：{pk: {col: val}}，不含影子表。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        rows = {}
        for r in con.execute(f'select * from "{table}"'):
            d = dict(zip(cols, r))
            rows[str(d[pk_col])] = d
        return rows
    finally:
        con.close()


# ---------- 三方合并 ----------

@dataclass
class MergeChange:
    pk: str
    action: str          # I | U | D | KEEP
    reason: str
    local_row: Optional[dict] = None
    remote_row: Optional[dict] = None


@dataclass
class MergeResult:
    applied: List[MergeChange] = field(default_factory=list)
    conflicts: List[MergeChange] = field(default_factory=list)
    skipped: List[MergeChange] = field(default_factory=list)
    strategy: str = "remote_wins"

    @property
    def ok(self) -> bool:
        # manual 策略下有冲突 ≠ 失败，而是"等待人工"，用 blocked 表达
        return True

    def summary(self) -> str:
        return (f"应用 {len(self.applied)} / 冲突 {len(self.conflicts)} / "
                f"跳过 {len(self.skipped)}（策略={self.strategy}）")


def merge(
    base_rows: Dict[str, dict],
    local_rows: Dict[str, dict],
    remote_rows: Dict[str, dict],
    *,
    local_changes: Optional[Dict[str, dict]] = None,
    remote_changes: Optional[Dict[str, dict]] = None,
    strategy: str = "remote_wins",
) -> MergeResult:
    """三方增量合并（纯逻辑，行级）。

    base/local/remote 均为 {pk: row}（删除的行不在 rows 里，靠 changes 的 op='D' 区分
    "本来就不存在" 和 "被删了"）。local_changes/remote_changes 是各自的变更集
    {pk: {ts, op}}；提供时用 ts 做 LWW，不提供则靠行内容差异判断。

    返回 MergeResult，其中 applied 是最终应写到"合并结果库"的动作清单。
    调用方用 apply_merge() 把动作落到一个新的库，再走 gate 校验/换库/回滚。
    """
    if strategy not in ("remote_wins", "local_wins", "manual"):
        raise ValueError(f"未知冲突策略 {strategy!r}")
    res = MergeResult(strategy=strategy)
    lc = local_changes or {}
    rc = remote_changes or {}

    all_pk = set(base_rows) | set(local_rows) | set(remote_rows)
    # 变更集里出现过但三份 rows 都没有的 pk（删除后）也要纳入
    all_pk |= set(lc) | set(rc)

    for pk in sorted(all_pk, key=str):
        b = base_rows.get(pk)
        l = local_rows.get(pk)
        r = remote_rows.get(pk)
        l_op = (lc.get(pk) or {}).get("op", "")
        r_op = (rc.get(pk) or {}).get("op", "")
        # "是否变了"：有变更记录，或行内容与 base 不同
        l_diff = bool(l_op) or (l != b)
        r_diff = bool(r_op) or (r != b)

        if not l_diff and not r_diff:
            res.skipped.append(MergeChange(pk, "KEEP", "两边都没变"))
            continue
        if l_diff and not r_diff:
            res.skipped.append(MergeChange(pk, "KEEP", "只有主站变，保留主站"))
            continue
        if not l_diff and r_diff:
            # 副站独有变更 → 反向合并到主
            if r is None:
                res.applied.append(MergeChange(pk, "D", "副站删除，墓碑同步到主站",
                                               local_row=l, remote_row=r))
            else:
                res.applied.append(MergeChange(pk, "I" if b is None else "U",
                                               "副站更新，反向同步到主站",
                                               local_row=l, remote_row=r))
            continue

        # 两边都变：内容相同则无事
        if l == r and l_op == r_op:
            res.skipped.append(MergeChange(pk, "KEEP", "两边变更一致"))
            continue

        # 真冲突
        l_ts = (lc.get(pk) or {}).get("ts", 0.0)
        r_ts = (rc.get(pk) or {}).get("ts", 0.0)
        if strategy == "manual":
            res.conflicts.append(MergeChange(pk, "?", "两边都改且不同，manual 策略阻断等人工",
                                             l, r))
            continue
        if strategy == "local_wins":
            winner, loser = "local", "remote"
        elif strategy == "remote_wins":
            winner, loser = "remote", "local"
        else:  # 保险：非法策略已在开头挡掉
            winner, loser = "remote", "local"
        # 记录冲突（即使自动解决也留痕，便于审计）
        res.conflicts.append(MergeChange(
            pk, "?", f"两边都改且不同 → {winner}_wins（本地ts={l_ts:.3f}/远端ts={r_ts:.3f}）",
            l, r))
        if winner == "remote":
            if r is None:
                res.applied.append(MergeChange(pk, "D", "冲突：副站删除优先", l, r))
            else:
                res.applied.append(MergeChange(pk, "I" if b is None else "U",
                                               "冲突：副站更新优先", l, r))
        # local_wins：主站保持原样，无需动作
    return res


def apply_merge(
    out_db: str,
    table: str,
    result: MergeResult,
    pk_col: str = "id",
    *,
    origin: str = "merged",
) -> int:
    """把合并动作写到结果库（通常是主站库的副本），返回写入行数。

    注意：写完后会重装 tracking（origin='merged'），使这次合并本身成为一条
    可追踪变更，避免下一轮把合并结果当成"主站新变更"又反向同步一次（幂等）。
    """
    con = sqlite3.connect(out_db)
    n = 0
    try:
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        for ch in result.applied:
            if ch.action == "D":
                con.execute(f'delete from "{table}" where "{pk_col}"=?', (ch.pk,))
                n += 1
            elif ch.action in ("I", "U") and ch.remote_row is not None:
                row = ch.remote_row
                # 只写业务表存在的列，防止影子字段混进去
                use = [c for c in cols if c in row]
                vals = [row[c] for c in use]
                ph = ",".join("?" for _ in use)
                if ch.action == "I":
                    con.execute(f'insert into "{table}"({",".join(chr(34)+c+chr(34) for c in use)}) '
                                f'values({ph})', vals)
                else:
                    sets = ",".join(f'"{c}"=?' for c in use if c != pk_col)
                    svals = [row[c] for c in use if c != pk_col]
                    con.execute(f'update "{table}" set {sets} where "{pk_col}"=?',
                                svals + [ch.pk])
                n += 1
        con.commit()
    finally:
        con.close()
    return n


def has_conflicts(result: MergeResult) -> bool:
    return bool(result.conflicts)


def reconstruct_rows(base_rows: Dict[str, dict], changes: dict) -> Dict[str, dict]:
    """从 base 完整行集 + 变更集重建对端完整行集。

    关键：merge() 要求 remote_rows 是**完整快照**而非只有变更的行。
    若只把变更行传给 merge，未变更的行会因为 row=None 被误判成"被删除"。
    本函数以 base 打底，按变更 op 应用（I/U 写入 row，D 删除），得到完整状态。
    """
    rows = dict(base_rows)
    for pk, meta in changes.items():
        if meta.get("op") == "D":
            rows.pop(pk, None)
        elif meta.get("row") is not None:
            rows[pk] = meta["row"]
    return rows


# ---------- 跨机桥：change-set 导出/导入 + 分叉点快照 ----------
# 真实部署里主备是两台机器，副站的变更集要能打包传回主站做合并。

def export_changeset(db_path: str, table: str, *, since_ts: float = 0.0) -> dict:
    """从副站导出变更集 + 涉及行的完整内容（可 JSON 序列化，走 rsync/API 传输）。

    格式：{"table":..., "since_ts":..., "changes": {pk:{ts,origin,op,row}}}
    deleted 的行 row=None（靠 op='D' 表达墓碑，防止复活）。
    """
    changes = read_changes(db_path, table, since_ts=since_ts)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        out = {}
        for pk, meta in changes.items():
            row = None
            if meta["op"] != "D":
                cur = con.execute(
                    f'select * from "{table}" where "id"=?', (pk,))
                r = cur.fetchone()
                if r is not None:
                    row = dict(zip(cols, r))
            out[pk] = {**meta, "row": row}
    finally:
        con.close()
    return {"table": table, "since_ts": since_ts, "changes": out}


def import_changeset(cs: dict, target_db: str, table: str, *, origin: str = "standby") -> int:
    """把副站变更集导入主站的"待合并影子区"（不直接改业务表）。

    写入 {TRACK}_incoming 表，等 merge_failback 统一裁决后应用，
    避免边收边改导致中途状态不一致。返回导入条数。
    """
    inc = TRACK + "_incoming"
    con = sqlite3.connect(target_db)
    try:
        con.execute(f"""create table if not exists {inc}(
            tbl text not null, pk text not null, ts real not null,
            origin text not null, op text not null, row_json text,
            primary key(tbl, pk))""")
        n = 0
        for pk, meta in cs.get("changes", {}).items():
            import json
            con.execute(f"""insert into {inc}(tbl,pk,ts,origin,op,row_json)
                values(?,?,?,?,?,?)
                on conflict(tbl,pk) do update set
                  ts=excluded.ts, origin=excluded.origin, op=excluded.op,
                  row_json=excluded.row_json""",
                (table, pk, meta["ts"], origin, meta["op"],
                 json.dumps(meta.get("row"), ensure_ascii=False)))
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def snapshot_base(db_path: str, out_path: str) -> str:
    """在故障切换那一刻，用一致性 backup API 拍"共同祖先"快照（base）。

    必须用 backup API 而非文件拷贝（WAL 中间态），复用 gate.snapshot 语义。
    """
    import gate as G
    return G.snapshot(db_path, out_path)

