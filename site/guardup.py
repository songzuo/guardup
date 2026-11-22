#!/usr/bin/env python3
"""GuardUp standalone — 零依赖单文件网站自动容灾 Agent。
由 tools/build_standalone.py 自动生成，请勿手改。
用法: python guardup-standalone.py init | status | check | drill | daemon
"""
from __future__ import annotations
import argparse, http.client, json, os, socket, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, List, Optional


# ===== guardup/providers/cloudflare.py =====

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

# ===== guardup/providers/aliyun.py =====

"""阿里云 DNS（万网 / hichina）provider。

u.org.cn 的 NS 是 dns17/18.hichina.com，所以它的 A 记录不走 Cloudflare，
必须用阿里云的 DescribeDomainRecords / UpdateDomainRecord。

阿里云 API 走 RPC 风格签名（SignatureMethod=HMAC-SHA1）：
  1. 把除 Signature / PublicKey 外的参数按 key 排序，拼成
     `k=v&k=v`（value 做 RFC3986 编码）；
  2. StringToSign = "GET&%2F&" + percent_encode(canonical_query)；
  3. Signature = base64(hmac_sha1(AccessKeySecret + "&", StringToSign))。

凭据从环境变量读：
  * ALICLOUD_ACCESS_KEY_ID / ALICLOUD_ACCESS_KEY_SECRET
  * ALI_REGION 可选，默认 cn-hangzhou
"""


import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ENDPOINT = "https://alidns.aliyuncs.com"
VERSION = "2015-01-09"


class AliyunDnsError(RuntimeError):
    pass


def _percent_encode(s):
    """RFC3986 编码：字母数字 -_.~ 不编码，空格变 %20（不是 +）。"""
    return urllib.parse.quote(str(s), safe="-_.~")


class AliyunProvider:
    name = "aliyun"

    def __init__(self, access_key_id=None, access_key_secret=None,
                 region="cn-hangzhou"):
        self.ak = access_key_id
        self.sk = access_key_secret
        self.region = region
        if not access_key_id or not access_key_secret:
            raise AliyunDnsError(
                "缺少阿里云凭据：需要 ALICLOUD_ACCESS_KEY_ID 与 "
                "ALICLOUD_ACCESS_KEY_SECRET"
            )

    def _sign(self, params):
        signed = {k: v for k, v in params.items()
                  if k not in ("Signature", "PublicKey")}
        canon = "&".join("%s=%s" % (k, _percent_encode(signed[k]))
                         for k in sorted(signed))
        sts = "GET&%2F&" + _percent_encode(canon)
        sig = base64.b64encode(
            hmac.new((self.sk + "&").encode("utf-8"),
                     sts.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        out = dict(signed)
        out["Signature"] = sig
        return out

    def _call(self, action, extra):
        params = {
            "AccessKeyId": self.ak,
            "Action": action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": VERSION,
        }
        params.update(extra)
        params = self._sign(params)
        url = ENDPOINT + "/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise AliyunDnsError("阿里云 %s -> HTTP %s: %s"
                                 % (action, e.code, detail)) from e
        except urllib.error.URLError as e:
            raise AliyunDnsError("阿里云网络错误: %s" % e) from e
        if body.get("Code"):
            raise AliyunDnsError("阿里云返回错误 %s: %s"
                                 % (body.get("Code"), body.get("Message")))
        return body

    def find_record(self, domain, rr, rec_type="A"):
        """返回 (record_id, value)。找不到抛异常。

        注意：不用 RRKeyWord 过滤——'@' 在 RRKeyWord 里语义特殊，
        而且阿里云会返回 LbaStatus（负载均衡）记录，容易匹配错。
        这里拉全量再按 RR + Type 精确匹配。
        """
        recs = self.list_records(domain, rec_type)
        for rec in recs:
            if rec.get("RR") == rr and rec.get("Type") == rec_type:
                return rec["RecordId"], rec.get("Value", "")
        raise AliyunDnsError("阿里云里找不到记录 %s %s" % (rr, rec_type))

    def list_records(self, domain, rec_type=None):
        """列出域名全部记录（可按类型过滤）。"""
        extra = {"DomainName": domain, "PageSize": "100"}
        if rec_type:
            extra["Type"] = rec_type
        res = self._call("DescribeDomainRecords", extra)
        return (res.get("DomainRecords") or {}).get("Record") or []

    def set_record(self, domain, rr, value, rec_type="A", ttl=60):
        """把 rr 指向 value。幂等：已经是目标值就当成功。

        阿里云 UpdateDomainRecord 在目标值与现存值相同时会抛
        DomainRecordDuplicate（HTTP 400），所以这里先读后写，
        并把「重复记录」错误在复读确认后视为成功。
        """
        rid, old = self.find_record(domain, rr, rec_type)
        if old == value:
            return old
        try:
            self._call("UpdateDomainRecord", {
                "RecordId": rid,
                "RR": rr,
                "Type": rec_type,
                "Value": value,
                "TTL": ttl,
            })
        except AliyunDnsError as e:
            if "DomainRecordDuplicate" not in str(e):
                raise
            # 复读确认是否已生效（可能是同名多记录中的另一条已改好）
            _, cur = self.find_record(domain, rr, rec_type)
            if cur != value:
                raise
        return old

    # ---------- TXT（用于 Let's Encrypt DNS-01 预签发）----------
    def add_txt(self, domain, rr, value, ttl=60):
        res = self._call("AddDomainRecord", {
            "DomainName": domain,
            "RR": rr,
            "Type": "TXT",
            "Value": value,
            "TTL": ttl,
        })
        return res.get("RecordId")

    def del_txt(self, domain, record_id):
        self._call("DeleteDomainRecord", {"RecordId": record_id})
        return True

# ===== guardup/providers/dnspod.py =====

"""腾讯云 DNSPod provider。

mainlander.cn 和 szspd.cn 的 NS 是 f1g1ns1/2.dnspod.net，
A 记录通过 DNSPod 的 v1 HTTP API 改。

认证两种：
  * login_token = "ID,TOKEN"（推荐，可用子账号的 API 密钥）
  * login_token = "ID,TOKEN,email"（老式）

接口是 POST + form，域名固定 dnsapi.cn。
"""


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

# ===== guardup/providers/tencentcloud.py =====

# -*- coding: utf-8 -*-
"""腾讯云 DNSPod（v20210323）适配器。

优先使用官方 SDK（tencentcloud-sdk-python-dnspod），它负责 TC3-HMAC-SHA256
签名；若环境里没有 SDK，则回退到内置的等价实现。

一个 SecretId/SecretKey 就能管理账号下所有 DNSPod 域名，无需把 Token 拼成
"ID,TOKEN"，也不依赖单个域名的账号 ID —— 比 DNSPod 旧版 HTTP API(v1) 更适合
做多域名统一的主备切换。

接口：
  DescribeDomainList  列出账号下的域名
  DescribeRecordList  列出某域名的解析记录
  ModifyRecord        修改记录（切 A 记录 IP）
  CreateRecord        新增记录
  DeleteRecord        删除记录
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SERVICE = "dnspod"
HOST = "dnspod.tencentcloudapi.com"
VERSION = "2021-03-23"
ALGORITHM = "TC3-HMAC-SHA256"
CONTENT_TYPE = "application/json"


class TencentCloudError(Exception):
    pass


# ---------------------------------------------------------------- 序列化
def _to_plain(obj):
    """把 SDK 的模型对象递归转成 dict/list/标量。

    官方 SDK 返回的是 RecordListItem 之类的模型对象，不是 dict；
    统一转成 dict 后，业务代码就能用 r["Value"] 这种一致写法。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    # SDK 模型：优先用 _serialize()
    ser = getattr(obj, "_serialize", None)
    if callable(ser):
        try:
            return _to_plain(ser())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        d = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        if d:
            return {k: _to_plain(v) for k, v in d.items()}
    return obj


# ---------------------------------------------------------------- 签名兜底实现
def _sha256hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hmac(key, msg):
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


class _RawClient(object):
    """不依赖 SDK 的最小实现（与官方 SDK 的签名逻辑一致）。"""

    def __init__(self, secret_id, secret_key, region="", endpoint=HOST, timeout=20):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.endpoint = endpoint
        self.timeout = timeout

    def _sign(self, action, body_bytes, timestamp):
        """按腾讯云 TC3-HMAC-SHA256 规范生成 Authorization。

        实现严格对齐官方 SDK（tencentcloud-sdk-python）：
          * 签名的 header 只有 content-type 和 host（**不含 x-tc-action**）
          * POST 的 canonical querystring 是**空字符串**
            （Action/Version 放在 X-TC-Action / X-TC-Version 头里，不进 query）
          * Content-Type 固定 "application/json"，不能带 charset
          * payload hash 用实际发送的 body 字节算
        这几点只要错一个，服务端就会回 AuthFailure.SignatureFailure。
        """
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        payload_hash = _sha256hex(body_bytes)

        canonical_headers = "content-type:%s\nhost:%s\n" % (CONTENT_TYPE,
                                                            self.endpoint)
        signed_headers = "content-type;host"
        canonical_request = "%s\n%s\n%s\n%s\n%s\n%s" % (
            "POST", "/", "", canonical_headers, signed_headers, payload_hash)

        scope = "%s/%s/tc3_request" % (date, SERVICE)
        sts = "%s\n%s\n%s\n%s" % (ALGORITHM, str(timestamp), scope,
                                  _sha256hex(canonical_request.encode("utf-8")))

        secret_date = _hmac(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = _hmac(secret_date, SERVICE)
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, sts.encode("utf-8"),
                             hashlib.sha256).hexdigest()

        return ("%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                % (ALGORITHM, self.secret_id, scope, signed_headers, signature))

    def call(self, action, **payload):
        timestamp = int(time.time())
        # body 字节必须与签名时算 hash 用的是同一份，不能中途改
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": self._sign(action, body, timestamp),
            "Content-Type": CONTENT_TYPE,
            "Host": self.endpoint,
            "X-TC-Action": action,
            "X-TC-Version": VERSION,
            "X-TC-Timestamp": str(timestamp),
        }
        if self.region:
            headers["X-TC-Region"] = self.region
        req = urllib.request.Request("https://%s/" % self.endpoint, data=body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.getcode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        if status != 200:
            raise TencentCloudError("HTTP %s: %s" % (status, raw[:400]))
        try:
            data = json.loads(raw)
        except ValueError:
            raise TencentCloudError("响应不是 JSON: %s" % raw[:400])
        err = (data.get("Response") or {}).get("Error")
        if err:
            raise TencentCloudError("%s / %s (RequestId=%s)" % (
                err.get("Code"), err.get("Message"), err.get("RequestId", "")))
        return data.get("Response") or {}


# ---------------------------------------------------------------- 统一适配器
class TencentCloudDNSPod(object):
    """DNSPod 解析记录读写。接口与 failover/providers/dnspod.py 保持一致。"""

    def __init__(self, secret_id=None, secret_key=None, region="ap-guangzhou",
                 token=None, timeout=20, endpoint=HOST, use_sdk=True):
        if not secret_id or not secret_key:
            raise TencentCloudError("SecretId / SecretKey 不能为空")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.token = token
        self.timeout = timeout
        self.endpoint = endpoint
        self._sdk = None
        self._raw = None

        if use_sdk:
            try:
                from tencentcloud.common import credential
                from tencentcloud.dnspod.v20210323 import dnspod_client, models
                cred = credential.Credential(secret_id, secret_key, token)
                self._sdk = dnspod_client.DnspodClient(cred, region)
                self._models = models
            except ImportError:
                self._sdk = None
        if self._sdk is None:
            self._raw = _RawClient(secret_id, secret_key, region, endpoint, timeout)

    # ---------- 底层调用 ----------
    def _call(self, action, **payload):
        if self._sdk is not None:
            m = self._models
            model = getattr(m, action + "Request")
            req = model()
            for k, v in payload.items():
                setattr(req, k, v)
            try:
                resp = getattr(self._sdk, action)(req)
            except Exception as e:
                raise TencentCloudError("%s: %s" % (action, e))
            return _to_plain(resp)
        return self._raw.call(action, **payload)

    # ---------- 业务方法 ----------
    def list_domains(self, offset=0, limit=100):
        resp = self._call("DescribeDomainList", Offset=offset, Limit=limit)
        return resp.get("DomainList") or []

    def find_domain(self, domain):
        domain = domain.lower().strip()
        for d in self.list_domains():
            if str(d.get("Name", "")).lower() == domain:
                return d
        return None

    def list_records(self, domain):
        """列出域名下所有解析记录。"""
        out = []
        offset = 0
        while True:
            resp = self._call("DescribeRecordList", Domain=domain,
                               Limit=100, Offset=offset)
            recs = resp.get("RecordList") or []
            out.extend(recs)
            if len(recs) < 100:
                break
            offset += 100
        return out

    def find_record(self, domain, record_name, rtype="A", line="默认"):
        """找一条记录（返回 dict，含 RecordId/Value），没有返回 None。

        record_name 用 DNSPod 的写法：根域是 '@'，通配是 '*'。
        """
        for r in self.list_records(domain):
            if r.get("Type") != rtype:
                continue
            if r.get("Name") != record_name:
                continue
            rline = r.get("Line") or "默认"
            if line in (None, "", "默认"):
                if rline in ("默认", ""):
                    return r
            elif rline == line:
                return r
        return None

    def set_record(self, domain, record_name, value, rec_type="A", line="默认",
                   ttl=600, record_id=None):
        """把记录改成 value；不存在则新建。"""
        rtype = rec_type
        # DNSPod 免费套餐 TTL 下限是 600，低于会被 API 拒（RecordTtlLimit）
        ttl = max(600, int(ttl or 600))
        rec = self.find_record(domain, record_name, rtype, line)
        if rec is None:
            resp = self._call("CreateRecord", Domain=domain,
                               SubDomain=record_name, RecordType=rtype,
                               RecordLine=line, Value=value, TTL=ttl)
            return {"action": "create", "record_id": resp.get("RecordId"),
                    "old_value": None}
        rid = record_id or rec.get("RecordId")
        self._call("ModifyRecord", Domain=domain, RecordId=rid,
                   SubDomain=record_name, RecordType=rtype,
                   RecordLine=line, Value=value, TTL=ttl)
        return {"action": "modify", "record_id": rid,
                "old_value": rec.get("Value")}

    def add_txt(self, domain, record_name, value, ttl=600):
        # DNSPod 用 SubDomain（相对域名），调用方可能传 FQDN，这里统一处理
        sub = self._to_subdomain(domain, record_name)
        resp = self._call("CreateRecord", Domain=domain, SubDomain=sub,
                          RecordType="TXT", RecordLine="默认", Value=value, TTL=ttl)
        return resp.get("RecordId")

    def del_txt(self, domain, record_name, value=None):
        sub = self._to_subdomain(domain, record_name)
        rec = None
        for r in self.list_records(domain):
            if r.get("Type") != "TXT" or r.get("Name") != sub:
                continue
            if value is None or r.get("Value") == value:
                rec = r
                break
        if rec is None:
            return False
        self._call("DeleteRecord", Domain=domain, RecordId=rec["RecordId"])
        return True

    def del_record(self, domain, record_name, rec_type="A", line="默认",
                   value=None):
        """删除任意类型的记录（del_txt 只处理 TXT，这里是通用版）。"""
        sub = self._to_subdomain(domain, record_name)
        for r in self.list_records(domain):
            if r.get("Type") != rec_type or r.get("Name") != sub:
                continue
            rline = r.get("Line") or "默认"
            if line not in (None, "", "默认"):
                if rline != line:
                    continue
            if value is not None and r.get("Value") != value:
                continue
            self._call("DeleteRecord", Domain=domain, RecordId=r["RecordId"])
            return True
        return False

    @staticmethod
    def _to_subdomain(domain, record_name):
        """把 FQDN 转成 DNSPod 的 SubDomain 写法。"""
        if not record_name:
            return "@"
        name = record_name.strip().rstrip(".")
        d = (domain or "").strip().rstrip(".")
        if d and name.lower().endswith("." + d.lower()):
            name = name[: -(len(d) + 1)]
        return name or "@"


# ---------------------------------------------------------------- provider 包装
class TencentCloudProvider:
    """与其它 provider 保持一致的对接接口。

    find_record(zone, name, rec_type) -> (record_id, value)
    set_record(zone, name, value, rec_type, ttl) -> old_value
    """

    name = "tencentcloud"

    def __init__(self, secret_id=None, secret_key=None, region="ap-guangzhou",
                 token=None, timeout=20):
        self.client = TencentCloudDNSPod(secret_id=secret_id,
                                          secret_key=secret_key,
                                          region=region, token=token,
                                          timeout=timeout)

    # ---------- 公开接口 ----------
    def find_record(self, zone, name, rec_type="A", rec_line="默认"):
        """返回 (record_id, value)。找不到抛异常。"""
        rec = self.client.find_record(zone, name, rec_type, rec_line)
        if rec is None:
            raise TencentCloudError(
                "DNSPod 里找不到记录 %s.%s (%s / %s)"
                % (name, zone, rec_type, rec_line))
        return rec.get("RecordId"), rec.get("Value", "")

    def set_record(self, zone, name, content, rec_type="A", ttl=60,
                   rec_line="默认"):
        """把记录改成 content。返回旧值。"""
        rec = self.client.find_record(zone, name, rec_type, rec_line)
        if rec is None:
            self.client.set_record(zone, name, content, rec_type=rec_type,
                                    line=rec_line, ttl=ttl)
            return ""
        self.client.set_record(zone, name, content, rec_type=rec_type,
                               line=rec_line, ttl=ttl,
                               record_id=rec.get("RecordId"))
        return rec.get("Value", "")

    # ---------- TXT（用于 Let's Encrypt DNS-01 预签发）----------
    def add_txt(self, zone, name, content, ttl=60):
        """新增一条 TXT，返回 record_id。"""
        return self.client.add_txt(zone, name, content, ttl=ttl)

    def del_txt(self, zone, record_id):
        """删除 TXT。record_id 可以是 RecordId，也可以是 (name, value)。"""
        if isinstance(record_id, (tuple, list)) and len(record_id) >= 1:
            return self.client.del_txt(zone, record_id[0],
                                       record_id[1] if len(record_id) > 1 else None)
        try:
            rid = int(record_id)
        except (TypeError, ValueError):
            return self.client.del_txt(zone, str(record_id))
        self.client._call("DeleteRecord", Domain=zone, RecordId=rid)
        return True


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


# ===== engine =====

"""健康检查 + DNS 主备切换引擎。

判定逻辑（避免"抖一下就切走"）：
  * 连续 N 次失败（默认 3）才判定主站宕机 -> 切到备用 IP；
  * 连续 M 次成功（默认 3）才判定主站恢复 -> 切回主 IP；
  * 每次切换后进入冷却期（默认 600 秒），冷却期内不再动作；
  * 切换是"幂等"的：只有目标 IP 与当前不一致才动手。

状态持久化在 state.json，进程重启不丢"当前指向谁"。

只做 DNS 层切换，不做流量层负载均衡 —— 这是免费版 Cloudflare
配合脚本能达到的效果，代价是切换生效要等 DNS TTL 过期。
"""


import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional



HERE = os.path.dirname(os.path.abspath(__file__))
# 部署布局有两种：
#   服务器 /opt/failover/failover/engine.py + /opt/failover/config.json
#   本地   deliverable7/failover/engine.py  + deliverable7/failover/config.json
# 两种都支持，找不到再报错。
def _default_path(name):
    for base in (os.path.dirname(HERE), HERE):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(HERE), name)


DEFAULT_CONFIG = os.path.join(os.path.expanduser('~'), '.guardup', 'config.json')
DEFAULT_STATE = os.path.join(os.path.expanduser('~'), '.guardup', 'state.json')
DEFAULT_LOG = "/var/log/failover/failover.log"


# ==================================================================
# 日志
# ==================================================================
def log(msg: str, path: str = DEFAULT_LOG) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


# ==================================================================
# 配置
# ==================================================================
class Config:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.targets: List[Dict[str, Any]] = data.get("targets", [])
        self.fail_threshold = int(data.get("fail_threshold", 3))
        self.recover_threshold = int(data.get("recover_threshold", 3))
        self.cooldown = int(data.get("cooldown_seconds", 600))
        self.timeout = int(data.get("http_timeout", 10))
        self.user_agent = data.get("user_agent",
                                  "Mozilla/5.0 (failover-healthcheck)")
        self.primary_ip = data.get("primary_ip", "")
        self.backup_ip = data.get("backup_ip", "")

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG) -> "Config":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))


# ==================================================================
# 健康检查
# ==================================================================
def check_url(url: str, timeout: int, user_agent: str,
              expect: Optional[List[int]] = None) -> Dict[str, Any]:
    """发一个 GET，返回 {ok, status, error, elapsed}。

    判定 ok 的标准：拿到 2xx/3xx，且（如果配了 expect）状态码在白名单里。
    连不上、超时、5xx 一律算失败 —— 5xx 说明源站活着但坏了，
    对"能不能对外服务"这个问题的答案同样是否。
    """
    t0 = time.time()
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Cache-Control": "no-cache",
    })
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            code = r.getcode()
            r.read(2048)
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            e.read(2048)
        except Exception:
            pass
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)[:200],
                "elapsed": round(time.time() - t0, 2)}
    ok = 200 <= code < 400
    if ok and expect:
        ok = code in expect
    return {"ok": ok, "status": code, "error": None,
            "elapsed": round(time.time() - t0, 2)}


def check_origin(ip: str, host: str, path: str = "/", timeout: int = 10,
                 user_agent: str = "Mozilla/5.0 (failover-healthcheck)",
                 expect: Optional[List[int]] = None) -> Dict[str, Any]:
    """**绕过 DNS 直连源站 IP** 做健康检查（Host 头仍为目标域名）。

    为什么必须这样：切换到备用 IP 之后，如果还按域名解析去检查，
    解析出来的就是备用机自己 —— 备用机当然健康，引擎于是判定
    "主站已恢复"，马上把 DNS 切回去，接着又切过来，**无限抖动**。
    只有直连主 IP 才能真正回答"主站回来了吗"。

    TLS 证书校验关闭：SNI 用目标域名，但连的是 IP，
    证书链由 SNI 决定，校验本来是对的；这里关掉只是为了让
    "证书过期"这类问题归到 HTTP 层处理，不干扰宕机判定。
    """
    t0 = time.time()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = None
    try:
        conn = http.client.HTTPSConnection(ip, 443, timeout=timeout,
                                           context=ctx)
        conn.request("GET", path, headers={
            "Host": host,
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Connection": "close",
        })
        resp = conn.getresponse()
        code = resp.status
        resp.read(2048)
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)[:200],
                "elapsed": round(time.time() - t0, 2)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    ok = 200 <= code < 400
    if ok and expect:
        ok = code in expect
    return {"ok": ok, "status": code, "error": None,
            "elapsed": round(time.time() - t0, 2)}


def check_tcp(host: str, port: int = 443, timeout: int = 5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ==================================================================
# 状态
# ==================================================================
class State:
    def __init__(self, path: str = DEFAULT_STATE):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value
        self.save()


# ==================================================================
# 引擎
# ==================================================================
class Failover:
    def __init__(self, cfg: Config, state: State, dry_run: bool = False):
        self.cfg = cfg
        self.state = state
        self.dry_run = dry_run
        self._providers: Dict[str, Any] = {}

    def provider(self, kind: str):
        if kind not in self._providers:
            self._providers[kind] = get_provider(kind)
        return self._providers[kind]

    # ---------- 单个 target ----------
    def check_target(self, t: Dict[str, Any]) -> Dict[str, Any]:
        """对一条记录做健康检查。

        判定以**直连主 IP** 为准（绕过 DNS），理由见 check_origin 的注释：
        切到备用后按域名解析会检查到备用自己，导致来回抖动。
        域名侧检查只作辅助信号，用于区分「主站真挂」与「DNS 坏了」。
        """
        host = t.get("host", "")
        path = t.get("health_path", "/")
        expect = t.get("expect")
        primary_ip = self.cfg.primary_ip

        res: Dict[str, Any] = {"ok": False, "detail": {}}

        # 快路径：本轮开跑时若主站 443 连都连不上，说明整机不可达，
        # 6 条记录没必要各等一个 http 超时（5s×6=30s，占满巡检周期）。
        if primary_ip and not getattr(self, "_primary_tcp_ok", True):
            res["ok"] = False
            res["detail"]["origin"] = {"ok": False, "status": None,
                                       "error": "primary tcp/443 unreachable",
                                       "elapsed": 0}
            try:
                res["detail"]["resolved"] = socket.gethostbyname(host)
            except OSError:
                res["detail"]["resolved"] = None
            return res

        if primary_ip:
            origin = check_origin(primary_ip, host, path, self.cfg.timeout,
                                  self.cfg.user_agent, expect)
            res["detail"]["origin"] = origin
            res["ok"] = bool(origin.get("ok"))
        else:
            # 没配 primary_ip 才退回域名检查
            url = t.get("health_url") or ("https://%s%s" % (host, path))
            origin = check_url(url, self.cfg.timeout, self.cfg.user_agent,
                               expect)
            res["detail"]["origin"] = origin
            res["ok"] = bool(origin.get("ok"))

        # 辅助信号：域名当前解析到哪（仅记录，不参与判定）
        try:
            res["detail"]["resolved"] = socket.gethostbyname(host)
        except OSError as e:
            res["detail"]["resolved"] = None
            res["detail"]["resolve_error"] = str(e)[:120]
        return res

    def switch_target(self, t: Dict[str, Any], to_backup: bool) -> Dict[str, Any]:
        """把一条记录指向主 IP 或备用 IP。返回 {changed, from, to}。"""
        kind = t["provider"]
        zone = t["zone"]
        name = t.get("record_name") or ""
        rec_type = t.get("type", "A")
        ttl = int(t.get("ttl", 60))
        # TTL 地板：阿里云与 DNSPod 的免费套餐都拒绝 <600（实测
        # QuotaExceeded.TTL / LimitExceeded.RecordTtlLimit）。若配置里写了
        # 60，切换时会整条失败 —— 那等于容灾失效，所以这里必须夹住。
        if kind in ("aliyun", "tencentcloud", "dnspod") and ttl < 600:
            log("  %s %s 的 TTL=%s 低于服务商下限，按 600 提交"
                % (zone, name, ttl))
            ttl = 600
        want = self.cfg.backup_ip if to_backup else self.cfg.primary_ip
        prov = self.provider(kind)
        extra = {}
        if kind in ("dnspod", "tencentcloud"):
            extra = {"rec_line": t.get("record_line", "默认")}
        elif kind == "cloudflare":
            extra = {"proxied": bool(t.get("proxied", False))}
        try:
            if self.dry_run:
                # dry-run 绝不能真的改 DNS —— 只读当前值。
                # （早期版本这里先调 set_record 再判断 dry_run，
                #   结果"干跑"把记录真的改了，是很坑的一个 bug。）
                old = self._peek_value(t, prov)
                log("  [dry-run] %s %s: %s -> %s（未真正改动）"
                    % (zone, name, old, want))
                return {"changed": old != want, "from": old, "to": want,
                        "dry_run": True}
            # 幂等：目标值和当前值一样就不写，避免每次巡检都打一次写 API。
            # （DNSPod ModifyRecord 每次都写会刷新记录时间，白费配额。）
            if self._peek_value(t, prov) == want:
                log("  已是目标值 %s %s: %s（无需改动）" % (zone, name, want))
                return {"changed": False, "from": want, "to": want}
            old = prov.set_record(zone, name, want, rec_type=rec_type,
                                  ttl=ttl, **extra)
        except TypeError:
            # provider 不接受该额外参数（保持最小签名兼容）
            old = prov.set_record(zone, name, want, rec_type=rec_type,
                                  ttl=ttl)
        log("  已切换 %s %s: %s -> %s" % (zone, name, old, want))
        return {"changed": old != want, "from": old, "to": want}

    def _peek_value(self, t, prov):
        """只读取记录当前值（dry-run 用），读不到就返回空串。"""
        kind = t["provider"]
        zone = t["zone"]
        name = t.get("record_name") or ""
        rec_type = t.get("type", "A")
        try:
            if kind in ("dnspod", "tencentcloud"):
                line = t.get("record_line", "默认")
                rec = prov.find_record(zone, name, rec_type, line)
                if isinstance(rec, tuple):
                    return rec[1]
                return rec.get("Value", "") if rec else ""
            # 各 provider 的 find_record 返回值形态不同：
            #   dnspod/tencentcloud/aliyun -> (record_id, value)
            #   cloudflare                 -> (record_id, content, ttl, proxied)
            # 统一取"值"的那一项。
            rec = prov.find_record(zone, name, rec_type)
            if isinstance(rec, (tuple, list)):
                return rec[1] if len(rec) > 1 else ""
            return rec.get("Value", "") if rec else ""
        except Exception as e:
            log("  [dry-run] 读取 %s %s 当前值失败: %s" % (zone, name, e))
            return ""

    def handle_target(self, t: Dict[str, Any]) -> Dict[str, Any]:
        key = "%s|%s" % (t["zone"], t.get("record_name", "@"))
        st = self.state.get(key, {}) or {}
        fails = int(st.get("fails", 0))
        oks = int(st.get("oks", 0))
        mode = st.get("mode", "primary")       # primary | backup
        last_switch = float(st.get("last_switch", 0))

        res = self.check_target(t)
        if res["ok"]:
            fails = 0
            oks += 1
        else:
            oks = 0
            fails += 1

        now = time.time()
        in_cooldown = (now - last_switch) < self.cfg.cooldown
        action = "none"
        switch_error = None

        if mode == "primary" and fails >= self.cfg.fail_threshold:
            if in_cooldown:
                action = "cooldown"
            else:
                try:
                    r = self.switch_target(t, to_backup=True)
                    # dry-run 只是演练，不改变真实状态机
                    if not self.dry_run:
                        mode = "backup"
                        last_switch = now
                    action = "switch_to_backup"
                except Exception as e:
                    # 切换失败（比如凭据缺失、API 报错）**不能**当成切换成功，
                    # 也不该丢掉计数 —— 否则状态不落盘，fails 永远涨不上去，
                    # 引擎就再也试不成功了。保持 mode 不变，下一轮重试。
                    switch_error = str(e)[:300]
                    action = "switch_failed"
                    log("  切换失败 %s %s: %s" % (t.get("zone"),
                                                t.get("record_name"), e))
        elif mode == "backup" and oks >= self.cfg.recover_threshold:
            if in_cooldown:
                action = "cooldown"
            else:
                try:
                    r = self.switch_target(t, to_backup=False)
                    if not self.dry_run:
                        mode = "primary"
                        last_switch = now
                    action = "switch_to_primary"
                except Exception as e:
                    switch_error = str(e)[:300]
                    action = "switch_failed"
                    log("  切回失败 %s %s: %s" % (t.get("zone"),
                                                t.get("record_name"), e))

        # ---- 漂移自愈 ----
        # 状态机只负责「切换时机」，不管「当前值对不对」。如果记录被人在
        # 控制台手动改掉、或被第三方 API 改动，状态机会一直停在 backup /
        # primary 模式不动，站点就一直指着错的地方。
        # 这里每轮巡检都核对一次「当前值 == 该模式应有的 IP」，不一致就拉回来。
        if action == "none" and not self.dry_run:
            expect_ip = (self.cfg.backup_ip if mode == "backup"
                         else self.cfg.primary_ip)
            if expect_ip:
                try:
                    prov = self.provider(t["provider"])
                    cur = self._peek_value(t, prov)
                    if cur and cur != expect_ip:
                        dextra = {}
                        if t["provider"] in ("dnspod", "tencentcloud"):
                            dextra = {"rec_line": t.get("record_line", "默认")}
                        elif t["provider"] == "cloudflare":
                            # 必须带上 proxied，否则 CF 记录会被改成不代理，
                            # 浏览器侧证书与加速都会变。
                            dextra = {"proxied": bool(t.get("proxied", False))}
                        try:
                            prov.set_record(t["zone"],
                                            t.get("record_name") or "",
                                            expect_ip,
                                            rec_type=t.get("type", "A"),
                                            ttl=int(t.get("ttl", 60)), **dextra)
                        except TypeError:
                            prov.set_record(t["zone"],
                                            t.get("record_name") or "",
                                            expect_ip,
                                            rec_type=t.get("type", "A"),
                                            ttl=int(t.get("ttl", 60)))
                        log("  漂移自愈 %s %s: %s -> %s"
                            % (t.get("zone"), t.get("record_name"),
                               cur, expect_ip))
                        action = "repair_drift"
                except Exception as e:
                    log("  漂移自愈失败 %s %s: %s"
                        % (t.get("zone"), t.get("record_name"), e))

        self.state.set(key, {"fails": fails, "oks": oks, "mode": mode,
                             "last_switch": last_switch,
                             "last_check": now,
                             "last_ok": bool(res["ok"]),
                             "last_detail": res["detail"]})
        out = {"target": key, "ok": res["ok"], "action": action,
               "mode": mode, "detail": res["detail"]}
        if self.dry_run:
            out["dry_run"] = True
        if switch_error:
            out["switch_error"] = switch_error
        return out

    # ---------- 全量 ----------
    def run(self) -> List[Dict[str, Any]]:
        out = []
        # 每轮先做一次 TCP 探活（3s 超时）。整站宕机时这一条就能定性，
        # 后面的逐条 HTTPS 检查会走 check_target 里的快路径直接判失败。
        self._primary_tcp_ok = True
        if self.cfg.primary_ip:
            self._primary_tcp_ok = check_tcp(self.cfg.primary_ip, 443,
                                             timeout=3)
            if not self._primary_tcp_ok:
                log("主站 %s:443 不可达，本轮所有目标直接判失败（快路径）"
                    % self.cfg.primary_ip)
        for t in self.cfg.targets:
            try:
                out.append(self.handle_target(t))
            except Exception as e:
                log("target %s 处理异常: %s" % (t.get("zone"), e))
                out.append({"target": str(t.get("zone")), "ok": False,
                            "action": "error", "error": str(e)[:300]})
        return out

    def status(self) -> List[Dict[str, Any]]:
        rows = []
        for t in self.cfg.targets:
            key = "%s|%s" % (t["zone"], t.get("record_name", "@"))
            st = self.state.get(key, {}) or {}
            rows.append({
                "target": key,
                "zone": t["zone"],
                "record": t.get("record_name", "@"),
                "provider": t["provider"],
                "mode": st.get("mode", "primary"),
                "fails": st.get("fails", 0),
                "oks": st.get("oks", 0),
                "last_ok": st.get("last_ok"),
                "last_check": st.get("last_check"),
                "last_detail": st.get("last_detail"),
            })
        return rows

# ===== CLI =====

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


import argparse
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional




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
    elif args.cmd == "daemon":
        cmd_daemon(args.interval)


if __name__ == "__main__":
    main()