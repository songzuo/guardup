# GuardUp

**Automatic website failover. One command. Your DNS keys stay yours.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://python.org)

GuardUp watches your origin server and, when it goes down, automatically repoints your DNS A record to a standby server. When your origin recovers, it switches back. It works across **Cloudflare, Aliyun DNS, DNSPod, and Tencent Cloud** from a single agent.

**Repo:** https://github.com/songzuo/guardup · **Site:** https://guard.7zi.com

> **Your DNS credentials never leave your machine.** The agent runs on your own standby server and talks to your DNS provider directly. There is no control plane to trust with write access to your DNS.

> **Need more than DNS?** [`keep/`](keep/README.md) is the commercial full-stack line — file + database + application takeover with consistent snapshots and auto-rollback (P2 prototype). This free agent stays DNS-only by design.

---

## Why GuardUp

Most "DNS failover" tools either lock you into one provider or ask you to hand over your DNS API keys to a third party. GuardUp is different:

| | GuardUp | Typical SaaS |
|---|---|---|
| DNS credentials | Stay on your server (BYOK) | Uploaded to their cloud |
| Provider lock-in | Cloudflare + Aliyun + DNSPod + Tencent | Usually one provider |
| Works if vendor goes down | **Yes** — logic runs locally | No |
| False-positive protection | Direct-IP health checks + cooldown + drift repair | Varies |
| Install | One command, 3 questions | Dashboard signup + config |

**Honest RTO**: 1–10 minutes depending on your DNS plan's TTL floor. We don't promise "instant" — free tiers of Aliyun/DNSPod enforce a 600s TTL minimum. Honesty is the product.

---

## Quick start (≈ 2 minutes)

```sh
curl -fsSL https://guard.7zi.com/install.sh | sh
```

Then:

```sh
guardup init     # 3 questions — auto-detects your DNS provider
guardup drill    # dry-run: reads current state, changes nothing
guardup daemon   # start continuous health checks
```

`init` asks only:
1. Your domain name
2. Your standby server IP
3. Your DNS API token (stored locally in `~/.guardup/credentials.env`, chmod 600)

It **auto-detects your DNS provider** by querying the authoritative nameservers — you never see the words "TTL", "A record", or "NS".

---

## Commands

| Command | What it does |
|---|---|
| `guardup init` | Interactive setup wizard (3 questions) |
| `guardup status` | One screen: health, current target, last switch, thresholds |
| `guardup check` | Run one health-check cycle now |
| `guardup switch` | Manually fail over to standby |
| `guardup restore` | Manually switch back to origin |
| `guardup drill` | Dry-run — reads only, never modifies DNS |
| `guardup daemon --interval 60` | Run continuous checks |
| `guardup dns list --zone example.com` | List every record in a zone (read-only) |
| `guardup dns ensure --zone example.com --name www --value 1.2.3.4 [--apply]` | Idempotently create/fix one record — dry-run by default |

---

## How it avoids false positives

These are hard-won patches from running this in production:

- **Direct-IP health checks** — checks your origin by IP (with the domain in the Host header), not through DNS. Otherwise after failover you'd check the standby, see it's healthy, and flap back and forth forever.
- **Threshold + cooldown** — 2 consecutive failures to fail over, 3 consecutive successes to recover, 600s cooldown between switches.
- **Drift repair** — if someone changes the DNS record by hand in the console, the agent pulls it back to the correct target on the next cycle.
- **TTL floor clamp** — Aliyun/DNSPod free tiers reject TTL < 600; the agent clamps automatically so a switch never silently fails.
- **Dry-run is truly read-only** — `drill` only reads current values. (An early version wrote DNS during "dry-run"; fixed and regression-tested.)
- **Failed switches keep their counters** — a failed API write doesn't reset the failure count, so the next cycle can retry.

---

## Running as a service

```ini
# /etc/systemd/system/guardup.service
[Unit]
Description=GuardUp failover agent
After=network-online.target

[Service]
ExecStart=%h/.local/bin/guardup daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now guardup
```

---

## Supported DNS providers

| Provider | Credentials env var |
|---|---|
| Cloudflare | `CF_API_TOKEN` |
| Aliyun DNS | `ALICLOUD_ACCESS_KEY_ID` + `ALICLOUD_ACCESS_KEY_SECRET` |
| DNSPod | `DNSPOD_LOGIN_TOKEN` (`ID,TOKEN`) |
| Tencent Cloud | `TENCENT_SECRET_ID` + `TENCENT_SECRET_KEY` |

---

## License & business model

GuardUp is **AGPL-3.0** licensed. The core agent is open source — audit it, fork it, self-host it.

A hosted control plane (multi-site dashboard, multi-region confirmation, alerting, switch reports, SLA) is offered separately as a paid service. Need to embed GuardUp in a closed-source product? A commercial license is available.

This is the open-core model used by GitLab and Grafana: the code is the trust signal, the operations are the product.

See [LICENSE](LICENSE) for details.

---

## Status

Early development (v0.1.0). Core engine is battle-tested; product packaging is in progress. Issues and PRs welcome.
