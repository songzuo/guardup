#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quorum_live.py —— 把产品 quorum.py 的多地投票能力回流给自建热备系统（P1 补强）。

背景（实地核实 2026-11-23）：
    自建系统 failover-check.timer 只在 bot6 一台跑，bot6 自己探测、自己改 DNS —— 单点。
    bot6 与目标主站之间网络抖动（主站没真挂）会误切；bot6 自身挂了则无人切换。

产品 quorum.py 已经把纯逻辑写好了（vote 多地投票 + Lease fencing），但注释里写明
"真实部署：探测上报来自 6 台 bot 的 HTTP 心跳，租约存 SQLite/etcd" —— 这个传输层
一直没实现（只有测试用的 MemoryStore）。本模块就是补这一层：

    1. SQLite 共享租约存储 SQLiteLeaseStore —— 满足 quorum.Lease 的 store 接口
       （get / set_if），多进程/多机通过共享 SQLite 文件争主，带 fencing epoch。
    2. 多地探测上报汇总 —— 各 bot 把探测结果写到共享 SQLite 的 probe_reports 表，
       投票前读窗口内上报、喂给 quorum.vote()。
    3. fencing 闸门 —— 真正改 DNS 前必须过 can_operate()，确认自己是当前 leader
       且租约未过期，杜绝旧主脑裂双写。

安全底线（与 P0 db_live_sync.py 一致）：
    - 默认 dry-run：report/apply 写操作都要显式 --apply。
    - 本模块只做"判定 + 出证据"，绝不直接调 DNS provider 改解析；真正切换仍走
      自建系统的 engine.py，本模块只给它一个带 fencing 的 go/no-go 信号。
    - 隔离验证：全部用临时目录里的 SQLite，不碰生产 /etc/failover 与任何 bot。

用法：
    python quorum_live.py init   --db /data/failover/quorum.db --nodes nodes.json --apply
    python quorum_live.py report --db /data/failover/quorum.db --node bot-sz --region cn-east --healthy 0 --apply
    python quorum_live.py vote   --db /data/failover/quorum.db
    python quorum_live.py lease  --db /data/failover/quorum.db --holder bot-sz --decide --apply
    python quorum_live.py fence  --db /data/failover/quorum.db --holder bot-sz --epoch 3 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# 复用产品的纯逻辑，不重复实现投票/fencing 算法
import quorum as Q


# ---------------------------------------------------------------- SQLite 共享租约存储
class SQLiteLeaseStore:
    """满足 quorum.Lease 所需 store 接口的 SQLite 实现。

    接口：
        get(key) -> dict | None
        set_if(key, value, expected_epoch) -> bool   # CAS：仅当 epoch 未变时写入

    set_if 用单条 UPDATE ... WHERE epoch=? 的 rowcount 做原子 CAS，多进程/多机
    共享同一个 SQLite 文件时，靠 SQLite 的行锁保证只有一个写入者成功 —— 这就是
    fencing token 单调递增的基础。
    """

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        con = sqlite3.connect(self.path)
        try:
            con.execute(
                "create table if not exists leases ("
                " key text primary key, holder text, epoch integer not null,"
                " expires real not null)")
            con.commit()
        finally:
            con.close()

    def get(self, key: str) -> Optional[dict]:
        con = sqlite3.connect(self.path)
        try:
            row = con.execute(
                "select holder, epoch, expires from leases where key=?", (key,)
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return {"holder": row[0], "epoch": row[1], "expires": row[2]}

    def set_if(self, key: str, value: dict, expected_epoch: int) -> bool:
        """CAS 写回。expected_epoch 与当前 epoch 相等才写，否则拒绝（别人抢先了）。"""
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.execute("begin immediate")          # 拿写锁，串行化 CAS
            cur = con.execute(
                "select epoch from leases where key=?", (key,)).fetchone()
            cur_epoch = cur[0] if cur else 0
            if cur_epoch != expected_epoch:
                con.rollback()
                return False
            con.execute(
                "insert into leases(key, holder, epoch, expires) values(?,?,?,?) "
                "on conflict(key) do update set holder=excluded.holder,"
                "epoch=excluded.epoch, expires=excluded.expires",
                (key, value.get("holder"), value.get("epoch", 0),
                 value.get("expires", 0.0)))
            con.commit()
            return True
        finally:
            con.close()


# ---------------------------------------------------------------- 多地探测上报存储
class ProbeStore:
    """各 bot 的探测上报汇总表（共享 SQLite 文件）。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        con = sqlite3.connect(self.path)
        try:
            con.execute(
                "create table if not exists probe_reports ("
                " node text, region text, healthy integer, latency real,"
                " ts real, weight real default 1.0)")
            con.execute(
                "create index if not exists idx_probe_ts on probe_reports(ts)")
            con.commit()
        finally:
            con.close()

    def report(self, node: str, region: str, healthy: bool,
               latency: float = 0.0, *, ts: Optional[float] = None,
               weight: float = 1.0) -> None:
        ts = time.time() if ts is None else ts
        con = sqlite3.connect(self.path)
        try:
            con.execute(
                "insert into probe_reports(node,region,healthy,latency,ts,weight)"
                " values(?,?,?,?,?,?)",
                (node, region, 1 if healthy else 0, latency, ts, weight))
            con.commit()
        finally:
            con.close()

    def recent(self, window: float, *, now: Optional[float] = None) -> List[Q.Probe]:
        """读窗口内的上报。quorum.vote 自己会按 node 取最新一条，这里只按时间过滤。"""
        now = time.time() if now is None else now
        con = sqlite3.connect(self.path)
        try:
            rows = con.execute(
                "select node,region,healthy,latency,ts from probe_reports "
                "where ts>=? order by ts", (now - window,)).fetchall()
        finally:
            con.close()
        return [Q.Probe(node=r[0], region=r[1], healthy=bool(r[2]),
                        latency_ms=float(r[3]), ts=float(r[4])) for r in rows]

    def weights(self) -> Dict[str, float]:
        con = sqlite3.connect(self.path)
        try:
            rows = con.execute(
                "select node, weight from probe_reports "
                "where ts=(select max(ts) from probe_reports)").fetchall()
        finally:
            con.close()
        return {r[0]: float(r[1]) for r in rows}

    # -- 投票状态持久化：confirm_rounds / cooldown 跨轮次生效的关键 --
    def load_state(self) -> Q.VoteState:
        con = sqlite3.connect(self.path)
        try:
            con.execute(
                "create table if not exists vote_state("
                " id integer primary key check(id=1), bad_rounds integer,"
                " last_switch_ts real)")
            row = con.execute(
                "select bad_rounds, last_switch_ts from vote_state where id=1"
            ).fetchone()
        finally:
            con.close()
        if not row:
            return Q.VoteState()
        ts = row[1]
        return Q.VoteState(
            bad_rounds=row[0],
            last_switch_ts=float("-inf") if ts is None else float(ts))

    def save_state(self, state: Q.VoteState, *, apply: bool = False) -> None:
        if not apply:
            return
        ts = None if state.last_switch_ts == float("-inf") else state.last_switch_ts
        con = sqlite3.connect(self.path)
        try:
            con.execute(
                "create table if not exists vote_state("
                " id integer primary key check(id=1), bad_rounds integer,"
                " last_switch_ts real)")
            con.execute(
                "insert into vote_state(id,bad_rounds,last_switch_ts) values(1,?,?) "
                "on conflict(id) do update set bad_rounds=excluded.bad_rounds,"
                "last_switch_ts=excluded.last_switch_ts",
                (state.bad_rounds, ts))
            con.commit()
        finally:
            con.close()


# ---------------------------------------------------------------- 判定结果
@dataclass
class Decision:
    verdict: str                   # switch | hold | no_quorum | recovering | no_leader
    reason: str
    decision: str = ""             # quorum.vote 的原始 decision
    can_operate: bool = False      # 过了 fencing 闸门才有资格改 DNS
    holder: str = ""
    epoch: int = 0
    bad_rounds: int = 0

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason,
                "quorum_decision": self.decision,
                "can_operate": self.can_operate, "holder": self.holder,
                "epoch": self.epoch, "bad_rounds": self.bad_rounds}


def decide(db_path: str, *, window: float = 120.0,
           quorum_ratio: float = 0.5, min_regions: int = 2,
           confirm_rounds: int = 2, cooldown: float = 120.0,
           actor: Optional[str] = None, state: Optional[Q.VoteState] = None,
           now: Optional[float] = None,
           now_fn: Optional[Callable[[], float]] = None) -> Decision:
    """核心编排：读多地上报 → 投票 → 若判死则过 fencing 闸门。

    actor 是本控制面节点名。投票决定 switch 时，只有当 actor 就是当前 leader
    且租约未过期，can_operate 才为 True —— engine.py 只应在这个信号下改 DNS。
    """
    store = ProbeStore(db_path)
    probes = store.recent(window, now=now)
    st = state if state is not None else Q.VoteState()
    res = Q.vote(
        probes, st, quorum_ratio=quorum_ratio, min_regions=min_regions,
        confirm_rounds=confirm_rounds, cooldown=cooldown, now=now,
        weights=store.weights())

    lease_store = SQLiteLeaseStore(db_path)
    # fencing 校验的时钟与投票时钟同源（默认 time.time，测试可注入虚拟钟）
    lease = Q.Lease(lease_store, key="guardup/leader",
                    now_fn=now_fn or (lambda: (time.time() if now is None else now)))
    cur = lease_store.get("guardup/leader") or {"holder": None, "epoch": 0}
    holder, epoch = cur.get("holder"), cur.get("epoch", 0)

    if res.decision != "switch":
        return Decision(verdict=res.decision, reason=res.reason,
                        decision=res.decision, holder=holder or "",
                        epoch=epoch, bad_rounds=state.bad_rounds if state else 0)

    # 判死：过 fencing 闸门
    if actor is None:
        return Decision(verdict="no_leader", 
                        reason="投票判死但未指定 actor，无法校验 fencing",
                        decision=res.decision, holder=holder or "",
                        epoch=epoch, bad_rounds=st.bad_rounds)
    ok = lease.is_leader(actor, epoch)
    return Decision(
        verdict="switch" if ok else "no_leader",
        reason=("投票判死且 fencing 通过，允许切换" if ok else
                f"投票判死但 fencing 拒绝：当前 leader={holder}, epoch={epoch}, actor={actor}"),
        decision=res.decision, can_operate=ok,
        holder=holder or "", epoch=epoch, bad_rounds=st.bad_rounds)


def can_operate(db_path: str, holder: str, epoch: int,
                *, now_fn: Callable[[], float] = time.time) -> bool:
    """fencing 闸门：改 DNS 前最后一道校验，确认仍是当前 leader 且租约未过期。"""
    lease = Q.Lease(SQLiteLeaseStore(db_path), key="guardup/leader", now_fn=now_fn)
    return lease.is_leader(holder, epoch)


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="quorum_live.py",
        description="GuardUp 自建系统多地投票补强（共享租约 + 上报汇总 + fencing 闸门）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_r = sub.add_parser("report", help="上报一次探测结果（默认 dry-run）")
    p_r.add_argument("--db", required=True)
    p_r.add_argument("--node", required=True, help="探测点标识，如 bot-sz")
    p_r.add_argument("--region", required=True, help="地域，如 cn-east")
    p_r.add_argument("--healthy", type=int, choices=[0, 1], required=True,
                     help="1=目标健康 0=判死")
    p_r.add_argument("--latency", type=float, default=0.0)
    p_r.add_argument("--weight", type=float, default=1.0)
    p_r.add_argument("--apply", action="store_true")

    p_v = sub.add_parser("vote", help="读多地上报做投票判定（只读）")
    p_v.add_argument("--db", required=True)
    p_v.add_argument("--window", type=float, default=120.0)
    p_v.add_argument("--quorum-ratio", type=float, default=0.5)
    p_v.add_argument("--min-regions", type=int, default=2)
    p_v.add_argument("--confirm-rounds", type=int, default=2)
    p_v.add_argument("--cooldown", type=float, default=120.0)
    p_v.add_argument("--actor", default=None, help="本控制面节点名，用于 fencing 校验")
    p_v.add_argument("--apply", action="store_true",
                     help="写回投票状态（连续确认/冷却跨轮次生效）；默认只读")

    p_l = sub.add_parser("lease", help="控制面争主 / 续租 / 查状态")
    p_l.add_argument("--db", required=True)
    p_l.add_argument("--holder", required=True)
    p_l.add_argument("--action", choices=["status", "acquire", "renew"], default="status")
    p_l.add_argument("--epoch", type=int, default=None, help="renew 时用")
    p_l.add_argument("--apply", action="store_true")

    p_f = sub.add_parser("fence", help="fencing 闸门：确认能否改 DNS")
    p_f.add_argument("--db", required=True)
    p_f.add_argument("--holder", required=True)
    p_f.add_argument("--epoch", type=int, required=True)

    p_i = sub.add_parser("init", help="初始化共享 SQLite（建表）")
    p_i.add_argument("--db", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "init":
        store = ProbeStore(args.db)
        SQLiteLeaseStore(args.db)
        print(json.dumps({"ok": True, "db": args.db, "tables": ["probe_reports", "leases"]},
                         ensure_ascii=False))
        return 0

    if args.cmd == "report":
        if not args.apply:
            print(f"[dry-run] 计划上报 node={args.node} region={args.region} "
                  f"healthy={args.healthy} latency={args.latency} → {args.db}")
            print("[dry-run] 加 --apply 真正写入共享库")
            return 0
        store = ProbeStore(args.db)
        store.report(args.node, args.region, bool(args.healthy),
                     args.latency, weight=args.weight)
        n = len(store.recent(3600))
        print(json.dumps({"ok": True, "node": args.node, "reports_in_1h": n},
                         ensure_ascii=False))
        return 0

    if args.cmd == "vote":
        store = ProbeStore(args.db)
        st = store.load_state()
        d = decide(
            args.db, window=args.window, quorum_ratio=args.quorum_ratio,
            min_regions=args.min_regions, confirm_rounds=args.confirm_rounds,
            cooldown=args.cooldown, actor=args.actor, state=st)
        store.save_state(st, apply=args.apply)
        out = d.to_dict()
        out["state_saved"] = bool(args.apply)
        out["bad_rounds_after"] = st.bad_rounds
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "lease":
        ls = SQLiteLeaseStore(args.db)
        lease = Q.Lease(ls, key="guardup/leader")
        cur = ls.get("guardup/leader") or {"holder": None, "epoch": 0, "expires": 0.0}
        if args.action == "status":
            print(json.dumps(cur, ensure_ascii=False))
            return 0
        if not args.apply:
            print(f"[dry-run] 计划 {args.action} holder={args.holder} → {args.db}")
            print("[dry-run] 加 --apply 真正执行")
            return 0
        if args.action == "acquire":
            r = lease.acquire(args.holder)
        else:  # renew
            if args.epoch is None:
                print("renew 需要 --epoch", file=sys.stderr)
                return 2
            r = {"ok": lease.renew(args.holder, args.epoch)}
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 3

    if args.cmd == "fence":
        ok = can_operate(args.db, args.holder, args.epoch)
        print(json.dumps({"can_operate": ok, "holder": args.holder,
                          "epoch": args.epoch}, ensure_ascii=False))
        return 0 if ok else 3

    return 1


if __name__ == "__main__":
    sys.exit(main())
