#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Service Profile —— 声明式服务档位。

用户不写脚本、不答技术问题，只填一个 JSON（或由向导生成）。
三档对应方案里的 static / warm / stateful，决定同步内容和接管动作。

设计目标（来自 v2 方案 §4）：
    - 新服务接入只写 JSON，不改脚本
    - 文档根、数据库路径可以留空，由 discover 自动发现
    - 用户零操作 = 默认值覆盖 95% 情况，只有特例才需要覆盖
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

VALID_TIERS = ("static", "warm", "stateful")


@dataclass
class ServiceProfile:
    """单个"保护单元"——通常是一个站点。"""

    name: str                          # 保护单元名，如 "7zi.com"
    tier: str = "warm"                 # static | warm | stateful
    domains: list = field(default_factory=list)   # 用于自动发现的 server_name
    docroots: list = field(default_factory=list)  # 留空=自动发现；填了=覆盖
    db_path: Optional[str] = None      # 留空=自动发现常见路径
    db_type: str = "sqlite"            # sqlite | mysql（P2 只实现 sqlite）
    business_tables: list = field(default_factory=list)  # 校验用非空表；空=只做 integrity_check
    exclude: list = field(default_factory=lambda: [
        "node_modules", ".git", "__pycache__", ".next/cache", "*.tmp", "*.log",
    ])
    health_url: Optional[str] = None   # 换库/接管后的健康检查地址
    health_token: Optional[str] = None # 健康检查需要的 token（环境变量名，不写明文）
    services: list = field(default_factory=list)  # 需要预热/起停的服务单元名
    sync_every: str = "5m"             # 同步频率提示（编排层用）
    cold_dirs: list = field(default_factory=list) # 冷数据目录（低频同步）

    def __post_init__(self):
        if self.tier not in VALID_TIERS:
            raise ValueError(f"tier 必须是 {VALID_TIERS} 之一，收到 {self.tier!r}")
        if not self.name:
            raise ValueError("name 不能为空")

    @classmethod
    def from_dict(cls, d: dict) -> "ServiceProfile":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        """给用户看的一行人话，不暴露 TTL/root 这类词。"""
        what = {"static": "文件", "warm": "文件+数据库", "stateful": "文件+数据库+应用接管"}[self.tier]
        return f"{self.name}：保护{what}，同步间隔 {self.sync_every}"


def load_profiles(path: str) -> list:
    """从 JSON 文件加载。支持纯数组或 {"profiles": [...]}。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("profiles", [])
    return [ServiceProfile.from_dict(d) for d in data]


def save_example(path: str) -> None:
    """写一份傻瓜化示例配置。"""
    sample = {
        "profiles": [
            {
                "name": "7zi.com",
                "tier": "warm",
                "domains": ["7zi.com", "www.7zi.com"],
                "docroots": [],           # 留空 → 自动从 nginx 发现
                "db_path": None,          # 留空 → 自动发现
                "db_type": "sqlite",
                "business_tables": ["channels"],
                "health_url": "http://127.0.0.1:2000/api/health",
                "health_token": None,
                "services": ["api-gateway"],
                "sync_every": "5m",
                "cold_dirs": ["/var/www/erhu-brand/china"]
            },
            {
                "name": "blog.example.com",
                "tier": "static",
                "domains": ["blog.example.com"],
                "sync_every": "30m"
            }
        ]
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sample, fh, ensure_ascii=False, indent=2)
