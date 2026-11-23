#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quorum.py 自测：多地投票 + 控制面 lease fencing，含脑裂/漂移演练。"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import quorum as Q  # noqa: E402


def probes(states, t=100.0):
    """states: [(node, region, healthy), ...]"""
    return [Q.Probe(node=n, region=r, healthy=h, ts=t) for n, r, h in states]


class TestVote(unittest.TestCase):
    def test_01_majority_bad_switches_after_confirm(self):
        st = Q.VoteState()
        ps = probes([("b1", "cn-east", False), ("b2", "cn-north", False),
                     ("b3", "ap-hk", False), ("b4", "us-west", True)])
        r1 = Q.vote(ps, st, now=100)
        self.assertEqual(r1.decision, "hold", "确认轮次不足，先观察")
        r2 = Q.vote(ps, st, now=101)
        self.assertEqual(r2.decision, "switch", "连续两轮多数判死 → 切换")
        self.assertEqual(r2.regions_agree, 3)

    def test_02_single_region_cannot_trigger(self):
        """同一地域多节点全判死也不能切——防机房级误判放大。"""
        st = Q.VoteState()
        ps = probes([("b1", "cn-east", False), ("b2", "cn-east", False),
                     ("b3", "cn-east", False), ("b4", "cn-east", False)])
        for t in (100, 101, 102):
            r = Q.vote(ps, st, now=t, min_regions=2)
        self.assertNotEqual(r.decision, "switch", "单地域不得触发切换")

    def test_03_drift_repair_resets_counter(self):
        st = Q.VoteState()
        bad = probes([("b1", "cn-east", False), ("b2", "cn-north", False),
                      ("b3", "ap-hk", False), ("b4", "us", True)])
        Q.vote(bad, st, now=100)
        self.assertEqual(st.bad_rounds, 1)
        good = probes([("b1", "cn-east", True), ("b2", "cn-north", True),
                       ("b3", "ap-hk", True), ("b4", "us", True)])
        Q.vote(good, st, now=101)
        self.assertEqual(st.bad_rounds, 0, "多数恢复健康 → 漂移自愈，清零计数")

    def test_04_cooldown_suppresses_flapping(self):
        st = Q.VoteState(last_switch_ts=90)
        ps = probes([("b1", "cn-east", False), ("b2", "cn-north", False),
                     ("b3", "ap-hk", False), ("b4", "us", True)])
        Q.vote(ps, st, now=100)
        r = Q.vote(ps, st, now=101, cooldown=120)
        self.assertEqual(r.decision, "recovering", "冷却期内抑制反复切换")

    def test_05_weighted_vote(self):
        st = Q.VoteState()
        ps = probes([("b1", "cn", False), ("b2", "us", True)])
        # b1 权重 5 vs b2 权重 1 → 判死占比 5/6 > 0.5，但地域只有 1 个判死 → 需 min_regions=1
        r2 = Q.vote(ps, st, now=100, weights={"b1": 5, "b2": 1},
                    min_regions=1, confirm_rounds=1)
        self.assertEqual(r2.decision, "switch")


class TestLease(unittest.TestCase):
    def test_06_fencing_prevents_split_brain(self):
        clock = {"t": 0.0}
        store = Q.MemoryStore()
        lease = Q.Lease(store, ttl=30, now_fn=lambda: clock["t"])
        a = lease.acquire("node-a")
        self.assertTrue(a["ok"])
        b = lease.acquire("node-b")
        self.assertFalse(b["ok"], "租约未过期时 node-b 抢不到")
        self.assertFalse(lease.is_leader("node-a", a["epoch"] - 1), "旧 epoch 被 fencing 拒绝")

    def test_07_expired_lease_takeover(self):
        clock = {"t": 0.0}
        store = Q.MemoryStore()
        lease = Q.Lease(store, ttl=30, now_fn=lambda: clock["t"])
        a = lease.acquire("node-a")
        clock["t"] = 31.0               # 主节点租约过期
        b = lease.acquire("node-b")
        self.assertTrue(b["ok"], "租约过期后备节点接管")
        self.assertGreater(b["epoch"], a["epoch"], "epoch 必须递增")
        self.assertFalse(lease.is_leader("node-a", a["epoch"]), "旧主 fencing token 失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
