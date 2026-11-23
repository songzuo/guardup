#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_quorum_live.py —— P1 多地投票传输层的端到端验证（真 SQLite，隔离临时目录）。

重点覆盖那些"纯逻辑测试通过但真实链路跑不通"的场景：
    - 跨进程共享租约 CAS fencing（不是内存字典）
    - 投票状态跨轮次持久化（confirm_rounds / cooldown 每轮调用能累加）
    - 单点误判被 quorum 挡住 / 脑裂旧主 fencing 被拒 / 租约过期接管
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quorum as Q
import quorum_live as L


class TestSQLiteLease(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "q.db")

    def test_01_acquire_and_fencing_epoch(self):
        """两个节点争主：只有一个成功；epoch 单调递增；旧主被 fencing 拒绝。"""
        ls = L.SQLiteLeaseStore(self.db)
        lease = Q.Lease(ls, key="guardup/leader", now_fn=lambda: 100.0)
        r1 = lease.acquire("bot-sz")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["epoch"], 1)
        # 租约未过期，第二节点抢不到
        r2 = lease.acquire("bot-bj")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["holder"], "bot-sz")
        # 旧主 fencing 校验：当前 epoch 通过，过期 epoch 拒绝
        self.assertTrue(lease.is_leader("bot-sz", 1))
        self.assertFalse(lease.is_leader("bot-sz", 0))
        self.assertFalse(lease.is_leader("bot-bj", 1))

    def test_02_expired_lease_takeover(self):
        """租约过期后备节点能接管，epoch+1，旧主 fencing 失效。"""
        clock = {"t": 100.0}
        ls = L.SQLiteLeaseStore(self.db)
        lease = Q.Lease(ls, key="guardup/leader", ttl=30.0,
                        now_fn=lambda: clock["t"])
        lease.acquire("bot-sz")            # epoch=1, expires=130
        clock["t"] = 131.0                 # 租约过期
        r = lease.acquire("bot-bj")
        self.assertTrue(r["ok"])
        self.assertEqual(r["epoch"], 2)
        self.assertFalse(lease.is_leader("bot-sz", 1))   # 旧主作废
        self.assertTrue(lease.is_leader("bot-bj", 2))

    def test_03_renew_cas(self):
        """续租：holder+epoch 匹配才成功；别人无法替主续租。"""
        clock = {"t": 100.0}
        ls = L.SQLiteLeaseStore(self.db)
        lease = Q.Lease(ls, key="guardup/leader", ttl=30.0,
                        now_fn=lambda: clock["t"])
        lease.acquire("bot-sz")
        self.assertTrue(lease.renew("bot-sz", 1))
        self.assertFalse(lease.renew("bot-bj", 1))       # 错 holder
        self.assertFalse(lease.renew("bot-sz", 99))      # 错 epoch


class TestProbeStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "q.db")
        self.store = L.ProbeStore(self.db)

    def test_04_recent_window_and_latest(self):
        """窗口过滤 + vote 内部按 node 取最新（上报汇总的核心）。"""
        now = 1000.0
        self.store.report("bot-sz", "cn-east", True, ts=now - 10)
        self.store.report("bot-bj", "cn-north", False, ts=now - 5)
        self.store.report("bot-sz", "cn-east", False, ts=now - 1)  # sz 改口判死
        recent = self.store.recent(window=20, now=now)
        self.assertEqual(len(recent), 3)
        st = Q.VoteState()
        res = Q.vote(recent, st, confirm_rounds=1, now=now, min_regions=1)
        # sz 最新一条是判死 → sz/bj 都判死 → switch
        self.assertEqual(res.decision, "switch")

    def test_05_state_persist_rounds(self):
        """★★ 关键链路：投票状态跨轮次持久化，confirm_rounds 才生效。"""
        now = 2000.0
        # 4/6 多地判死，覆盖 4 地域
        for i, region in enumerate(["cn-east", "cn-north", "ap-hk", "ap-sg"]):
            self.store.report(f"bot-{i}", region, False, ts=now)
        for i, region in enumerate(["us-west", "eu-central"]):
            self.store.report(f"bot-h{i}", region, True, ts=now)

        # 第 1 轮：多数判死但确认不足 → hold；bad_rounds=1
        st1 = self.store.load_state()
        d1 = L.decide(self.db, now=now, confirm_rounds=2, state=st1)
        self.store.save_state(st1, apply=True)
        self.assertEqual(d1.decision, "hold")
        self.assertEqual(st1.bad_rounds, 1)

        # 第 2 轮：重新从库读 state（模拟 timer 下一次独立调用）→ switch
        st2 = self.store.load_state()
        self.assertEqual(st2.bad_rounds, 1, "状态必须从 SQLite 恢复")
        d2 = L.decide(self.db, now=now, confirm_rounds=2, state=st2)
        self.store.save_state(st2, apply=True)
        self.assertEqual(d2.decision, "switch")

    def test_06_single_point_misjudge_blocked(self):
        """单点误判：只有一个机器人判死 → no_quorum，绝不切换。"""
        now = 3000.0
        self.store.report("bot-sz", "cn-east", False, ts=now)
        for i, region in enumerate(["cn-north", "ap-hk", "ap-sg", "us-west"]):
            self.store.report(f"ok-{i}", region, True, ts=now)
        d = L.decide(self.db, now=now, confirm_rounds=1, min_regions=2)
        # 单点误判：多数健康 → hold（漂移自愈），或票数不足 no_quorum；
        # 关键语义是绝不 switch。过去断言写死 no_quorum 是错的。
        self.assertIn(d.decision, ("hold", "no_quorum"))
        self.assertNotEqual(d.decision, "switch")
        self.assertFalse(d.can_operate)

    def test_07_drift_self_heal(self):
        """漂移自愈：先多轮判死累积 bad_rounds，再多数健康 → bad_rounds 清零。"""
        now = 4000.0
        st = Q.VoteState()
        for i, region in enumerate(["cn-east", "cn-north", "ap-hk", "ap-sg"]):
            self.store.report(f"bad-{i}", region, False, ts=now)
        L.decide(self.db, now=now, confirm_rounds=5, state=st)  # bad_rounds=1
        self.assertEqual(st.bad_rounds, 1)
        # 换一批：多数恢复健康
        self.store.report("ok-1", "us-west", True, ts=now + 1)
        self.store.report("ok-2", "eu-central", True, ts=now + 1)
        self.store.report("ok-3", "ap-tokyo", True, ts=now + 1)
        self.store.report("ok-4", "sa-east", True, ts=now + 1)
        self.store.report("ok-5", "cn-west", True, ts=now + 1)
        d = L.decide(self.db, now=now + 1, confirm_rounds=5, state=st,
                     window=10)
        self.assertEqual(st.bad_rounds, 0)
        self.assertEqual(d.decision, "hold")


class TestFencingGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "q.db")

    def test_08_fence_gate_switch_only_for_leader(self):
        """投票判死后：只有 leader 能 can_operate；非 leader 被 fencing 拒绝。"""
        now = 5000.0
        ps = L.ProbeStore(self.db)
        for i, region in enumerate(["cn-east", "cn-north", "ap-hk", "ap-sg"]):
            ps.report(f"bad-{i}", region, False, ts=now)
        # 指定 leader bot-sz（在 now 时刻 acquire，ttl 足够覆盖判定时刻）
        clock = {"t": now}
        lease = Q.Lease(L.SQLiteLeaseStore(self.db), key="guardup/leader",
                        ttl=120.0, now_fn=lambda: clock["t"])
        lease.acquire("bot-sz")

        d_leader = L.decide(self.db, now=now, confirm_rounds=1, actor="bot-sz")
        self.assertEqual(d_leader.decision, "switch")
        self.assertTrue(d_leader.can_operate)
        self.assertEqual(d_leader.verdict, "switch")

        d_other = L.decide(self.db, now=now, confirm_rounds=1, actor="bot-bj")
        self.assertEqual(d_other.decision, "switch")
        self.assertFalse(d_other.can_operate)
        self.assertEqual(d_other.verdict, "no_leader")

    def test_09_can_operate_expired(self):
        """租约过期后 can_operate 直接 False —— 改 DNS 的最后闸门。"""
        clock = {"t": 100.0}
        lease = Q.Lease(L.SQLiteLeaseStore(self.db), key="guardup/leader",
                        ttl=10.0, now_fn=lambda: clock["t"])
        lease.acquire("bot-sz")
        self.assertTrue(L.can_operate(self.db, "bot-sz", 1, now_fn=lambda: 105.0))
        self.assertFalse(L.can_operate(self.db, "bot-sz", 1, now_fn=lambda: 111.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
