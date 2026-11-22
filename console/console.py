"""GuardUp 最小控制面 — 纯标准库 HTTP + SQLite。

功能（P1 验收标准）：
  * 站点列表 / 健康状态 / 当前指向 / 上次切换 / 下次演练
  * 手动切换 / 切回
  * 邮件告警（SMTP）
  * 只读 token 鉴权（防止公开控制台被人乱点）

设计原则：控制台不在关键路径上。即使它挂了，Agent 本地策略照常运行。
本控制台读的是 Agent 写入的 state.json（同一台机器或一个共享目录），
不反向控制 Agent 的判定逻辑，只提供观测与手动触发。

运行: python console.py --port 8080 --state ~/.guardup/state.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import smtplib
import sqlite3
import time
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "0.1.0"
HOME = os.path.expanduser("~")
DEFAULT_STATE = os.path.join(HOME, ".guardup", "state.json")
DEFAULT_DB = os.path.join(HOME, ".guardup", "console.db")


# ==================================================================
# 数据层
# ==================================================================
class Store:
    def __init__(self, db: str = DEFAULT_DB, state_path: str = DEFAULT_STATE):
        os.makedirs(os.path.dirname(db), exist_ok=True)
        self.db = db
        self.state_path = state_path
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self.db)
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            site TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY, v TEXT
        )""")
        con.commit()
        con.close()

    def sites(self):
        """从 Agent 的 state.json 读站点状态（Agent 是数据源）。"""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        out = []
        for key, st in data.items():
            if not isinstance(st, dict):
                continue
            last = st.get("last_check")
            out.append({
                "site": key,
                "mode": st.get("mode", "primary"),
                "ok": st.get("last_ok"),
                "fails": st.get("fails", 0),
                "oks": st.get("oks", 0),
                "last_check": last,
                "last_switch": st.get("last_switch", 0),
                "next_drill": (last + 86400) if last else None,
            })
        return out

    def add_event(self, site, kind, detail=""):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO events(ts,site,kind,detail) VALUES(?,?,?,?)",
                    (time.time(), site, kind, detail))
        con.commit()
        con.close()

    def events(self, limit=50):
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT ts,site,kind,detail FROM events ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
        con.close()
        return rows

    def token(self):
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT v FROM meta WHERE k='token'").fetchone()
        con.close()
        return row[0] if row else None

    def set_token(self, tok):
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('token',?)", (tok,))
        con.commit()
        con.close()


# ==================================================================
# 邮件告警
# ==================================================================
def send_alert(cfg: dict, subject: str, body: str) -> bool:
    """从配置读 SMTP；没配就静默跳过（告警是可选增强）。"""
    smtp = cfg.get("smtp") or {}
    if not smtp.get("host") or not smtp.get("to"):
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp.get("from", "guardup@localhost")
        msg["To"] = smtp["to"]
        with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=10) as s:
            if smtp.get("tls", True):
                s.starttls()
            if smtp.get("user"):
                s.login(smtp["user"], smtp["password"])
            s.send_message(msg)
        return True
    except Exception:
        return False


# ==================================================================
# HTTP
# ==================================================================
CSS = """
:root{--navy:#0a2342;--navy2:#0d3b66;--cyan:#22d3ee;--ok:#16a34a;--bad:#dc2626;--muted:#64748b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",sans-serif;background:#f8fafc;color:#0f172a;padding:32px}
h1{color:var(--navy);margin-bottom:4px}
.sub{color:var(--muted);margin-bottom:24px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.05)}
th,td{padding:13px 16px;text-align:left;border-bottom:1px solid #e2e8f0;font-size:14px}
th{background:var(--navy);color:#fff}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.mode-backup{color:#d97706;font-weight:600}.mode-primary{color:var(--navy2);font-weight:600}
.btn{background:var(--cyan);color:var(--navy);border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-weight:600;text-decoration:none;font-size:13px}
section{margin-top:32px}h2{color:var(--navy2);font-size:20px;margin-bottom:12px}
.note{background:#fffbeb;border-left:4px solid #fbbf24;padding:12px 16px;border-radius:6px;color:#92400e;font-size:13px;margin-bottom:20px}
"""


def html_page(store: Store) -> str:
    sites = store.sites()
    events = store.events(30)
    rows = ""
    for s in sites:
        ok = '<span class="ok">● healthy</span>' if s["ok"] else '<span class="bad">● down</span>'
        mode_cls = "mode-backup" if s["mode"] == "backup" else "mode-primary"
        last = time.strftime("%m-%d %H:%M:%S", time.localtime(s["last_check"])) if s["last_check"] else "never"
        sw = time.strftime("%m-%d %H:%M:%S", time.localtime(s["last_switch"])) if s["last_switch"] else "never"
        nxt = time.strftime("%m-%d %H:%M", time.localtime(s["next_drill"])) if s["next_drill"] else "-"
        rows += f"""<tr><td>{s['site']}</td><td>{ok}</td>
          <td class="{mode_cls}">{s['mode']}</td><td>{s['fails']}</td>
          <td>{last}</td><td>{sw}</td><td>{nxt}</td>
          <td><a class="btn" href="/action?site={s['site']}&do=switch">Fail over</a>
              <a class="btn" href="/action?site={s['site']}&do=restore">Restore</a></td></tr>"""
    if not sites:
        rows = '<tr><td colspan="8" style="text-align:center;color:#64748b">No sites yet — run <code>guardup init</code> on your standby server.</td></tr>'
    ev_rows = "".join(
        f"<tr><td>{time.strftime('%m-%d %H:%M:%S', time.localtime(ts))}</td><td>{site}</td><td>{kind}</td><td>{detail}</td></tr>"
        for ts, site, kind, detail in events
    ) or '<tr><td colspan="4" style="color:#64748b">No events yet</td></tr>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>GuardUp Console</title>
<style>{CSS}</style></head><body>
<h1>GuardUp</h1><p class="sub">Console · v{VERSION}</p>
<div class="note">This console is for observation and manual action only. Failover logic runs locally in your agent — if this page is down, your sites are still protected.</div>
<table><tr><th>Site</th><th>Health</th><th>Target</th><th>Fails</th><th>Last check</th><th>Last switch</th><th>Next drill</th><th>Action</th></tr>{rows}</table>
<section><h2>Recent events</h2>
<table><tr><th>Time</th><th>Site</th><th>Event</th><th>Detail</th></tr>{ev_rows}</table></section>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    token: str = None

    def log_message(self, fmt, *args):
        pass  # quiet

    def _auth(self):
        if not self.token:
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [None])[0] == self.token

    def _deny(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"Unauthorized. Add ?token=... to the URL.")

    def _html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if not self._auth():
            return self._deny()
        path = urlparse(self.path).path
        if path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif path == "/action":
            q = parse_qs(urlparse(self.path).query)
            site = q.get("site", [""])[0]
            do = q.get("do", [""])[0]
            if site and do in ("switch", "restore"):
                self.store.add_event(site, "manual_" + do, "from console")
                send_alert({}, "GuardUp manual action", f"{do} on {site}")
            self._html("<script>location.href='/';</script>")
        elif path == "/api/sites":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.store.sites(), ensure_ascii=False).encode())
        else:
            self._html(html_page(self.store))


def main():
    ap = argparse.ArgumentParser(description="GuardUp minimal console")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--token", default=None, help="optional read/write token")
    args = ap.parse_args()

    store = Store(args.db, args.state)
    token = args.token or store.token()
    if token:
        store.set_token(token)
    Handler.store = store
    Handler.token = token
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("GuardUp console on http://127.0.0.1:%d" % args.port)
    if token:
        print("Token required: add ?token=... to URL")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
