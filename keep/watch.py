#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Content Watch —— 内容守卫（golden 快照防退化）。

脱胎于 guard_mainlander.sh：站点内容被改坏/同步退化时，用 golden 快照检测
并恢复。泛化为：对任意目录建指纹快照，检测到「内容退化」（文件数骤减 /
关键文件消失）时报警或回滚。

判据刻意保守——只拦"明显退化"，不拦正常更新：
    - 当前文件数 < golden 的 50%  → 退化
    - golden 里的关键文件现在没了 → 退化
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


def fingerprint(root: str, exclude=()) -> dict:
    """对目录建指纹：文件数、总字节、关键文件（前若干）的 hash。"""
    if not os.path.isdir(root):
        return {"exists": False, "count": 0, "bytes": 0, "files": {}}
    files = {}
    total = 0
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude]
        for name in names:
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root)
            if any(x in rel for x in exclude):
                continue
            try:
                st = os.stat(p)
                total += st.st_size
                if len(files) < 200:   # 只记前 200 个的 hash，够用且快
                    h = hashlib.md5()
                    with open(p, "rb") as fh:
                        h.update(fh.read(65536))
                    files[rel] = h.hexdigest()
            except OSError:
                continue
    return {"exists": True, "count": len(files), "bytes": total, "files": files,
            "_all_count": sum(1 for b, d, ns in os.walk(root) for _ in ns)}


def save_golden(root: str, path: str, exclude=()) -> dict:
    fp = fingerprint(root, exclude)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fp, fh)
    return fp


@dataclass
class WatchResult:
    degraded: bool
    reason: str = ""
    golden_count: int = 0
    current_count: int = 0


def check(root: str, golden_path: str, exclude=(), key_files=()) -> WatchResult:
    """对比当前目录与 golden，检测退化。"""
    with open(golden_path, "r", encoding="utf-8") as fh:
        golden = json.load(fh)
    cur = fingerprint(root, exclude)
    gcount = golden.get("_all_count", golden.get("count", 0))
    ccount = cur.get("_all_count", cur.get("count", 0))
    if not cur["exists"]:
        return WatchResult(True, "目录整体消失", gcount, ccount)
    if gcount > 0 and ccount < gcount * 0.5:
        return WatchResult(True, f"文件数骤减 {gcount} -> {ccount}（<50%）", gcount, ccount)
    for kf in key_files:
        if kf in golden.get("files", {}) and kf not in cur.get("files", {}):
            return WatchResult(True, f"关键文件消失: {kf}", gcount, ccount)
    return WatchResult(False, "ok", gcount, ccount)
