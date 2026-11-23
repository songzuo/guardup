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
import db as DB  # noqa: E402
import quorum as Q  # noqa: E402
import checkout as C  # noqa: E402

VERSION = "0.3.0-p4"


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


def cmd_db(args):
    """PG/MySQL 真副本：延迟闸门 + PITR 目录管理（不碰真实 DB，命令注入）。"""
    if args.db_action == "gate":
        r = DB.lag_gate(args.lag, args.max_lag, force=args.force)
        print(f"{'✓ 放行' if r.allowed else '✗ 暂缓'}: {r.reason}")
        return 0 if r.allowed else 2
    if args.db_action == "pitr":
        snaps = DB.list_pitr(args.dir)
        if args.prune is not None:
            n = DB.prune_pitr(args.dir, keep=args.prune)
            print(f"已清理 {n} 份旧快照，保留最近 {args.prune} 份")
        elif args.point is not None:
            s = DB.pick_for_point(args.dir, args.point)
            print(f"恢复点 <= {args.point}: {s.path if s else '（无匹配快照）'}")
        else:
            if not snaps:
                print("（PITR 目录为空）")
            for s in snaps:
                print(f"  {s.created_at:.0f}  {s.kind:8}  {s.size}B  {s.path}")
        return 0
    return 1


def cmd_quorum(args):
    """多地投票演示：6 探测点 + 连续确认 + 冷却，纯逻辑无需真 bot。"""
    ps = [
        Q.Probe("bot-sz", "cn-east", False, ts=100),
        Q.Probe("bot-bj", "cn-north", False, ts=100),
        Q.Probe("bot-hk", "ap-hk", False, ts=100),
        Q.Probe("bot-sg", "ap-sg", False, ts=100),
        Q.Probe("bot-us", "us-west", True, ts=100),
        Q.Probe("bot-de", "eu-central", True, ts=100),
    ]
    st = Q.VoteState()
    print("模拟：4/6 探测点判死（跨 4 地域），主站疑似故障\n")
    for t in (100, 101):
        r = Q.vote(ps, st, now=t)
        print(f"  第 {t-99} 轮: {r.decision:11} — {r.reason}")
    # 租约 fencing 演示
    store = Q.MemoryStore()
    lease = Q.Lease(store, ttl=30, now_fn=lambda: 0.0)
    a = lease.acquire("node-a")
    b = lease.acquire("node-b")
    print(f"\n控制面租约: node-a 争主={'成功' if a['ok'] else '失败'}(epoch={a['epoch']}), "
          f"node-b 争主={'成功' if b['ok'] else '失败'}(fencing 防脑裂)")
    return 0


def cmd_checkout(args):
    """收款适配层：打印三档报价 + 主备双通道结算链接（不建单、不碰钱）。"""
    print("GuardUp Keep 定价（USD，RTO 是唯一价格阶梯）：\n")
    for p in C.list_plans():
        q = C.quote(p["key"], with_onboarding=(p["key"] == "guard"))
        print(f"  {p['name']:9} ${p['price_monthly']:>3}/月  — {p['tagline']}")
        if q["onboarding_usd"]:
            print(f"            含 ${q['onboarding_usd']} 一次性接入费（抵首月）")
    if args.plan:
        d = C.dual_checkout(args.plan, email=args.email)
        print(f"\n{args.plan} 双通道结算链接（Creem 主 / Dodo 备，一键切换防封）：")
        print(f"  主: {d['primary']['url']}")
        print(f"  备: {d['fallback']['url']}")
        if "PLACEHOLDER" in d["primary"]["url"]:
            print("  提示: 未配置 product id，设环境变量 GUARDUP_CREEM_PRODUCT_<PLAN> / "
                  "GUARDUP_DODO_PRODUCT_<PLAN> 后生成真实链接。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="keep.py", description="GuardUp Keep 全栈热备编排（P4）")
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

    p_db = sub.add_parser("db", help="PG/MySQL 真副本：延迟闸门 + PITR 管理")
    p_db.add_argument("db_action", choices=["gate", "pitr"])
    p_db.add_argument("--lag", type=float, default=5.0, help="gate: 当前复制延迟秒数")
    p_db.add_argument("--max-lag", type=float, default=30.0, help="gate: 允许的最大延迟")
    p_db.add_argument("--force", action="store_true", help="gate: 演练强制放行")
    p_db.add_argument("--dir", default="./.guardup/pitr", help="pitr: 快照目录")
    p_db.add_argument("--prune", type=int, default=None, help="pitr: 保留最近 N 份")
    p_db.add_argument("--point", type=float, default=None, help="pitr: 查 <= 时间点的恢复快照")
    p_db.set_defaults(fn=cmd_db)

    p_q = sub.add_parser("quorum", help="多地投票 + 租约 fencing 演示")
    p_q.set_defaults(fn=cmd_quorum)

    p_co = sub.add_parser("checkout", help="三档报价 + 主备双通道结算链接")
    p_co.add_argument("--plan", choices=list(C.PLANS), default=None, help="显示该档结算链接")
    p_co.add_argument("--email", default=None, help="预填邮箱（不存储）")
    p_co.set_defaults(fn=cmd_checkout)

    args = ap.parse_args(argv)
    if not hasattr(args, "fn"):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
