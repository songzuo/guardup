#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep 核心模块自测 —— 真 SQLite（WAL 模式）+ 真 backup API + 真回滚。

不用假 provider / 假快照，全部走真实 SQLite 引擎，验证那套"血泪逻辑"真的对。
覆盖：
  1. snapshot: WAL 模式下 backup API 拿到一致快照（含未 checkpoint 的数据）
  2. verify: good 库通过；空业务表 / 损坏库被拒
  3. swap: 无变化不动 / dry-run 只读 / 换库成功 / 健康检查失败自动回滚
  4. discover: server 级 root 优先，排除 ACME 假根；自动发现 SQLite 库
  5. watch: golden 快照，文件骤减检测为退化
  6. profile: 三档声明式配置加载
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gate as G  # noqa: E402
import discover as D  # noqa: E402
import watch as W  # noqa: E402
from profile import ServiceProfile, load_profiles  # noqa: E402


def make_db(path, channels=3, wal=True):
    """造一个 api-gateway 风格的库，可指定 WAL 模式与 channels 行数。"""
    con = sqlite3.connect(path)
    if wal:
        con.execute("pragma journal_mode=WAL")
    con.execute("create table if not exists channels(id integer primary key, name text)")
    con.execute("delete from channels")
    for i in range(channels):
        con.execute("insert into channels(name) values(?)", (f"ch{i}",))
    con.commit()
    con.close()


class TestGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="keep-gate-")
        self.live = os.path.join(self.d, "live.db")
        self.snap = os.path.join(self.d, "snap.db")

    def test_01_snapshot_wal_consistency(self):
        """WAL 里未 checkpoint 的数据也必须进快照——这是不能裸拷的根本原因。"""
        make_db(self.live, channels=3, wal=True)
        # 保持写连接打开：最后一个连接关闭时 SQLite 会自动 checkpoint 并删 WAL，
        # 所以要在连接仍开着的状态下做快照，才能证明 backup 合并了 WAL。
        c = sqlite3.connect(self.live)
        c.execute("insert into channels(name) values('in-wal')")
        c.commit()   # 事务提交进 WAL，不执行 wal_checkpoint
        self.assertTrue(os.path.exists(self.live + "-wal"), "前置：WAL 文件应存在（写连接未关）")
        G.snapshot(self.live, self.snap)   # backup 只读打开，能看到 WAL 数据
        c.close()
        vr = G.verify(self.snap, ["channels"])
        self.assertTrue(vr.ok, vr.reason)
        self.assertEqual(vr.table_counts["channels"], 4, "快照应包含 WAL 中的那条数据")

    def test_02_verify_rejects_bad(self):
        """空业务表 + 损坏库都要被拒。"""
        make_db(self.live, channels=0, wal=False)
        G.snapshot(self.live, self.snap)
        self.assertFalse(G.verify(self.snap, ["channels"]).ok, "channels=0 应被拒")
        self.assertTrue(G.verify(self.snap, []).ok, "不带业务表时 integrity 通过即可")
        # 真损坏：随机字节覆盖
        with open(self.snap, "wb") as fh:
            fh.write(os.urandom(4096))
        vr = G.verify(self.snap, [])
        self.assertFalse(vr.ok)
        self.assertTrue("integrity" in vr.reason or "打不开库" in vr.reason,
                        f"损坏库应被拒，reason={vr.reason}")

    def test_03_swap_unchanged_and_dryrun(self):
        make_db(self.live, channels=3, wal=False)
        G.snapshot(self.live, self.snap)
        # 逻辑内容相同 → unchanged（即使字节不同，靠逻辑指纹识别）
        r = G.swap_with_rollback(self.snap, self.live, ["channels"], lambda: True)
        self.assertEqual(r.action, "unchanged", r.detail)
        # 制造真实内容变化 + dry-run → 不替换
        c = sqlite3.connect(self.snap)
        c.execute("insert into channels(name) values('new')"); c.commit(); c.close()
        before = open(self.live, "rb").read()
        r = G.swap_with_rollback(self.snap, self.live, ["channels"], lambda: True, dry_run=True)
        self.assertEqual(r.action, "unchanged")
        self.assertEqual(open(self.live, "rb").read(), before, "dry-run 绝不能改 live 库")

    def test_04_swap_success_and_rollback(self):
        make_db(self.live, channels=3, wal=False)
        G.snapshot(self.live, self.snap)
        # 改快照，制造"有变化"
        c = sqlite3.connect(self.snap)
        c.execute("insert into channels(name) values('new')"); c.commit(); c.close()
        # 健康检查成功 → swapped
        r = G.swap_with_rollback(self.snap, self.live, ["channels"],
                                 lambda: True, health_interval=0.01,
                                 backup_dir=os.path.join(self.d, "bk"))
        self.assertEqual(r.action, "swapped", r.detail)
        self.assertEqual(G.verify(self.live, ["channels"]).table_counts["channels"], 4)

        # 再改一次，健康检查恒失败 → 回滚
        c = sqlite3.connect(self.snap)
        c.execute("insert into channels(name) values('bad')"); c.commit(); c.close()
        r = G.swap_with_rollback(self.snap, self.live, ["channels"],
                                 lambda: False, health_interval=0.01, health_timeout=0.05,
                                 backup_dir=os.path.join(self.d, "bk"))
        self.assertEqual(r.action, "rolled_back", r.detail)
        self.assertEqual(G.verify(self.live, ["channels"]).table_counts["channels"], 4,
                         "回滚后 live 库必须回到换库前的 4 行")

    def test_05_reject_bad_newdb_never_touches_live(self):
        make_db(self.live, channels=3, wal=False)
        before = open(self.live, "rb").read()
        with open(self.snap, "wb") as fh:
            fh.write(os.urandom(2048))
        r = G.swap_with_rollback(self.snap, self.live, ["channels"], lambda: True)
        self.assertEqual(r.action, "rejected")
        self.assertEqual(open(self.live, "rb").read(), before, "坏库绝对不能覆盖现库")


class TestDiscover(unittest.TestCase):
    def test_06_nginx_server_level_and_acme(self):
        d = tempfile.mkdtemp(prefix="keep-disc-")
        conf = os.path.join(d, "site.conf")
        open(conf, "w", encoding="utf-8").write("""
server {
    listen 80;
    server_name example.com www.example.com;
    root /var/www/real-site;
    location ^~ /.well-known/acme-challenge/ { root /web/acme/challenges; }
    location /api { root /var/www/wrong-nested; }
}
""")
        found = D.discover_docroots(["example.com"], conf_globs=[conf])
        self.assertIn("example.com", found)
        # 产品跑在 Linux；Windows 上 realpath 会把 /var/... 挂到当前盘符，故比 basename 尾部
        got = found["example.com"][0].replace("\\", "/")
        self.assertTrue(got.endswith("/var/www/real-site"),
                        f"必须取 server 级 root，不能取 ACME/location 假根，实际={got}")

    def test_07_discover_sqlite_db(self):
        d = tempfile.mkdtemp(prefix="keep-db-")
        db = os.path.join(d, "app.db")
        make_db(db, channels=2, wal=False)
        found = D.discover_sqlite_db(["channels"], scan_dirs=[os.path.join(d, "*.db")])
        self.assertEqual(found, os.path.realpath(db))


class TestWatch(unittest.TestCase):
    def test_08_golden_degradation(self):
        d = tempfile.mkdtemp(prefix="keep-watch-")
        site = os.path.join(d, "site")
        os.makedirs(site)
        for i in range(10):
            with open(os.path.join(site, f"p{i}.html"), "w") as fh:
                fh.write("x" * 100)
        gpath = os.path.join(d, "golden.json")
        W.save_golden(site, gpath)
        r = W.check(site, gpath)
        self.assertFalse(r.degraded, r.reason)
        # 删掉 8 个 → 剩 2 个 < 50% → 退化
        for i in range(8):
            os.remove(os.path.join(site, f"p{i}.html"))
        r = W.check(site, gpath)
        self.assertTrue(r.degraded, "文件数骤减应判为退化")
        self.assertIn("骤减", r.reason)


class TestProfile(unittest.TestCase):
    def test_09_profile_tiers(self):
        p = ServiceProfile(name="x.com", tier="stateful", business_tables=["channels"])
        self.assertIn("应用接管", p.describe())
        with self.assertRaises(ValueError):
            ServiceProfile(name="x", tier="bogus")
        d = tempfile.mkdtemp(prefix="keep-prof-")
        f = os.path.join(d, "p.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump({"profiles": [{"name": "a.com", "tier": "static"}]}, fh)
        self.assertEqual(len(load_profiles(f)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
