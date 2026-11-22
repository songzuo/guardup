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

from __future__ import annotations

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
