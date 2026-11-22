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

