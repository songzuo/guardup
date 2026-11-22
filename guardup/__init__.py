"""failover —— DNS 主备切换引擎。

模块构成：
    engine            健康检查 + 状态机 + 切换调度
    providers.*       Cloudflare / 阿里云 / DNSPod 三套 DNS API 适配
    acme_auth.py      certbot DNS-01 hook（写入 TXT 挑战）
    acme_cleanup.py   certbot DNS-01 cleanup hook（删除 TXT 挑战）
"""

__version__ = "1.0.0"
