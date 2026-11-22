#!/usr/bin/env python3
"""把 GuardUp 打包成单个可执行 Python 文件（零依赖分发）。

原理：读取模块化源码，去掉 import 语句，按依赖顺序内联合并成一个文件。
输出 guardup-standalone.py，用户 curl 下载后 python 直接跑。

运行: python tools/build_standalone.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "guardup-standalone.py")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def strip_imports(src):
    """移除模块间的 import（内联后不再需要）。"""
    lines = []
    for line in src.splitlines():
        s = line.strip()
        # 跳过 from .xxx import / import .xxx
        if s.startswith("from .") or s.startswith("from guardup"):
            continue
        if s.startswith("from providers import") or s.startswith("from engine import"):
            continue
        # __future__ 只能在文件头出现一次，合并时全部去掉
        if s.startswith("from __future__"):
            continue
        lines.append(line)
    return "\n".join(lines)


def main():
    provider_files = [
        "guardup/providers/cloudflare.py",
        "guardup/providers/aliyun.py",
        "guardup/providers/dnspod.py",
        "guardup/providers/tencentcloud.py",
    ]

    parts = []
    parts.append('''#!/usr/bin/env python3
"""GuardUp standalone — 零依赖单文件网站自动容灾 Agent。
由 tools/build_standalone.py 自动生成，请勿手改。
用法: python guardup-standalone.py init | status | check | drill | daemon
"""
from __future__ import annotations
import argparse, http.client, json, os, socket, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, List, Optional
''')

    # provider 类定义
    for pf in provider_files:
        parts.append("\n# ===== " + pf + " =====\n")
        parts.append(strip_imports(read(pf)))

    # provider 注册表（重写，去掉 import）
    parts.append('''
PROVIDERS = {
    "cloudflare": CloudflareProvider,
    "aliyun": AliyunProvider,
    "dnspod": DnsPodProvider,
    "tencentcloud": TencentCloudProvider,
}
def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v: return v.strip()
    return None
def _defaults_for(kind):
    if kind == "cloudflare":
        return {"token": _env("CF_API_TOKEN"), "api_key": _env("CF_API_KEY"), "email": _env("CF_API_EMAIL")}
    if kind == "aliyun":
        return {"access_key_id": _env("ALICLOUD_ACCESS_KEY_ID"), "access_key_secret": _env("ALICLOUD_ACCESS_KEY_SECRET")}
    if kind == "dnspod":
        return {"login_token": _env("DNSPOD_LOGIN_TOKEN")}
    if kind == "tencentcloud":
        return {"secret_id": _env("TENCENT_SECRET_ID", "TENCENTCLOUD_SECRET_ID"),
                "secret_key": _env("TENCENT_SECRET_KEY", "TENCENTCLOUD_SECRET_KEY"),
                "region": _env("TENCENT_REGION") or "ap-guangzhou"}
    return {}
def get_provider(kind, **kwargs):
    if kind not in PROVIDERS:
        raise ValueError("未知 provider: %s" % kind)
    params = _defaults_for(kind)
    params.update({k: v for k, v in kwargs.items() if v is not None})
    return PROVIDERS[kind](**params)
''')

    # 引擎（去掉 from .providers import get_provider）
    parts.append("\n# ===== engine =====\n")
    engine = read("guardup/engine.py")
    engine = engine.replace("from .providers import get_provider", "")
    engine = "\n".join(l for l in engine.splitlines() if not l.strip().startswith("from __future__"))
    engine = re.sub(r"^HERE = .*$", 'HERE = os.path.dirname(os.path.abspath(__file__))', engine, flags=re.M)
    engine = engine.replace("DEFAULT_CONFIG = _default_path(\"config.json\")", "DEFAULT_CONFIG = os.path.join(os.path.expanduser('~'), '.guardup', 'config.json')")
    engine = engine.replace("DEFAULT_STATE = _default_path(\"state.json\")", "DEFAULT_STATE = os.path.join(os.path.expanduser('~'), '.guardup', 'state.json')")
    parts.append(engine)

    # CLI（去掉对 guardup 包的 import）
    parts.append("\n# ===== CLI =====\n")
    cli = read("guardup.py")
    cli = "\n".join(l for l in cli.splitlines() if not l.strip().startswith("from __future__"))
    cli = cli.replace("from guardup.engine import Config, Failover, State, check_tcp, log", "")
    cli = cli.replace("try:\n    from guardup.providers import get_provider\nexcept ImportError:  # 兼容脚本目录直接运行\n    from providers import get_provider\n", "")
    parts.append(cli)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print("已生成:", OUT)
    print("行数:", sum(1 for _ in open(OUT, encoding="utf-8")))


if __name__ == "__main__":
    main()
