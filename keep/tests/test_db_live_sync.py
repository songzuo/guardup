#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_live_sync.py 端到端测试 —— 真 SQLite，不 mock 核心逻辑。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import db_live_sync as M  # noqa: E402


def make_db(path: str, rows: int = 5) -> None:
    con = sqlite3.connect(path)
    con.execute("create table orders(id integer primary key, name text, qty int)")
    for i in range(1, rows + 1):
        con.execute("insert into orders values(?,?,?)", (i, f"item{i}", i * 10))
    con.commit()
    con.close()


class TestDbLiveSync(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="gu_dblive_")
        self.src = os.path.join(self.d, "app.db")
        make_db(self.src, 5)
        self.dest = os.path.join(self.d, "standby_db")
        os.makedirs(self.dest, exist_ok=True)

    # 1. backup API 快照一致性
    def test_01_snapshot_sqlite_consistent(self):
        snap = M.snapshot_sqlite(self.src, os.path.join(self.dest, "app.dump"))
        self.assertEqual(snap.kind, "sqlite")
        self.assertGreater(snap.size, 64)
        self.assertEqual(len(snap.checksum), 64)
        con = sqlite3.connect(snap.path)
        rows = con.execute("select count(*) from orders").fetchone()[0]
        con.close()
        self.assertEqual(rows, 5)

    # 2. 快照不是文件拷贝能糊弄的：源加一行，旧快照行数不变
    def test_02_snapshot_is_point_in_time(self):
        snap = M.snapshot_sqlite(self.src, os.path.join(self.dest, "pt.dump"))
        con = sqlite3.connect(self.src)
        con.execute("insert into orders values(99,'later',0)")
        con.commit()
        con.close()
        vr = M.verify(snap.path)
        self.assertEqual(vr.rows, 5)  # 快照里仍是 5 行

    # 3. 并发写入下 backup API 不崩、拿到一致库
    def test_03_backup_under_concurrent_writes(self):
        stop = threading.Event()
        def writer():
            i = 100
            while not stop.is_set():
                try:
                    c = sqlite3.connect(self.src, timeout=5)
                    c.execute("insert or ignore into orders values(?,?,?)", (i, "w", i))
                    c.commit()
                    c.close()
                except sqlite3.OperationalError:
                    pass
                i += 1
                time.sleep(0.001)
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.05)
        snap = M.snapshot_sqlite(self.src, os.path.join(self.dest, "concurrent.dump"))
        stop.set()
        t.join(timeout=2)
        vr = M.verify(snap.path)
        self.assertTrue(vr.ok, vr.reason)
        self.assertGreaterEqual(vr.rows, 5)

    # 4. verify 拒绝损坏快照
    def test_04_verify_rejects_corrupt(self):
        bad = os.path.join(self.d, "bad.dump")
        with open(bad, "wb") as fh:
            fh.write(b"SQLite format 3\x00" + b"\x00" * 200)
        vr = M.verify(bad)
        self.assertFalse(vr.ok)

    # 5. verify 拒绝空/小文件
    def test_05_verify_rejects_tiny(self):
        tiny = os.path.join(self.d, "tiny.dump")
        with open(tiny, "wb") as fh:
            fh.write(b"x")
        self.assertFalse(M.verify(tiny).ok)

    # 6. save + list + prune PITR
    def test_06_pitr_save_list_prune(self):
        for i in range(3):
            s = M.snapshot_sqlite(self.src, os.path.join(self.dest, f"s{i}.dump"))
            s.created_at = 1000 + i
            M.save(s, self.dest, apply=True)
        snaps = M.list_snapshots(self.dest)
        self.assertEqual(len(snaps), 3)
        self.assertGreater(snaps[0].created_at, snaps[1].created_at)
        self.assertEqual(M.prune(self.dest, keep=2, apply=True), 1)
        self.assertEqual(len(M.list_snapshots(self.dest)), 2)

    # 7. 恢复演练：还原到临时目录真查询
    def test_07_restore_drill(self):
        snap = M.snapshot_sqlite(self.src, os.path.join(self.dest, "drill.dump"))
        res = M.restore_drill(snap.path, table="orders", workdir=os.path.join(self.d, "drill"))
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["rows"], 5)
        self.assertIn("temp_path", res)
        # 演练库真存在且能查
        con = sqlite3.connect(res["temp_path"])
        self.assertEqual(con.execute("select qty from orders where id=1").fetchone()[0], 10)
        con.close()

    # 8. dry-run 不落盘
    def test_08_dry_run_no_write(self):
        rc = M.main(["snapshot", "--src", self.src, "--dest-dir", self.dest])
        self.assertEqual(rc, 0)
        self.assertEqual(M.list_snapshots(self.dest), [])

    # 9. MySQL 转储通过 runner 桩验证（不装 mysql 客户端也能测核心逻辑）
    def test_09_mysql_dump_via_runner(self):
        out = os.path.join(self.dest, "mysql.dump")
        def stub(cmd):
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("CREATE TABLE `t` (id int);\nINSERT INTO `t` VALUES(1);\n" * 10)
            return 0
        snap = M.snapshot_mysql("--host=local db", out, runner=stub)
        self.assertEqual(snap.kind, "mysql")
        vr = M.verify(out, kind="mysql")
        self.assertTrue(vr.ok, vr.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
