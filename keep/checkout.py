#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkout.py —— 收款通道适配层：Creem 主 / Dodo 备，可切换（商业化落地）。

背景（方案 v1.4 定稿）：
    - Lemon Squeezy 已停（2026-11-16 停新注册、2027-03-31 停付款）。
    - Creem 主（大陆个人证件+人脸、支付宝提现、3.9%+$0.40、MoR 免税）。
    - Dodo 备（证件签发国判定含 China、4%+$0.40，提现搁置投诉需盯）。
    - PayPal 只应急。

本模块是一个「可切换适配层」——两件事：
    1. 定价/套餐目录的唯一真相源（三档 + 接入费），避免落地页/结算页价格打架。
    2. checkout URL 生成：给一个 plan + 一个 provider，产出该平台的结算链接。
       前端同时挂主备两个 provider， Creem 被封/审核卡住时一键切 Dodo，
       不改后端、不改价格。

刻意不做（安全边界）：
    - 不实际建单、不碰钱、不存 KYC、不放任何密钥明文。
    - product_id / API key 从环境变量注入，缺失时生成「配置占位」URL，
      保证本地测试与审核前流程可跑通而不泄露任何真实凭证。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_monthly: int          # USD
    tagline: str
    features: tuple

    def to_dict(self) -> dict:
        d = asdict(self)
        d["features"] = list(self.features)
        return d


# 三档定价（FULL-STACK-PLAN-v2.md §3 定稿）；价格阶梯唯一维度 = RTO
PLANS = {
    "seed": Plan(
        key="seed", name="Seed", price_monthly=19,
        tagline="个人站点与博客的自动故障转移",
        features=("DNS 自动切换", "静态文件同步（每 30 分钟）",
                  "含 5GB 存储", "每季度演练报告"),
    ),
    "guard": Plan(
        key="guard", name="Guard", price_monthly=79,
        tagline="出海小 SaaS / 电商的全栈热备（主推）",
        features=("DNS 自动切换", "静态文件同步（每 5 分钟）",
                  "SQLite/MySQL 一致性快照", "无状态应用自动接管",
                  "多地二次确认", "含 50GB 存储", "每月演练报告"),
    ),
    "fortress": Plan(
        key="fortress", name="Fortress", price_monthly=199,
        tagline="收入强依赖系统的 SMB，保证 RTO",
        features=("DNS 自动切换", "静态文件同步（实时）",
                  "PG/MySQL 实时 + PITR 7 天", "有状态应用自动接管",
                  "多地二次确认", "含 200GB 存储", "每月演练报告 + SLA credit"),
    ),
}

ONBOARDING_FEE_USD = 99  # 一次性接入费：环境审计 + 首次全量同步 + 一次真实演练，抵首月

# provider 配置：base URL + 环境变量名（值从环境注入，绝不在代码里出现）
PROVIDERS = {
    "creem": {
        "name": "Creem",
        "base": "https://www.creem.io/checkout",
        "product_env_prefix": "GUARDUP_CREEM_PRODUCT_",
        "primary": True,
    },
    "dodo": {
        "name": "Dodo Payments",
        "base": "https://checkout.dodopayments.com",
        "product_env_prefix": "GUARDUP_DODO_PRODUCT_",
        "primary": False,
    },
}


def list_plans() -> list:
    return [p.to_dict() for p in PLANS.values()]


def get_plan(key: str) -> Plan:
    if key not in PLANS:
        raise KeyError(f"未知套餐 {key!r}，可选 {list(PLANS)}")
    return PLANS[key]


def quote(plan_key: str, *, with_onboarding: bool = False) -> dict:
    """报价：月度 + 可选一次性接入费，返回 USD 明细。"""
    plan = get_plan(plan_key)
    monthly = plan.price_monthly
    onboarding = ONBOARDING_FEE_USD if with_onboarding else 0
    return {
        "plan": plan.name,
        "monthly_usd": monthly,
        "onboarding_usd": onboarding,
        "first_charge_usd": monthly + onboarding,
        "note": "接入费抵首月" if onboarding else "",
    }


def product_id(provider: str, plan_key: str) -> Optional[str]:
    """从环境变量取该 provider 的 product id；未配置返回 None。"""
    if provider not in PROVIDERS:
        raise KeyError(f"未知 provider {provider!r}，可选 {list(PROVIDERS)}")
    env = PROVIDERS[provider]["product_env_prefix"] + plan_key.upper()
    return os.environ.get(env) or None


def checkout_url(provider: str, plan_key: str, *, email: Optional[str] = None) -> str:
    """生成结算 URL。product id 未配置时用占位 id，保证审核前流程可跑通。

    email 仅作为预填参数传给平台；本模块不存储、不打印、不落盘。
    """
    if provider not in PROVIDERS:
        raise KeyError(f"未知 provider {provider!r}，可选 {list(PROVIDERS)}")
    plan = get_plan(plan_key)
    cfg = PROVIDERS[provider]
    pid = product_id(provider, plan_key) or f"PLACEHOLDER_{provider}_{plan_key.upper()}"
    if provider == "creem":
        url = f"{cfg['base']}?product_id={pid}"
    else:  # dodo
        url = f"{cfg['base']}/{pid}"
    if email:
        sep = "&" if "?" in url else "?"
        url += f"{sep}customer_email={email}"
    return url


def dual_checkout(plan_key: str, *, email: Optional[str] = None) -> dict:
    """返回主备两条结算链接，供前端一键切换防封。"""
    return {
        "plan": get_plan(plan_key).name,
        "primary": {"provider": "creem", "url": checkout_url("creem", plan_key, email=email)},
        "fallback": {"provider": "dodo", "url": checkout_url("dodo", plan_key, email=email)},
    }
