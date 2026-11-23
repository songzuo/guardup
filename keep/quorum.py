#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quorum.py —— 引擎去单点：控制面双副本 + 多地投票确认（v2 方案自动化缺口 3）。

现状痛点（_failover_live/README.md 自述）：
    - failover 引擎只跑在备机，备机自己挂了就没人切 —— 单点。
    - 单个探测点误判（本机网卡抖动/机房网络抖动）会导致错误切换。

本模块解决两件事：

    1. 多地投票（Vote）：N 个探测点各自上报目标健康状态，按法定人数
       (quorum) 判定是否真故障，连续轮次确认 + 冷却 + 漂移自愈。
       支持权重（不同地域/运营商），支持「至少 K 个不同地域同意」防止
       同一机房的多个探测点把单点评判放大成多数。
    2. 控制面双副本 lease：两个控制面实例用带租约的 leader 锁争主，
       主节点定期续租；租约过期后备节点接管。租约带 fencing token
       (epoch)，旧主拿不到 fencing 就不能操作 DNS，杜绝脑裂双写。

设计上纯逻辑、零外部依赖，探测上报/租约存储都通过接口注入：
    - 真实部署：探测上报来自 6 台 bot 的 HTTP 心跳，租约存 SQLite/etcd。
    - 本机测试：用内存字典 + 注入的时钟，可完整演练故障、脑裂、漂移。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Probe:
    """一个探测点的一次上报。"""
    node: str                      # 探测点标识，如 "bot-sz" / "bot-hk"
    region: str                    # 地域，如 "cn-east" / "ap-hk"
    healthy: bool                  # 目标是否健康
    latency_ms: float = 0.0
    ts: float = 0.0


@dataclass
class VoteResult:
    decision: str                  # switch | hold | no_quorum | recovering
    reason: str
    healthy_weight: float = 0.0
    unhealthy_weight: float = 0.0
    regions_agree: int = 0
    rounds: int = 0


@dataclass
class VoteState:
    """跨轮次的投票状态机：连续 N 轮判死才切，切后进入冷却防抖。"""
    bad_rounds: int = 0
    # 初始为 -inf 表示"从未切换过"，否则 if now 从小值起会被误判进冷却期
    last_switch_ts: float = float("-inf")
    total_weight: float = 0.0


def vote(
    probes: List[Probe],
    state: VoteState,
    *,
    quorum_ratio: float = 0.5,
    min_regions: int = 2,
    confirm_rounds: int = 2,
    cooldown: float = 120.0,
    now: Optional[float] = None,
    weights: Optional[Dict[str, float]] = None,
) -> VoteResult:
    """多地投票决定是否切换。

    规则（全部来自实战踩坑）：
      - 健康/不健康按权重求和，权重默认每个探测点 1；同一 node 重复上报只取最新一条。
      - 不健康权重占比 > quorum_ratio 才算「多数判死」。
      - 且同意判死的探测点必须覆盖 >= min_regions 个不同地域 —— 防同机房误判。
      - 连续 confirm_rounds 轮多数判死才触发 switch —— 防瞬时抖动。
      - 距上次切换 < cooldown 秒进入 recovering —— 防来回抖（flapping）。
      - 一旦某轮恢复多数健康，bad_rounds 清零 —— 漂移自愈。
    """
    now = time.time() if now is None else now
    weights = weights or {}

    # 同一 node 只取最新一条上报
    latest: Dict[str, Probe] = {}
    for p in probes:
        if p.node not in latest or p.ts >= latest[p.node].ts:
            latest[p.node] = p

    total_w = 0.0
    bad_w = 0.0
    bad_regions = set()
    for node, p in latest.items():
        w = weights.get(node, 1.0)
        total_w += w
        if not p.healthy:
            bad_w += w
            bad_regions.add(p.region)

    if total_w == 0:
        return VoteResult("no_quorum", "无有效探测上报", 0, 0, 0, state.bad_rounds)

    bad_ratio = bad_w / total_w
    majority_bad = bad_ratio > quorum_ratio and len(bad_regions) >= min_regions

    # 多数健康 → 漂移自愈，清零连续计数
    if (1 - bad_ratio) > quorum_ratio:
        state.bad_rounds = 0
        return VoteResult("hold", "多数探测点健康，维持现状",
                          total_w - bad_w, bad_w, len(bad_regions), 0)

    if not majority_bad:
        state.bad_rounds = 0
        return VoteResult("no_quorum",
                          f"未达法定人数(判死占比={bad_ratio:.2f}，地域={len(bad_regions)})，继续观察",
                          total_w - bad_w, bad_w, len(bad_regions), 0)

    state.bad_rounds += 1
    if state.bad_rounds < confirm_rounds:
        return VoteResult("hold",
                          f"多数判死但确认轮次不足({state.bad_rounds}/{confirm_rounds})",
                          total_w - bad_w, bad_w, len(bad_regions), state.bad_rounds)

    if now - state.last_switch_ts < cooldown:
        return VoteResult("recovering",
                          f"处于切换冷却期(距上次 {now - state.last_switch_ts:.0f}s)，抑制抖动",
                          total_w - bad_w, bad_w, len(bad_regions), state.bad_rounds)

    state.last_switch_ts = now
    return VoteResult("switch",
                      f"多地确认故障(判死占比={bad_ratio:.2f}，地域={len(bad_regions)})，执行切换",
                      total_w - bad_w, bad_w, len(bad_regions), state.bad_rounds)


class Lease:
    """带 fencing token 的控制面租约（避免脑裂双写）。

    存储通过 store 注入，需实现：
        get(key) -> dict | None
        set_if(key, value, expected_epoch) -> bool   # CAS：仅当 epoch 未变时写入
    这样即使两个控制面同时争主，只有一个能拿到递增 epoch 的租约。
    """

    def __init__(self, store, key: str = "guardup/leader", ttl: float = 30.0,
                 now_fn: Callable[[], float] = time.time):
        self.store = store
        self.key = key
        self.ttl = ttl
        self.now_fn = now_fn

    def _read(self) -> dict:
        return self.store.get(self.key) or {"holder": None, "epoch": 0, "expires": 0.0}

    def acquire(self, holder: str) -> dict:
        """争主：租约空/过期时，CAS 写入本节点 + epoch+1，返回 fencing token。"""
        cur = self._read()
        now = self.now_fn()
        expired = now >= cur["expires"]
        if cur["holder"] not in (None, holder) and not expired:
            return {"ok": False, "epoch": cur["epoch"], "holder": cur["holder"]}
        new = {"holder": holder, "epoch": cur["epoch"] + 1, "expires": now + self.ttl}
        ok = self.store.set_if(self.key, new, cur["epoch"])
        return {"ok": ok, "epoch": new["epoch"] if ok else cur["epoch"],
                "holder": holder if ok else cur["holder"]}

    def renew(self, holder: str, epoch: int) -> bool:
        """续租：仅当本节点仍是持有者且 epoch 匹配才成功。"""
        cur = self._read()
        if cur["holder"] != holder or cur["epoch"] != epoch:
            return False
        if self.now_fn() >= cur["expires"]:
            return False
        new = dict(cur)
        new["expires"] = self.now_fn() + self.ttl
        return self.store.set_if(self.key, new, epoch)

    def is_leader(self, holder: str, epoch: int) -> bool:
        """ fencing 校验：操作 DNS 前必须确认自己仍是持有者且未过期。"""
        cur = self._read()
        return (cur["holder"] == holder and cur["epoch"] == epoch
                and self.now_fn() < cur["expires"])


class MemoryStore:
    """租约存储的内存实现（测试用；真实部署换 SQLite/etcd）。"""

    def __init__(self):
        self.data: Dict[str, dict] = {}

    def get(self, key: str) -> Optional[dict]:
        v = self.data.get(key)
        return dict(v) if v else None

    def set_if(self, key: str, value: dict, expected_epoch: int) -> bool:
        cur = self.data.get(key)
        cur_epoch = cur["epoch"] if cur else 0
        if (cur is None and expected_epoch == 0) or (cur and cur_epoch == expected_epoch):
            self.data[key] = dict(value)
            return True
        return False
