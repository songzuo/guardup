#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GuardUp Keep —— 全栈热备编排 CLI（P2 原型）。

傻瓜化原则：用户只跑 3 个命令，其余全自动。
    keep.py init      # 生成示例 profile.json
    keep.py plan      # 自动发现：文档根 + 数据库路径，输出人话计划
    keep.py sync      # 一致性快照 + 校验 + 换库 + 健康检查 + 失败回滚
    keep.py watch     # 内容守卫，检测退化

设计上健康检查通过 --health-cmd 注入（编排层接真实 curl/systemctl），
原型默认用"库可打开"作为健康判据，测试时可注入故意失败的函数。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from profile import load_profiles, save_example, ServiceProfile  # noqa: E402
import discover as D  # noqa: E402
import gate as G  # noqa: E402
import watch as W  # noqa: E402

VERSION = "0.2.0-p2"


def cmd_init(args):
    target = args.profile
    if os.path.exists(target) and not args.force:
        print(f"配置已存在: {target}（--force 覆盖）")
        return 1
    save_example(target)
    print(f"✓ 已生成示例配置: {target}")
    print("  改成你的站点信息；docroots/db_path 留空会自动发现。")
    return 0


def cmd_plan(args):
    profiles = load_profiles(args.profile)
    print(f"发现计划（{len(profiles)} 个保护单元）：\n")
    for p in profiles:
        info = D.discover(p)
        roots = info.get("docroots") or ["（未发现，检查域名/nginx 配置）"]
        db = info.get("db_path") or "（未发现，静态档可忽略）"
        print(f"● {p.describe()}")
        print(f"    文档根   : {', '.join(roots)}")
        print(f"    数据库   : {db}")
        if p.business_tables:
            print(f"    业务表   : {', '.join(p.business_tables)}")
        print()
    return 0


def _default_health(db_path: str):
    """默认健康判据：库能以只读方式打开且 integrity_check=ok。"""
    def check():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            ok = con.execute("pragma integrity_check").fetchone()[0]
            con.close()
            return ok == "ok"
        except Exception:
            return False
    return check


def cmd_sync(args):
    profiles = load_profiles(args.profile)
    rc = 0
    for p in profiles:
        info = D.discover(p)
        db = info.get("db_path")
        roots = info.get("docroots") or []
        print(f"▶ {p.name} [{p.tier}]")
        if p.tier == "static" or not db:
            print("    静态档 / 无数据库：跳过 DB 换库（文件同步由 rsync 层负责）")
            continue
        # 源=发现到的主库；目标=本机的 live 库。原型里用 --db-src 覆盖源，
        # 真实部署时 src 是主站、live 是备站，中间用传输层搬运快照。
        src = args.db_src or db
        live = args.live_db or db
        if not os.path.exists(src):
            print(f"    ✗ 源库不存在: {src}")
            rc = 1
            continue
        snap = os.path.join(args.workdir, f"{p.name}.snapshot.db")
        try:
            G.snapshot(src, snap)
        except Exception as e:
            print(f"    ✗ 快照失败: {e}")
            rc = 1
            continue
        vr = G.verify(snap, p.business_tables)
        if not vr:
            print(f"    ✗ 校验失败，保留现库: {vr.reason}")
            rc = 1
            continue
        health = _default_health(live) if not args.health_fail else (lambda: False)
        # 演练回滚时不需要等满 timeout（健康检查恒失败），缩短到可感知的时长
        eff_timeout = 0.3 if args.health_fail else args.health_timeout
        res = G.swap_with_rollback(
            snap, live, p.business_tables, health,
            backup_dir=os.path.join(args.workdir, "backups"),
            health_timeout=eff_timeout,
            health_interval=0.1,
            dry_run=args.dry_run,
        )
        icon = {"unchanged": "=", "swapped": "✓", "rolled_back": "↩", "rejected": "✗"}[res.action]
        print(f"    {icon} {res.action}: {res.detail}")
        if res.action in ("rolled_back", "rejected"):
            rc = 1
    return rc


def cmd_watch(args):
    profiles = load_profiles(args.profile)
    rc = 0
    for p in profiles:
        info = D.discover(p)
        roots = info.get("docroots") or []
        for root in roots:
            gpath = os.path.join(args.workdir, f"{p.name}.golden.json")
            if args.save:
                W.save_golden(root, gpath, p.exclude)
                print(f"✓ {p.name}: 已保存 golden 快照 ({root})")
                continue
            if not os.path.exists(gpath):
                print(f"  {p.name}: 无 golden 快照，先跑 watch --save")
                continue
            res = W.check(root, gpath, p.exclude)
            print(f"  {'⚠' if res.degraded else '✓'} {p.name}: {res.reason} "
                  f"({res.golden_count} -> {res.current_count})")
            if res.degraded:
                rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="keep.py", description="GuardUp Keep 全栈热备编排（P2 原型）")
    ap.add_argument("--version", action="version", version=f"GuardUp Keep {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="生成示例配置")
    p_init.add_argument("--profile", default="profile.json")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(fn=cmd_init)

    p_plan = sub.add_parser("plan", help="自动发现并输出计划")
    p_plan.add_argument("--profile", default="profile.json")
    p_plan.set_defaults(fn=cmd_plan)

    p_sync = sub.add_parser("sync", help="一致性快照+校验+换库+回滚")
    p_sync.add_argument("--profile", default="profile.json")
    p_sync.add_argument("--db-src", default="", help="覆盖源库路径（测试用）")
    p_sync.add_argument("--live-db", default="", help="覆盖本机 live 库（测试用）")
    p_sync.add_argument("--workdir", default="./.guardup")
    p_sync.add_argument("--health-fail", action="store_true", help="强制健康检查失败（演练回滚）")
    p_sync.add_argument("--health-timeout", type=float, default=150)
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(fn=cmd_sync)

    p_watch = sub.add_parser("watch", help="内容守卫")
    p_watch.add_argument("--profile", default="profile.json")
    p_watch.add_argument("--workdir", default="./.guardup")
    p_watch.add_argument("--save", action="store_true", help="保存 golden 快照")
    p_watch.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    if not hasattr(args, "fn"):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
