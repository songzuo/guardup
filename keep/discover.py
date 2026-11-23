#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover —— 自动发现站点文档根与数据库路径（Sync Planner 的感知层）。

脱胎于 G:/v/_failover_live/discover_roots.py，保留它的两条血泪修正：
  1. 只认 server 级 root（大括号深度 0），不取 location 内的 root；
  2. 排除 ACME 验证目录（.well-known / acme-challenge）这种"假根"。
     —— 曾因取错根把 mainlander.cn 同步成 2988 字节占位页（真站 11 个 q-* 区块）。

新增：自动发现 SQLite 数据库（扫描常见路径 + 按业务表名匹配），
让用户连 db_path 都不用填。
"""
from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Tuple

CONF_GLOBS = [
    "/etc/nginx/nginx.conf",
    "/etc/nginx/conf.d/*.conf",
    "/etc/nginx/sites-enabled/*",
    "/etc/nginx/conf.d/*.conf.*",
]

ROOT_RE = re.compile(r"\broot\s+([^;]+);")
# 数据库常见藏身路径（按优先级）
DB_GLOBS = [
    "/opt/*/*.db", "/opt/*/data/*.db", "/var/lib/*/*.db",
    "/opt/*/*.sqlite3", "/srv/*/*.db", "/data/*/*.db",
]
DB_SKIP = ("apigw.bak", ".bak", "test", "fixture")


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        in_s = in_d = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def server_level_roots(block: str) -> List[Tuple[str, bool, bool]]:
    """返回 [(root, 是否 server 级, 是否 ACME 假根)]，按出现顺序。"""
    roots = []
    depth, i, n = 0, 0, len(block)
    while i < n:
        ch = block[i]
        if ch == "{":
            depth += 1; i += 1; continue
        if ch == "}":
            depth -= 1; i += 1; continue
        m = ROOT_RE.match(block, i)
        if m:
            val = m.group(1).strip()
            if val:
                ctx = block[max(0, i - 300):i]
                is_acme = (".well-known" in ctx) or ("acme-challenge" in ctx)
                roots.append((val, depth == 0, is_acme))
            i = m.end(); continue
        i += 1
    return roots


def _pick_roots(roots):
    top = [r for r, is_top, _ in roots if is_top]
    if top:
        return top
    return [r for r, _, is_acme in roots if not is_acme]


def parse_nginx(paths: List[str]) -> List[Tuple[str, List[str]]]:
    """解析 nginx 配置 → [(server_name, [roots...])]。"""
    result = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = strip_comments(raw)
        for m in re.finditer(r"\bserver\s*\{", text):
            start = m.end(); depth = 1; i = start
            while i < len(text) and depth > 0:
                if text[i] == "{": depth += 1
                elif text[i] == "}": depth -= 1
                i += 1
            block = text[start:i - 1]
            names = re.findall(r"\bserver_name\s+([^;]+);", block)
            roots = _pick_roots(server_level_roots(block))
            if not roots:
                continue
            for nline in names:
                for name in nline.split():
                    name = name.strip()
                    if name and name != "_" and not name.startswith(("~", "$")):
                        result.append((name, roots))
    return result


def discover_docroots(domains: List[str], conf_globs=None, root_dir: str = "") -> Dict[str, List[str]]:
    """给域名列表，返回每个域名存在的文档根候选（绝对路径、去重、按存在性排序）。

    root_dir 用于测试时把 /etc/nginx 重定向到临时目录。
    """
    patterns = conf_globs or CONF_GLOBS
    if root_dir:
        patterns = [os.path.join(root_dir, p.lstrip("/")) for p in patterns]
    paths = []
    for pat in patterns:
        paths.extend(sorted(glob.glob(pat)))
    parsed = parse_nginx(paths)
    wanted = set(domains)
    found: Dict[str, List[str]] = {}
    for name, roots in parsed:
        if name not in wanted:
            continue
        for root in roots:
            real = os.path.realpath(root)
            if real not in found.setdefault(name, []):
                found[name].append(real)
    # 存在的排前面（调用方取第一个）
    for name in found:
        found[name].sort(key=lambda p: (not os.path.isdir(p), p))
    return found


def discover_sqlite_db(business_tables: List[str] = None, scan_dirs: List[str] = None) -> str:
    """扫描常见路径，找第一个带业务表且 integrity_check=ok 的 SQLite 库。"""
    import sqlite3
    candidates = []
    for pat in (scan_dirs or DB_GLOBS):
        candidates.extend(glob.glob(pat))
    business_tables = business_tables or []
    for path in candidates:
        if any(skip in os.path.basename(path) for skip in DB_SKIP):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            ok = con.execute("pragma integrity_check").fetchone()[0]
            if ok == "ok":
                if business_tables:
                    tables = {r[0] for r in con.execute(
                        "select name from sqlite_master where type='table'")}
                    if not set(business_tables).issubset(tables):
                        con.close(); continue
                con.close()
                return os.path.realpath(path)
            con.close()
        except Exception:
            continue
    return ""


def discover(profile) -> dict:
    """对一个 ServiceProfile 做完整发现，返回补全后的字段（不修改原对象）。"""
    out = profile.to_dict()
    if not out.get("docroots"):
        found = discover_docroots(profile.domains)
        roots = []
        for name in profile.domains:
            for r in found.get(name, []):
                if r not in roots:
                    roots.append(r)
        out["docroots"] = roots
    if profile.db_type == "sqlite" and not out.get("db_path"):
        out["db_path"] = discover_sqlite_db(profile.business_tables)
    out["_discovered"] = True
    return out
