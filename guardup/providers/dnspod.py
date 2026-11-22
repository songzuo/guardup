"""腾讯云 DNSPod provider。

mainlander.cn 和 szspd.cn 的 NS 是 f1g1ns1/2.dnspod.net，
A 记录通过 DNSPod 的 v1 HTTP API 改。

认证两种：
  * login_token = "ID,TOKEN"（推荐，可用子账号的 API 密钥）
  * login_token = "ID,TOKEN,email"（老式）

接口是 POST + form，域名固定 dnsapi.cn。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://dnsapi.cn"


class DnsPodError(RuntimeError):
    pass


class DnsPodProvider:
    name = "dnspod"

    def __init__(self, login_token=None):
        # login_token 形如 "12345,abcdef..."
        self.login_token = login_token
        if not login_token or "," not in login_token:
            raise DnsPodError(
                "缺少 DNSPod 凭据：需要 DNSPOD_LOGIN_TOKEN，形如 \"ID,TOKEN\""
            )

    def _post(self, path, params):
        data = dict(params)
        data["login_token"] = self.login_token
        data["format"] = "json"
        data["lang"] = "en"
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            API + path, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "failover/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise DnsPodError("DNSPod %s -> HTTP %s: %s"
                              % (path, e.code, detail)) from e
        except urllib.error.URLError as e:
            raise DnsPodError("DNSPod 网络错误: %s" % e) from e
        status = out.get("status", {})
        if str(status.get("code")) not in ("1", "0"):
            raise DnsPodError("DNSPod 返回错误 %s: %s"
                              % (status.get("code"), status.get("message")))
        return out

    def find_record(self, domain, sub, rec_type="A", rec_line="默认"):
        """返回 (record_id, value)。找不到抛异常。"""
        out = self._post("/Record.List", {
            "domain": domain,
            "sub_domain": sub,
            "record_type": rec_type,
            "record_line": rec_line,
            "limit": 100,
        })
        for rec in out.get("records") or []:
            if rec.get("type") == rec_type and rec.get("line") == rec_line:
                return rec["id"], rec.get("value", "")
        raise DnsPodError("DNSPod 里找不到记录 %s.%s (%s)"
                          % (sub, domain, rec_type))

    def set_record(self, domain, sub, value, rec_type="A", ttl=60,
                   rec_line="默认"):
        rid, old = self.find_record(domain, sub, rec_type, rec_line)
        self._post("/Record.Modify", {
            "domain": domain,
            "record_id": rid,
            "sub_domain": sub,
            "record_type": rec_type,
            "record_line": rec_line,
            "value": value,
            "ttl": str(ttl),
        })
        return old

    # ---------- TXT（用于 Let's Encrypt DNS-01 预签发）----------
    def add_txt(self, domain, sub, value, ttl=60, rec_line="默认"):
        out = self._post("/Record.Create", {
            "domain": domain,
            "sub_domain": sub,
            "record_type": "TXT",
            "record_line": rec_line,
            "value": value,
            "ttl": str(ttl),
        })
        rec = (out.get("record") or {})
        return rec.get("id")

    def del_txt(self, domain, record_id):
        self._post("/Record.Remove", {
            "domain": domain,
            "record_id": record_id,
        })
        return True
