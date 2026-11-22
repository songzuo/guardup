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

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .providers import get_provider

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


DEFAULT_CONFIG = _default_path("config.json")
DEFAULT_STATE = _default_path("state.json")
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
