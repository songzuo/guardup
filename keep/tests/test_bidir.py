#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bidir.py 自测：真 SQLite + 真触发器，验证灾备回切的反向增量合并。

覆盖用户场景：主站挂→副站接管产生新数据→主站恢复→副站增量反向合并回主站。
  1. 安装追踪 + 触发器捕获 I/U/D（含删除墓碑）
  2. 副站独有新增 → 反向同步到主站
  3. 副站独有更新 → 反向同步到主站
  4. 两边都改且不同 → 冲突：remote_wins / local_wins / manual 阻断
  5. 删除墓碑同步，且不会被旧 base 复活
  6. changeset 导出/导入跨机桥
  7. 幂等：合并结果不重复应用
  8. 端到端：base 分叉点 → 两边各自演进 → 合并结果库内容正确
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bidir as B  # noqa: E402


def make_db(path, rows=()):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("create table if not exists orders(id integer primary key, name text, qty int)")
    con.execute("delete from orders")
    for pk, name, qty in rows:
        con.execute("insert into orders(id,name,qty) values(?,?,?)", (pk, name, qty))
    con.commit(); con.close()


class TestTracking(unittest.TestCase):
    def test_01_trigger_captures_iud(self):
        d = tempfile.mkdtemp(prefix="bi-track-")
        db = os.path.join(d, "db.sqlite")
        make_db(db, [(1, "a", 1)])
        B.install_tracking(db, "orders", origin="standby")
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("insert into orders values(2,'b',2)")       # I
        con.execute("update orders set qty=9 where id=1")        # U
        con.execute("delete from orders where id=2")             # D
        con.commit(); con.close()
        ch = B.read_changes(db, "orders")
        self.assertEqual(ch["1"]["op"], "U")
        self.assertEqual(ch["2"]["op"], "D", "删除必须留墓碑")
        self.assertEqual(ch["2"]["origin"], "standby")


class TestMerge(unittest.TestCase):
    def test_02_remote_only_changes_sync_back(self):
        base = {"1": {"id": 1, "name": "a", "qty": 1},
                "2": {"id": 2, "name": "b", "qty": 2}}
        local = dict(base)                                   # 主站没动
        remote = {"1": {"id": 1, "name": "a", "qty": 1},
                  "2": {"id": 2, "name": "b", "qty": 2},
                  "3": {"id": 3, "name": "new-order", "qty": 7}}  # 副站新增
        remote["1"] = {"id": 1, "name": "a", "qty": 5}       # 副站改了 qty
        res = B.merge(base, local, remote,
                      remote_changes={"3": {"ts": 2.0, "op": "I"},
                                      "1": {"ts": 2.0, "op": "U"}})
        acts = {c.pk: c.action for c in res.applied}
        self.assertEqual(acts.get("3"), "I", "副站新增订单要反向同步")
        self.assertEqual(acts.get("1"), "U", "副站更新要反向同步")
        self.assertEqual(len(res.conflicts), 0)

    def test_03_conflict_strategies(self):
        base = {"1": {"id": 1, "name": "a", "qty": 1}}
        local = {"1": {"id": 1, "name": "a", "qty": 100}}     # 主改成 100
        remote = {"1": {"id": 1, "name": "a", "qty": 5}}      # 副改成 5
        lc = {"1": {"ts": 2.0, "op": "U"}}
        rc = {"1": {"ts": 3.0, "op": "U"}}

        rw = B.merge(base, local, remote, local_changes=lc, remote_changes=rc,
                     strategy="remote_wins")
        self.assertEqual(rw.applied[0].remote_row["qty"], 5, "默认副站优先")
        self.assertEqual(len(rw.conflicts), 1, "冲突要留痕")

        lw = B.merge(base, local, remote, local_changes=lc, remote_changes=rc,
                     strategy="local_wins")
        self.assertEqual(len(lw.applied), 0, "local_wins 时主站保持原样")
        self.assertEqual(len(lw.conflicts), 1)

        man = B.merge(base, local, remote, local_changes=lc, remote_changes=rc,
                      strategy="manual")
        self.assertEqual(len(man.applied), 0, "manual 必须阻断")
        self.assertEqual(len(man.conflicts), 1)

    def test_04_delete_tombstone_no_resurrect(self):
        base = {"1": {"id": 1, "name": "a", "qty": 1},
                "2": {"id": 2, "name": "b", "qty": 2}}
        local = dict(base)
        remote = {"1": {"id": 1, "name": "a", "qty": 1}}       # 副站删了 id=2
        res = B.merge(base, local, remote,
                      remote_changes={"2": {"ts": 2.0, "op": "D"}})
        acts = {c.pk: c.action for c in res.applied}
        self.assertEqual(acts.get("2"), "D", "删除要作为墓碑同步")
        # 关键：base 里还有 id=2，但绝不能因为"base 有"就把它复活

    def test_05_local_only_change_kept(self):
        base = {"1": {"id": 1, "name": "a", "qty": 1}}
        local = {"1": {"id": 1, "name": "a", "qty": 9}}        # 主站恢复后自己改了
        remote = dict(base)
        res = B.merge(base, local, remote, local_changes={"1": {"ts": 2.0, "op": "U"}})
        self.assertEqual(len(res.applied), 0, "只有主站变，不动")
        self.assertIn("只有主站变", res.skipped[0].reason)


class TestBridge(unittest.TestCase):
    def test_06_changeset_export_import(self):
        d = tempfile.mkdtemp(prefix="bi-bridge-")
        standby = os.path.join(d, "standby.db")
        main = os.path.join(d, "main.db")
        make_db(standby, [(1, "a", 1)])
        make_db(main, [(1, "a", 1)])
        B.install_tracking(standby, "orders", origin="standby")
        import sqlite3
        con = sqlite3.connect(standby)
        con.execute("insert into orders values(2,'new',3)")
        con.commit(); con.close()
        cs = B.export_changeset(standby, "orders")
        self.assertIn("2", cs["changes"])
        self.assertEqual(cs["changes"]["2"]["row"]["name"], "new")
        n = B.import_changeset(cs, main, "orders")
        self.assertEqual(n, 1, "变更集要能导入主站影子区")

    def test_07b_reconstruct_no_false_delete(self):
        """回归：变更集只含变更行时，未变更的行不能被误判为删除。"""
        base = {"1": {"id": 1, "qty": 1}, "2": {"id": 2, "qty": 2},
                "3": {"id": 3, "qty": 3}}
        changes = {
            "1": {"ts": 2.0, "op": "U", "row": {"id": 1, "qty": 9}},   # 改了
            "3": {"ts": 2.0, "op": "D", "row": None},                   # 删了
            # id=2 没变更 → 不在 changes 里
        }
        remote = B.reconstruct_rows(base, changes)
        self.assertEqual(remote["1"]["qty"], 9)
        self.assertEqual(remote["2"]["qty"], 2, "未变更的 id=2 必须保留")
        self.assertNotIn("3", remote, "op=D 的行要删掉")
        # 用重建的完整行集做合并，id=2 不应出现在 applied（不被误删）
        res = B.merge(base, base, remote,
                      remote_changes={k: {"ts": v["ts"], "op": v["op"]}
                                      for k, v in changes.items()})
        acts = {c.pk: c.action for c in res.applied}
        self.assertNotIn("2", acts, "id=2 未被副站改动，绝不能反向删除")

    def test_07_idempotent_merge(self):
        base = {"1": {"id": 1, "name": "a", "qty": 1}}
        local = {"1": {"id": 1, "name": "a", "qty": 1},
                 "2": {"id": 2, "name": "new", "qty": 3}}
        # 合并已经应用过：现在 local 就是结果，再跑一次应无动作
        res = B.merge(local, local, local)
        self.assertEqual(len(res.applied), 0, "幂等：不重复应用")


class TestEndToEnd(unittest.TestCase):
    def test_08_full_failback_merge_into_result_db(self):
        d = tempfile.mkdtemp(prefix="bi-e2e-")
        base_db = os.path.join(d, "base.db")     # 故障切换那一刻的共同快照
        main_db = os.path.join(d, "main.db")     # 主站（恢复后，旧数据）
        sb_db = os.path.join(d, "standby.db")    # 副站（故障期产生新数据）
        out_db = os.path.join(d, "merged.db")    # 合并结果库（要换回主站）
        for p in (base_db, main_db, sb_db, out_db):
            make_db(p, [(1, "orig-a", 1), (2, "orig-b", 2)])

        # 副站在故障期：新增 order 3、改 order 1、删 order 2
        import sqlite3
        B.install_tracking(sb_db, "orders", origin="standby")
        con = sqlite3.connect(sb_db)
        con.execute("insert into orders values(3,'new-c',5)")
        con.execute("update orders set qty=8 where id=1")
        con.execute("delete from orders where id=2")
        con.commit(); con.close()

        base_rows = B.read_rows(base_db, "orders")
        local_rows = B.read_rows(main_db, "orders")
        remote_rows = B.read_rows(sb_db, "orders")
        rchg = B.read_changes(sb_db, "orders")

        res = B.merge(base_rows, local_rows, remote_rows, remote_changes=rchg)
        n = B.apply_merge(out_db, "orders", res)
        self.assertGreater(n, 0)

        final = B.read_rows(out_db, "orders")
        self.assertEqual(final["3"]["name"], "new-c", "副站新增订单进主站")
        self.assertEqual(final["1"]["qty"], 8, "副站更新进主站")
        self.assertNotIn("2", final, "副站删除 applied 到主站，且不复活")
        print("\n  E2E 合并结果:", {k: v["qty"] for k, v in final.items()},
              "|", res.summary())


if __name__ == "__main__":
    unittest.main(verbosity=2)
