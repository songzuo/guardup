"""DNS provider 统一入口。

按 provider 名动态加载 providers/ 下的模块，保证公开方法签名一致：

    find_record(zone, name, rec_type) -> (record_id, current_value)
    set_record(zone, name, value, rec_type, ttl) -> old_value

「zone」在不同 provider 里含义不同：
    * cloudflare:    zone 名（7zi.com）
    * aliyun:        域名本身（u.org.cn）
    * dnspod:        域名（mainlander.cn），记录名另由配置给出
    * tencentcloud:  域名（mainlander.cn / szspd.cn）

凭据统一从环境变量读（见 credentials.env.example），也可用关键字参数覆盖。
"""

from __future__ import annotations

import importlib
import os

from .aliyun import AliyunDnsError, AliyunProvider
from .cloudflare import CloudflareError, CloudflareProvider
from .dnspod import DnsPodError, DnsPodProvider
from .tencentcloud import (TencentCloudDNSPod, TencentCloudError,
                           TencentCloudProvider)

PROVIDERS = {
    "cloudflare": (CloudflareProvider, CloudflareError),
    "aliyun": (AliyunProvider, AliyunDnsError),
    "dnspod": (DnsPodProvider, DnsPodError),
    "tencentcloud": (TencentCloudProvider, TencentCloudError),
}


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


def _defaults_for(kind):
    """从环境变量取出该 provider 的默认凭据参数。"""
    if kind == "cloudflare":
        return {
            "token": _env("CF_API_TOKEN"),
            "api_key": _env("CF_API_KEY"),
            "email": _env("CF_API_EMAIL"),
        }
    if kind == "aliyun":
        return {
            "access_key_id": _env("ALICLOUD_ACCESS_KEY_ID"),
            "access_key_secret": _env("ALICLOUD_ACCESS_KEY_SECRET"),
        }
    if kind == "dnspod":
        # DNSPod v1 HTTP API：login_token = "ID,TOKEN"
        tok = _env("DNSPOD_LOGIN_TOKEN")
        if tok and "," not in tok:
            # 只给了 token 没给 ID —— 用腾讯云 API 走不通 v1，提示清楚
            raise DnsPodError(
                "DNSPOD_LOGIN_TOKEN 格式应为 \"ID,TOKEN\"（DNSPod 令牌页的"
                "\"ID\"和\"令牌\"两部分，用逗号分隔）。若只有腾讯云 "
                "SecretId/SecretKey，请把 provider 改成 tencentcloud。")
        return {"login_token": tok}
    if kind == "tencentcloud":
        return {
            "secret_id": _env("TENCENT_SECRET_ID", "TENCENTCLOUD_SECRET_ID"),
            "secret_key": _env("TENCENT_SECRET_KEY", "TENCENTCLOUD_SECRET_KEY"),
            "region": _env("TENCENT_REGION") or "ap-guangzhou",
        }
    return {}


def get_provider(kind, **kwargs):
    if kind not in PROVIDERS:
        raise ValueError("未知 provider: %s（可用: %s）"
                         % (kind, ", ".join(sorted(PROVIDERS))))
    cls, _err = PROVIDERS[kind]
    params = _defaults_for(kind)
    params.update({k: v for k, v in kwargs.items() if v is not None})
    return cls(**params)


__all__ = ["PROVIDERS", "get_provider",
           "CloudflareProvider", "AliyunProvider", "DnsPodProvider",
           "TencentCloudProvider",
           "CloudflareError", "AliyunDnsError", "DnsPodError",
           "TencentCloudError"]
