#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db.py 自测：PG/MySQL 真副本核心逻辑。

本机无 psql/mysql/docker，故 DB 命令用「桩执行器」注入（写一份假转储文本），
但校验/延迟闸门/PITR 目录/回滚编排全部是真实代码、真实文件操作。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import db as D  # noqa: E402


def fake_dump(text):
    """造一个 cmd_runner 桩：解析 -f <out> 或 > <out>，往目标写转储文本。"""
    def runner(cmd):
        import shlex
        parts = shlex.split(cmd.replace(" > ", " > "))
        out = None
        if "-f" in parts:
            out = parts[parts.index("-f") + 1]
        elif ">" in parts:
            out = parts[parts.index(">") + 1]
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 0
    return runner


class TestCaptureVerify(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="keep-db-")

    def test_01_capture_postgres_and_verify(self):
        out = os.path.join(self.d, "pg.dump")
        text = "CREATE TABLE channels (id int);\nCOPY channels FROM stdin;\n1\n2\n\\.\n"
        snap = D.capture("postgres", out, "dbname=test", cmd_runner=fake_dump(text))
        self.assertEqual(snap.kind, "postgres")
        self.assertIn("schema", snap.markers)
        self.assertIn("data", snap.markers)
        vr = D.verify_dump(out, ("schema", "data"))
        self.assertTrue(vr.ok, vr.reason)

    def test_02_verify_rejects_bad_dump(self):
        out = os.path.join(self.d, "bad.dump")
        with open(out, "w") as fh:
            fh.write("garbage")
        vr = D.verify_dump(out, ("schema",))
        self.assertFalse(vr.ok, "缺 schema 标记应被拒")

    def test_03_mysql_binlog_marker(self):
        out = os.path.join(self.d, "my.dump")
        text = "CREATE TABLE t(id int);\nINSERT INTO t VALUES(1);\nCHANGE MASTER TO MASTER_LOG_FILE='binlog.000001';\n"
        D.capture("mysql", out, "test", cmd_runner=fake_dump(text))
        markers = D.detect_markers(out)
        self.assertIn("schema", markers)
        self.assertIn("binlog_pos", markers)


class TestLagGate(unittest.TestCase):
    def test_04_lag_gate(self):
        self.assertTrue(D.lag_gate(5).allowed, "低延迟放行")
        self.assertFalse(D.lag_gate(120).allowed, "高延迟暂缓")
        self.assertTrue(D.lag_gate(120, force=True).allowed, "演练 force 放行")
        self.assertFalse(D.lag_gate(-1).allowed, "负延迟拒绝")


class TestPITR(unittest.TestCase):
    def test_05_pitr_catalog_prune_pick(self):
        d = tempfile.mkdtemp(prefix="keep-pitr-")
        for i, ts in enumerate([100.0, 200.0, 300.0, 400.0]):
            dump = os.path.join(d, f"s{i}.dump")
            with open(dump, "w") as fh:
                fh.write("CREATE TABLE t(id int);")
            D.save_manifest(D.Snapshot(path=dump, kind="postgres", created_at=ts), d)
        snaps = D.list_pitr(d)
        self.assertEqual(len(snaps), 4)
        self.assertEqual(snaps[0].created_at, 400.0, "按创建时间倒序")
        picked = D.pick_for_point(d, 250.0)
        self.assertEqual(picked.created_at, 200.0, "PITR 选 <= 目标的最近一份")
        removed = D.prune_pitr(d, keep=2)
        self.assertEqual(removed, 2)
        self.assertEqual(len(D.list_pitr(d)), 2)


class TestPromoteRollback(unittest.TestCase):
    def test_06_promote_success_and_rollback(self):
        d = tempfile.mkdtemp(prefix="keep-prom-")
        dump = os.path.join(d, "new.dump")
        with open(dump, "w") as fh:
            fh.write("-- consistent snapshot\nCREATE TABLE t(id int primary key, name text);\n"
                     "INSERT INTO t VALUES(1,'a'),(2,'b'),(3,'c');\n")
        applied = {"n": 0, "rolled": False}

        def apply_fn(path):
            applied["n"] += 1
            return True

        def rollback_fn():
            applied["rolled"] = True
            return True

        r = D.promote_with_rollback(dump, apply_fn, lambda: True,
                                    rollback_fn=rollback_fn, health_interval=0.01)
        self.assertEqual(r.action, "promoted", r.detail)
        self.assertTrue(r.health_ok)

        r = D.promote_with_rollback(dump, apply_fn, lambda: False,
                                    rollback_fn=rollback_fn,
                                    health_timeout=0.05, health_interval=0.01)
        self.assertEqual(r.action, "rolled_back", r.detail)
        self.assertTrue(applied["rolled"], "失败必须回滚")

    def test_07_promote_rejects_bad_dump_never_applies(self):
        d = tempfile.mkdtemp(prefix="keep-prom-")
        dump = os.path.join(d, "bad.dump")
        with open(dump, "w") as fh:
            fh.write("junk")
        called = {"n": 0}

        def apply_fn(path):
            called["n"] += 1
            return True

        r = D.promote_with_rollback(dump, apply_fn, lambda: True)
        self.assertEqual(r.action, "rejected")
        self.assertEqual(called["n"], 0, "坏转储绝不能触发 apply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
