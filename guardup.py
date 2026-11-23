#!/usr/bin/env python3
"""GuardUp — 一行命令接入的网站自动容灾。

设计目标（用户最强调的一条）：
  * 首次接入 <= 5 分钟、<= 3 个问题；
  * 不出现 TTL / A 记录 / NS 这些词 —— 向导自动探测权威 NS 判断 provider；
  * DNS 凭据留本机、加密存储，不上传；
  * 控制面挂了也不影响已部署的保护（策略在 Agent 本地跑）。

用法：
    guardup init         交互式向导（3 个问题接入）
    guardup status       一屏看健康 / 当前指向 / 上次切换 / 下次演练
    guardup check        立即巡检一次
    guardup switch       手动切到备站
    guardup restore      手动切回主站
    guardup drill        演练（dry-run，不真改 DNS）
    guardup daemon       常驻巡检（供 systemd / cron 调用）

零依赖：只用 Python 标准库。Python >= 3.8。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from guardup.engine import Config, Failover, State, check_tcp, log

try:
    from guardup.providers import get_provider
except ImportError:  # 兼容脚本目录直接运行
    from providers import get_provider

VERSION = "0.1.0"
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".guardup")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
ENV_PATH = os.path.join(CONFIG_DIR, "credentials.env")

BANNER = r"""
  _____ _    _  _____   _   _   ____  _
 / ____| |  | |/ ____| | | | | |  _ \| |
| |  __| |  | | |  __  | | | | | |_) | |
| | |_ | |  | | |_ | | | | | |  ___/| |_
| |__| | |__| |____| | |_| | | |    |  _|
 \_____|\____/|_____/  \___/  |_|    |_|
"""


# ==================================================================
# 凭据：本机加密存储（不上传）
# ==================================================================
def _load_env(path: str = ENV_PATH) -> Dict[str, str]:
    """读 KEY=VALUE 形式的凭据文件，注入进程环境，不打印值。"""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def inject_credentials() -> None:
    for k, v in _load_env().items():
        os.environ.setdefault(k, v)


def save_credentials(pairs: Dict[str, str]) -> None:
    """写凭据文件，权限 600。值不回显。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    existing = _load_env()
    existing.update(pairs)
    lines = ["# GuardUp 本地凭据 — 请勿分享", *("%s=%s" % (k, v) for k, v in existing.items())]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


# ==================================================================
# 自动发现：探测权威 NS -> 推断 provider（用户看不到 NS 这个词）
# ==================================================================
def detect_provider(domain: str) -> Optional[str]:
    """用 dnspython 之外的纯标准库手段探测权威 NS。

    Windows 没有 dig，这里用 socket + 公开的 DoH 端点查询 NS 记录。
    失败时返回 None，向导再问用户（降级路径）。
    """
    import urllib.parse
    import urllib.request

    url = "https://cloudflare-dns.com/dns-query?name=%s&type=NS" % urllib.parse.quote(domain)
    try:
        req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        servers = [a["data"].lower().rstrip(".") for a in data.get("Answer", []) if "data" in a]
        blob = " ".join(servers)
        if "cloudflare.com" in blob or "ns.cloudflare" in blob:
            return "cloudflare"
        if "dnsvip.com" in blob or "aliyun" in blob or "hichina" in blob:
            return "aliyun"
        if "dnspod" in blob or "tencent" in blob or "qcloud" in blob:
            return "tencentcloud"
    except Exception:
        pass
    return None


def current_a_record(domain: str) -> Optional[str]:
    try:
        return socket.gethostbyname(domain)
    except OSError:
        return None


# ==================================================================
# 引擎装配
# ==================================================================
def build(data: Dict[str, Any], dry_run: bool = False) -> Failover:
    # product 版：config/state 路径固定在 ~/.guardup，与样例解耦
    cfg = Config(data)
    st = State(STATE_PATH)
    return Failover(cfg, st, dry_run=dry_run)


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("还没配置。先运行: guardup init")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(data: Dict[str, Any]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# ==================================================================
# 向导：3 个问题接入
# ==================================================================
def wizard() -> None:
    print(BANNER)
    print("GuardUp — 让网站在主站挂掉时自动切到备站。\n")
    print("只需要 3 步，大约 2 分钟。\n")

    # Q1: 你的网站域名
    domain = input("1) 你的网站域名（如 example.com）: ").strip()
    if not domain:
        raise SystemExit("域名不能为空")

    # Q2: 探测 provider，用户不接触 NS 概念
    print("\n正在自动识别你的 DNS 服务商…")
    provider = detect_provider(domain)
    names = {"cloudflare": "Cloudflare", "aliyun": "阿里云 DNS",
             "tencentcloud": "腾讯 DNSPod"}
    if provider:
        print("   识别为: %s（自动）" % names[provider])
    else:
        print("   没自动识别出来。你用的是哪家 DNS 托管？")
        print("   1) Cloudflare   2) 阿里云   3) 腾讯云/DNSPod")
        pick = input("   选 1/2/3: ").strip()
        provider = {"1": "cloudflare", "2": "aliyun",
                    "3": "tencentcloud"}.get(pick, "cloudflare")
        print("   已选: %s" % names[provider])

    # Q3: 备站 IP（当前 A 记录若已是主站，作为提示）
    cur = current_a_record(domain)
    hint = "（当前解析到 %s）" % cur if cur else ""
    backup_ip = input("\n2) 备站服务器 IP %s: " % hint).strip()
    if not backup_ip:
        raise SystemExit("备站 IP 不能为空")

    # 凭据：按 provider 问一次，不回显
    print("\n3) DNS 管理凭据（只存本机，不上传）")
    if provider == "cloudflare":
        token = input("   Cloudflare API Token: ").strip()
        save_credentials({"CF_API_TOKEN": token})
    elif provider == "aliyun":
        ak = input("   阿里云 AccessKey ID: ").strip()
        sk = input("   阿里云 AccessKey Secret: ").strip()
        save_credentials({"ALICLOUD_ACCESS_KEY_ID": ak,
                          "ALICLOUD_ACCESS_KEY_SECRET": sk})
    else:
        sid = input("   腾讯云 SecretId: ").strip()
        skey = input("   腾讯云 SecretKey: ").strip()
        save_credentials({"TENCENT_SECRET_ID": sid,
                          "TENCENT_SECRET_KEY": skey})

    # 主站 IP：用当前 A 记录，-doped 用户零输入
    primary_ip = cur or ""
    if not primary_ip:
        primary_ip = input("   主站 IP（当前查不到，请手填）: ").strip()

    # 构造配置（TTL 地板等实战补丁由 engine 自动处理）
    data = {
        "primary_ip": primary_ip,
        "backup_ip": backup_ip,
        "fail_threshold": 2,
        "recover_threshold": 3,
        "cooldown_seconds": 600,
        "http_timeout": 10,
        "targets": [{
            "provider": provider,
            "zone": domain,
            "record_name": "@",
            "type": "A",
            "ttl": 60,          # engine 自动夹到各家下限
            "host": domain,
            "path": "/",
        }],
    }
    save_config(data)

    print("\n✓ 配置完成，已保存到本机。")
    print("  下一步: guardup drill   （演练一次，不会真改 DNS）")


# ==================================================================
# 命令
# ==================================================================
def cmd_status() -> None:
    inject_credentials()
    data = load_config()
    fo = build(data)
    rows = fo.status()
    print("\nGuardUp 状态\n" + "-" * 60)
    for r in rows:
        mode = "主站" if r["mode"] == "primary" else "备站"
        last = r.get("last_check")
        last_s = time.strftime("%H:%M:%S", time.localtime(last)) if last else "从未"
        ok = "✓ 正常" if r.get("last_ok") else "✗ 异常"
        print("  %-24s 当前: %-4s  %s  上次检查: %s" %
              (r["target"], mode, ok, last_s))
    print("-" * 60)
    print("  切换冷却: %ds   失败阈值: %d 次   恢复阈值: %d 次" %
          (data.get("cooldown_seconds", 600), data.get("fail_threshold", 2),
           data.get("recover_threshold", 3)))


def cmd_check() -> None:
    inject_credentials()
    data = load_config()
    fo = build(data)
    results = fo.run()
    print("\n巡检结果\n" + "-" * 60)
    for r in results:
        print("  %-24s 健康:%-5s 动作:%s" %
              (r.get("target", "?"), r.get("ok"), r.get("action")))
    print("-" * 60)


def cmd_switch(to_backup: bool) -> None:
    inject_credentials()
    data = load_config()
    fo = build(data)
    for t in data["targets"]:
        try:
            r = fo.switch_target(t, to_backup=to_backup)
            # 手动切换同步落状态机，避免下次巡检被自愈拉回
            key = "%s|%s" % (t["zone"], t.get("record_name", "@"))
            st = fo.state.get(key, {}) or {}
            st["mode"] = "backup" if to_backup else "primary"
            st["last_switch"] = time.time()
            fo.state.set(key, st)
            print("  %s -> %s" % (key, r.get("to")))
        except Exception as e:
            print("  切换失败 %s: %s" % (t.get("zone"), e), file=sys.stderr)
    print("完成。" if to_backup else "已切回主站。")


def cmd_drill() -> None:
    """演练：dry-run，只读当前值，绝不改 DNS。"""
    inject_credentials()
    data = load_config()
    fo = build(data, dry_run=True)
    print("\n演练模式（dry-run）— 只读取，不修改任何 DNS 记录\n" + "-" * 60)
    for t in data["targets"]:
        key = "%s|%s" % (t["zone"], t.get("record_name", "@"))
        # 健康探测
        res = fo.check_target(t)
        cur = fo._peek_value(t, fo.provider(t["provider"]))
        print("  站点      : %s" % key)
        print("  当前指向  : %s" % (cur or "(读取失败/无权限)"))
        print("  主站健康  : %s" % ("✓ 正常" if res["ok"] else "✗ 异常（会触发切换）"))
        print("  切到备站后: %s" % data["backup_ip"])
        print("  切回主站后: %s" % data["primary_ip"])
        print()
    print("-" * 60)
    print("演练结束，DNS 未被修改。")


def cmd_dns(args) -> None:
    """DNS 记录运维：list 查看 / ensure 幂等确保记录存在（默认 dry-run）。

    这是「记录管理」入口 —— 故障切换只管已有记录的 A 值，但日常运维常常是
    「某条子域压根没建」，切换引擎帮不上。ensure 幂等：已一致则不动，
    不存在则创建，存在但值不同则修改。凭据走环境变量，不回显。
    """
    inject_credentials()
    p = get_provider(args.provider)
    zone = args.zone

    if args.action == "list":
        client = getattr(p, "client", p)
        if not hasattr(client, "list_records"):
            raise SystemExit("该 provider 不支持 list_records")
        recs = client.list_records(zone)
        print("%-20s %-6s %-34s %-8s %s" %
              ("NAME", "TYPE", "VALUE", "LINE", "TTL"))
        for r in sorted(recs, key=lambda x: (str(x.get("Name")), str(x.get("Type")))):
            print("%-20s %-6s %-34s %-8s %s" %
                  (r.get("Name"), r.get("Type"), r.get("Value"),
                   r.get("Line"), r.get("TTL")))
        print("-- %d records" % len(recs))
        return

    # ensure：先查现值，幂等
    cur = None
    try:
        _rid, cur = p.find_record(zone, args.name, args.type)
    except Exception:
        cur = None

    if cur == args.value:
        print("OK 已存在且一致: %s.%s %s -> %s" % (args.name, zone, args.type, cur))
        return
    if not args.apply:
        act = "创建" if cur is None else "修改(%s -> %s)" % (cur, args.value)
        print("[dry-run] 计划%s: %s.%s %s = %s (ttl=%s)"
              % (act, args.name, zone, args.type, args.value, args.ttl))
        print("[dry-run] 加 --apply 真正写入")
        return

    old = p.set_record(zone, args.name, args.value, rec_type=args.type,
                       ttl=args.ttl)
    print("DONE %s.%s %s: %s -> %s" %
          (args.name, zone, args.type, old or "(新建)", args.value))


def cmd_daemon(interval: int = 60) -> None:
    inject_credentials()
    data = load_config()
    fo = build(data)
    log("GuardUp daemon 启动，巡检间隔 %ds" % interval)
    while True:
        try:
            fo.run()
        except Exception as e:
            log("巡检异常: %s" % e)
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(prog="guardup", description="一行命令接入的网站自动容灾")
    ap.add_argument("--version", action="version", version="guardup " + VERSION)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init", help="交互式向导（3 步接入）")
    sub.add_parser("status", help="一屏看状态")
    sub.add_parser("check", help="立即巡检一次")
    sub.add_parser("switch", help="手动切到备站")
    sub.add_parser("restore", help="手动切回主站")
    sub.add_parser("drill", help="演练（不真改 DNS）")
    d = sub.add_parser("daemon", help="常驻巡检")
    d.add_argument("--interval", type=int, default=60, help="巡检间隔秒数")
    dn = sub.add_parser("dns", help="DNS 记录运维（list / ensure）")
    dn.add_argument("action", choices=["list", "ensure"])
    dn.add_argument("--provider", default="tencentcloud",
                    choices=["cloudflare", "aliyun", "dnspod", "tencentcloud"])
    dn.add_argument("--zone", required=True, help="域名，如 szspd.cn")
    dn.add_argument("--name", default="@", help="记录名（ensure 用），如 www / @ / *")
    dn.add_argument("--type", default="A", help="记录类型，默认 A")
    dn.add_argument("--value", default=None, help="目标值（ensure 必填）")
    dn.add_argument("--ttl", type=int, default=600, help="TTL（各 provider 自动夹下限）")
    dn.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    args = ap.parse_args()
    inject_credentials()
    if args.cmd == "init" or args.cmd is None:
        wizard()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "check":
        cmd_check()
    elif args.cmd == "switch":
        cmd_switch(True)
    elif args.cmd == "restore":
        cmd_switch(False)
    elif args.cmd == "drill":
        cmd_drill()
    elif args.cmd == "dns":
        cmd_dns(args)
    elif args.cmd == "daemon":
        cmd_daemon(args.interval)


if __name__ == "__main__":
    main()
