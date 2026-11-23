#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App Takeover —— Next.js 动态站接管（全栈热备的硬骨头，P3 dogfooding 产物）。

这是 v2 方案里"产品化要补的三个自动化缺口"之第一个：动态站/Next.js 接管。
现有 _failover_live 对 13 个反代子域只给 503 兜底，7zi.com 无 .next 产物——
本模块把"构建产物 + 依赖 + 环境变量 → 备站起服务 → 健康检查 → 失败回滚"
整条链自动化，把人工"半天/站"的活压成一条命令。

对齐真实部署形态（实读 7zi-frontend/Dockerfile + next.config.ts）：
    Next.js 16 + output:'standalone'
    standalone 产物自带依赖，拷过去 node server.js 直接起，无需 npm install
    健康检查 GET /api/health 返回 200

设计原则：
    - 版本化目录：每个产物版本放独立目录，切换=换软链，回滚=切回旧软链
      （不是原地覆盖——动态站必须能秒切回上一个能跑的版本）
    - 真健康检查：HTTP 轮询 /api/health，不是"进程活着就算健康"
    - 冷启动耐心：实测 api-gateway 50 秒，默认给 60 秒
    - dry-run 只读：演练不真起服务
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# ---------------- 产物发现与指纹 ----------------

def detect_standalone(build_dir: str) -> dict:
    """识别 Next.js standalone 产物布局，返回各部分路径。

    真实 standalone 结构：
        build_dir/server.js
        build_dir/.next/standalone/  (可选：Docker 把它当根)
        build_dir/.next/static/
        build_dir/public/
    兼容两种常见形态：Docker 式（standalone 内容已摊到根）和原地式。
    """
    out = {"build_dir": build_dir}
    candidates = [build_dir, os.path.join(build_dir, ".next", "standalone")]
    server = ""
    for c in candidates:
        for name in ("server.js", "index.js"):
            p = os.path.join(c, name)
            if os.path.exists(p):
                server = p
                out["root"] = c
                break
        if server:
            break
    out["server_js"] = server
    out["is_standalone"] = bool(server)
    for sub, key in [(".next/static", "static"), ("public", "public")]:
        for c in (build_dir, out.get("root", build_dir)):
            p = os.path.join(c, sub)
            if os.path.isdir(p):
                out[key] = p
                break
    return out


def artifact_fingerprint(build_dir: str, exclude=("node_modules", ".git")) -> str:
    """对产物目录算内容指纹（路径+大小+内容前 64K 的 hash）。

    用于判断"产物是否真的变了"，避免同步到一半或重复重启。
    """
    h = hashlib.md5()
    root = build_dir
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in exclude)
        for name in sorted(files):
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root).replace("\\", "/")
            h.update(rel.encode(errors="replace"))
            try:
                h.update(str(os.path.getsize(p)).encode())
                with open(p, "rb") as fh:
                    h.update(fh.read(65536))
            except OSError:
                h.update(b"<unreadable>")
    return h.hexdigest()


def sync_artifact(build_dir: str, version_dir: str, static_dir: str = "",
                  public_dir: str = "") -> str:
    """把产物复制到一个版本化目录（standalone 根 + static + public）。

    复制而非移动——主站构建产物不能被破坏。
    """
    info = detect_standalone(build_dir)
    if not info["is_standalone"]:
        raise RuntimeError(f"不是有效的 Next.js standalone 产物: {build_dir}")
    root = info["root"]
    if os.path.exists(version_dir):
        shutil.rmtree(version_dir)
    os.makedirs(version_dir, exist_ok=True)
    # 复制 standalone 根（server.js + 依赖）
    for item in os.listdir(root):
        src = os.path.join(root, item)
        dst = os.path.join(version_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", "__pycache__"))
        else:
            shutil.copy2(src, dst)
    # static / public 要放到 .next/static 和 public 的位置（Next 约定）
    if static_dir and os.path.isdir(static_dir):
        shutil.copytree(static_dir, os.path.join(version_dir, ".next", "static"),
                        dirs_exist_ok=True)
    if public_dir and os.path.isdir(public_dir):
        shutil.copytree(public_dir, os.path.join(version_dir, "public"),
                        dirs_exist_ok=True)
    # 写指纹清单
    fp = artifact_fingerprint(build_dir)
    with open(os.path.join(version_dir, ".guardup-artifact.json"), "w", encoding="utf-8") as fh:
        json.dump({"fingerprint": fp, "source": build_dir, "synced_at": time.time()}, fh)
    return fp


# ---------------- 服务生命周期 ----------------

def _free_port(start: int = 3000) -> int:
    """找一个可用端口（避免和主站/其他实例冲突）。"""
    for p in range(start, start + 100):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def http_health(url: str, timeout: float = 3, expect=(200,)) -> bool:
    """真实 HTTP 健康检查。"""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in expect
    except Exception:
        return False


@dataclass
class TakeoverResult:
    action: str           # started | unchanged | rolled_back | failed | dry_run
    detail: str = ""
    port: int = 0
    healthy: bool = False


def takeover(
    version_dir: str,
    *,
    port: int = 0,
    health_path: str = "/api/health",
    health_timeout: float = 60,
    health_interval: float = 2,
    dry_run: bool = False,
    rollback_to: Optional[str] = None,
    proc_factory=None,      # 测试注入：返回可 start 的进程对象
    health_fn=None,         # 测试注入：无参 -> bool
    now_fn=time.time,
    sleep_fn=time.sleep,
) -> TakeoverResult:
    """启动一个版本的 Next.js 服务并做健康检查，失败可回滚到旧版本。

    version_dir: 版本化产物目录（含 server.js）
    rollback_to: 旧版本目录；新版本不健康时停掉新的、起旧的
    proc_factory: 注入进程构造（dogfooding 用真 subprocess，单测可注入假进程）
    """
    server = os.path.join(version_dir, "server.js")
    if not os.path.exists(server):
        return TakeoverResult("failed", f"版本目录缺 server.js: {version_dir}")
    port = port or _free_port()
    url = f"http://127.0.0.1:{port}{health_path}"

    if dry_run:
        return TakeoverResult("dry_run", f"[dry-run] 将在端口 {port} 启动并探测 {url}", port)

    def launch(vdir):
        srv = os.path.join(vdir, "server.js")
        env = os.environ.copy()
        env["PORT"] = str(port)
        env["HOSTNAME"] = "127.0.0.1"
        if proc_factory:
            proc = proc_factory(srv, env)
        else:
            proc = subprocess.Popen(
                [sys.executable if srv.endswith(".py") else "node", srv],
                cwd=vdir, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return proc

    probe = health_fn or (lambda: http_health(url))
    proc = launch(version_dir)
    deadline = now_fn() + health_timeout
    healthy = False
    while now_fn() < deadline:
        if proc.poll() is not None:
            break  # 进程自己退了
        if probe():
            healthy = True
            break
        sleep_fn(health_interval)

    if healthy:
        return TakeoverResult("started", f"新版本已起并通过 {health_path}", port, True)

    # 新版本失败：停掉，尝试回滚
    try:
        proc.terminate(); proc.wait(timeout=5)
    except Exception:
        try: proc.kill()
        except Exception: pass

    if rollback_to and os.path.exists(os.path.join(rollback_to, "server.js")):
        # 关键：回滚必须沿用【同一个端口】。对外软链/反代指向的是这个端口，
        # 换端口等于回滚后用户仍打到坏版本，回滚无效。
        # launch() 已把 port 注入 env，这里复用同一个 port 即可。
        old_proc = launch(rollback_to)
        old_env_ok = False
        deadline = now_fn() + health_timeout
        while now_fn() < deadline:
            if old_proc.poll() is not None:
                break
            if probe():
                old_env_ok = True
                break
            sleep_fn(health_interval)
        if old_env_ok:
            return TakeoverResult("rolled_back",
                                  f"新版本不健康，已切回旧版本（同端口 {port}）", port, True)
        try:
            old_proc.terminate(); old_proc.wait(timeout=5)
        except Exception:
            try: old_proc.kill()
            except Exception: pass
    return TakeoverResult("failed", f"新版本健康检查失败，且无可用回滚版本", port, False)


# ---------------- 软链切换（生产接管的原子动作） ----------------

def switch_symlink(link_path: str, target_dir: str) -> None:
    """把对外服务目录软链指向新版本目录。回滚=指回旧目录。

    Linux 上可用 os.replace 原子替换；Windows 上 os.replace 覆盖已存在软链
    会报 WinError 5，故先移除旧链再建。产品部署在 Linux，这里兼顾本地测试。
    """
    if os.path.islink(link_path) or os.path.exists(link_path):
        if os.path.isdir(link_path) and not os.path.islink(link_path):
            shutil.rmtree(link_path)
        else:
            os.remove(link_path)
    os.symlink(target_dir, link_path, target_is_directory=True)
