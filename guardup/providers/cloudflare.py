"""Cloudflare DNS provider（API v4）。

Cloudflare 免费版没有原生 LB，但 DNS 记录可以改 —— 配合健康检查脚本
就构成了「主备切换」的效果。

凭据（两者任选其一，优先级从上到下）：
  * Global API Key + Email   → 用 X-Auth-Email / X-Auth-Key
  * Scoped API Token          → 用 Authorization: Bearer <token>

资源标识（可选，从环境变量读）：
  * CF_API_TOKEN / CF_API_KEY / CF_API_EMAIL
  * CF_ZONE_<ZONE>  可选，用于免去按域名反查 zone 的那一次请求

改记录用 PUT /zones/{zone_id}/dns_records/{record_id}，body 需带上完整记录
（type/name/content/ttl/proxied），少字段会被 Cloudflare 清空。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


class CloudflareProvider:
    name = "cloudflare"

    def __init__(self, token=None, api_key=None, email=None, zone_hint=None):
        self.token = token
        self.api_key = api_key
        self.email = email
        self.zone_hint = zone_hint
        if not token and not (api_key and email):
            raise CloudflareError(
                "缺少 Cloudflare 凭据：需要 CF_API_TOKEN，"
                "或 CF_API_KEY + CF_API_EMAIL"
            )

    # ---------- 内部 ----------
    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        else:
            h["X-Auth-Email"] = self.email
            h["X-Auth-Key"] = self.api_key
        return h

    def _req(self, method, path, body=None):
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise CloudflareError("Cloudflare %s %s -> HTTP %s: %s"
                                  % (method, path, e.code, detail)) from e
        except urllib.error.URLError as e:
            raise CloudflareError("Cloudflare 网络错误: %s" % e) from e
        if not out.get("success"):
            raise CloudflareError("Cloudflare 返回 success=false: %s"
                                  % json.dumps(out.get("errors"), ensure_ascii=False))
        return out.get("result")

    # ---------- 公开接口 ----------
    def zone_id(self, zone):
        if self.zone_hint:
            return self.zone_hint
        res = self._req("GET", "/zones?" + urllib.parse.urlencode({"name": zone}))
        if not res:
            raise CloudflareError("Cloudflare 里找不到 zone: %s" % zone)
        return res[0]["id"]

    def find_record(self, zone, name, rec_type="A"):
        """返回 (record_id, content, ttl, proxied)。找不到抛异常。"""
        zid = self.zone_id(zone)
        q = urllib.parse.urlencode({
            "type": rec_type, "name": name, "per_page": 100,
        })
        res = self._req("GET", "/zones/%s/dns_records?%s" % (zid, q)) or []
        if not res:
            raise CloudflareError("Cloudflare 里找不到记录 %s (%s)"
                                  % (name, rec_type))
        rec = res[0]
        return rec["id"], rec["content"], rec.get("ttl", 1), rec.get("proxied", False)

    def set_record(self, zone, name, content, rec_type="A", ttl=60,
                   proxied=False):
        """把记录改成 content。返回旧值。"""
        rid, old, old_ttl, old_proxied = self.find_record(zone, name, rec_type)
        body = {
            "type": rec_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        }
        self._req("PUT", "/zones/%s/dns_records/%s" % (self.zone_id(zone), rid),
                  body)
        return old

    # ---------- TXT（用于 Let's Encrypt DNS-01 预签发）----------
    def add_txt(self, zone, name, content, ttl=60):
        """新增一条 TXT，返回 record_id。"""
        zid = self.zone_id(zone)
        body = {"type": "TXT", "name": name, "content": content,
                "ttl": ttl, "proxied": False}
        res = self._req("POST", "/zones/%s/dns_records" % zid, body)
        return res.get("id")

    def del_txt(self, zone, record_id):
        self._req("DELETE",
                  "/zones/%s/dns_records/%s" % (self.zone_id(zone), record_id))
        return True
