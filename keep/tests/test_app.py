#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App Takeover 单元测试 —— 假进程 + 假健康检查，快速可重复。

真 node 进程的端到端 dogfooding 见 DOGFOODING.md；这里用注入件覆盖分支逻辑，
不依赖 node 运行时，CI 可跑。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as A  # noqa: E402


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False
    def poll(self):
        return None if self._alive else 1
    def terminate(self):
        self.terminated = True
        self._alive = False
    def kill(self):
        self._alive = False
    def wait(self, timeout=None):
        return 0


class TestApp(unittest.TestCase):
    def make_version(self, name):
        d = tempfile.mkdtemp(prefix="app-ut-")
        vd = os.path.join(d, name)
        os.makedirs(os.path.join(vd, ".next", "static"))
        os.makedirs(os.path.join(vd, "public"))
        with open(os.path.join(vd, "server.js"), "w") as f:
            f.write("// mock next server")
        with open(os.path.join(vd, ".next", "static", "a.js"), "w") as f:
            f.write("x")
        return d, vd

    def test_01_detect_standalone(self):
        _, vd = self.make_version("v1")
        info = A.detect_standalone(vd)
        self.assertTrue(info["is_standalone"])
        self.assertTrue(info["server_js"].endswith("server.js"))
        self.assertTrue(info.get("static") and info.get("public"))

    def test_02_fingerprint_changes(self):
        _, vd = self.make_version("v1")
        fp1 = A.artifact_fingerprint(vd)
        with open(os.path.join(vd, "server.js"), "a") as f:
            f.write("\n// changed")
        self.assertNotEqual(fp1, A.artifact_fingerprint(vd))

    def test_03_sync_artifact(self):
        d, src = self.make_version("v1")
        info = A.detect_standalone(src)
        target = os.path.join(d, "versions", "v1")
        A.sync_artifact(src, target, info.get("static"), info.get("public"))
        self.assertTrue(os.path.exists(os.path.join(target, "server.js")))
        self.assertTrue(os.path.exists(os.path.join(target, ".next", "static", "a.js")))
        self.assertTrue(os.path.exists(os.path.join(target, ".guardup-artifact.json")))

    def test_04_takeover_healthy(self):
        _, vd = self.make_version("v1")
        procs = []
        def factory(srv, env):
            p = FakeProc(alive=True); procs.append(p); return p
        r = A.takeover(vd, health_timeout=2, health_interval=0.01,
                       proc_factory=factory, health_fn=lambda: True)
        self.assertEqual(r.action, "started")
        self.assertTrue(r.healthy)

    def test_05_takeover_dry_run_no_proc(self):
        _, vd = self.make_version("v1")
        called = []
        r = A.takeover(vd, dry_run=True, proc_factory=lambda s, e: called.append(1))
        self.assertEqual(r.action, "dry_run")
        self.assertEqual(called, [], "dry-run 绝不能起进程")

    def test_06_rollback_uses_same_port(self):
        """回归：回滚必须沿用同端口，否则反代仍指向坏版本。"""
        _, good = self.make_version("good")
        _, bad = self.make_version("bad")
        procs = []
        # 坏版本(server.js 路径含 bad)进程立刻崩溃；好版本活着
        def factory(srv, env):
            p = FakeProc(alive=("bad" not in srv)); procs.append(p); return p
        r = A.takeover(bad, port=3456, health_timeout=2, health_interval=0.01,
                       rollback_to=good, proc_factory=factory, health_fn=lambda: True)
        self.assertEqual(r.action, "rolled_back", r.detail)
        self.assertEqual(r.port, 3456, "回滚端口必须等于原端口")
        self.assertTrue(r.healthy)
        self.assertGreaterEqual(len(procs), 2, "应起了坏版本+回滚版本两个进程")

    def test_07_switch_symlink(self):
        d = tempfile.mkdtemp(prefix="app-link-")
        v1 = os.path.join(d, "v1"); os.makedirs(v1)
        v2 = os.path.join(d, "v2"); os.makedirs(v2)
        link = os.path.join(d, "current")
        A.switch_symlink(link, v1)
        self.assertEqual(os.path.realpath(link), os.path.realpath(v1))
        A.switch_symlink(link, v2)
        self.assertEqual(os.path.realpath(link), os.path.realpath(v2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
