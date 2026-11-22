"""GuardUp 核心逻辑自测 — 用假 provider，不碰真实 DNS / 网络。

覆盖：
  1. 主站挂 -> 连续失败达阈值 -> 切到备站
  2. 备站期间主站恢复 -> 连续成功达阈值 -> 切回
  3. 冷却期阻止抖动
  4. 漂移自愈（记录被人手改回去）
  5. dry-run 绝不真改 DNS
  6. 切换失败不丢计数（下一轮可重试）

运行: python tests/test_guardup.py
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardup.engine import Config, Failover, State

TMP = tempfile.mkdtemp(prefix="guardup-test-")
STATE = os.path.join(TMP, "state.json")


class FakeProvider:
    """内存 DNS 记录 + 可编程的主站健康状态。"""
    store = {}          # (zone, name) -> value
    healthy = True      # 主站是否健康
    fail_switch = False  # 模拟写 API 报错
    writes = 0

    def __init__(self, **kw):
        pass

    def find_record(self, zone, name, rec_type="A", *a, **kw):
        return ("rid", self.store.get((zone, name), "1.1.1.1"))

    def set_record(self, zone, name, value, rec_type="A", ttl=60, **kw):
        if self.fail_switch:
            raise RuntimeError("simulated API error")
        self.store[(zone, name)] = value
        FakeProvider.writes += 1
        return value


def make_cfg(fail=2, recover=2, cooldown=0):
    return Config({
        "primary_ip": "1.1.1.1",
        "backup_ip": "2.2.2.2",
        "fail_threshold": fail,
        "recover_threshold": recover,
        "cooldown_seconds": cooldown,
        "http_timeout": 1,
        "targets": [{"provider": "fake", "zone": "example.com",
                     "record_name": "@", "type": "A", "ttl": 60,
                     "host": "example.com", "path": "/"}],
    })


def fresh_engine(cfg, dry=False):
    if os.path.exists(STATE):
        os.remove(STATE)
    FakeProvider.store = {("example.com", "@"): "1.1.1.1"}
    FakeProvider.healthy = True
    FakeProvider.fail_switch = False
    FakeProvider.writes = 0
    fo = Failover(cfg, State(STATE), dry_run=dry)
    fo.provider = lambda kind: FakeProvider()
    return fo


class TestGuardUp(unittest.TestCase):

    def setUp(self):
        self.cfg = make_cfg()

    def test_1_failover_to_backup(self):
        fo = fresh_engine(self.cfg)
        FakeProvider.healthy = False
        # 用 handle_target 但替换 check_target 为受控健康
        fo.check_target = lambda t: {"ok": FakeProvider.healthy, "detail": {}}
        fo.handle_target(self.cfg.targets[0])
        fo.handle_target(self.cfg.targets[0])
        st = fo.state.get("example.com|@")
        self.assertEqual(st["mode"], "backup")
        self.assertEqual(FakeProvider.store[("example.com", "@")], "2.2.2.2")

    def test_2_recover_to_primary(self):
        fo = fresh_engine(self.cfg)
        t = self.cfg.targets[0]
        fo.check_target = lambda t: {"ok": FakeProvider.healthy, "detail": {}}
        FakeProvider.healthy = False
        fo.handle_target(t); fo.handle_target(t)   # 切到备
        FakeProvider.healthy = True
        fo.handle_target(t); fo.handle_target(t)   # 切回主
        st = fo.state.get("example.com|@")
        self.assertEqual(st["mode"], "primary")
        self.assertEqual(FakeProvider.store[("example.com", "@")], "1.1.1.1")

    def test_3_cooldown_blocks_flap(self):
        cfg = make_cfg(cooldown=9999)
        fo = fresh_engine(cfg)
        t = cfg.targets[0]
        fo.check_target = lambda t: {"ok": FakeProvider.healthy, "detail": {}}
        FakeProvider.healthy = False
        fo.handle_target(t); fo.handle_target(t)   # 切到备，last_switch=now
        # 主站恢复，但冷却期内不应切回
        FakeProvider.healthy = True
        fo.handle_target(t); fo.handle_target(t)
        st = fo.state.get("example.com|@")
        self.assertEqual(st["mode"], "backup", "冷却期内不该切回")

    def test_4_drift_repair(self):
        fo = fresh_engine(self.cfg)
        t = self.cfg.targets[0]
        fo.check_target = lambda t: {"ok": True, "detail": {}}
        # 人为把记录改错
        FakeProvider.store[("example.com", "@")] = "9.9.9.9"
        r = fo.handle_target(t)
        self.assertEqual(r["action"], "repair_drift")
        self.assertEqual(FakeProvider.store[("example.com", "@")], "1.1.1.1")

    def test_5_dry_run_never_writes(self):
        fo = fresh_engine(self.cfg, dry=True)
        t = self.cfg.targets[0]
        fo.check_target = lambda t: {"ok": False, "detail": {}}
        fo.handle_target(t); fo.handle_target(t)
        self.assertEqual(FakeProvider.writes, 0, "dry-run 绝不能写 DNS")
        self.assertEqual(FakeProvider.store[("example.com", "@")], "1.1.1.1")

    def test_6_switch_failure_keeps_counter(self):
        fo = fresh_engine(self.cfg)
        t = self.cfg.targets[0]
        fo.check_target = lambda t: {"ok": False, "detail": {}}
        FakeProvider.fail_switch = True
        fo.handle_target(t)   # fails=1，未达阈值
        r1 = fo.handle_target(t)   # fails=2，触发切换但 API 报错
        self.assertEqual(r1["action"], "switch_failed")
        st = fo.state.get("example.com|@")
        self.assertEqual(st["fails"], 2, "切换失败不能丢计数")
        self.assertEqual(st["mode"], "primary", "失败不该改状态")
        # 恢复 API，下一轮应能成功
        FakeProvider.fail_switch = False
        r2 = fo.handle_target(t)
        self.assertEqual(r2["action"], "switch_to_backup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
