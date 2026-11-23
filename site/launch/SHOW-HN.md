# Show HN — launch draft (ready to paste)

**Title (exact):**

```
Show HN: GuardUp – DNS failover that keeps your API keys on your own server
```

**URL:** https://guard.7zi.com

---

## Body

Hi HN,

I built GuardUp after getting paged one too many times for a server that died
at 3am while I was the only one awake to fix it.

Most DNS failover SaaS products ask you to **upload your DNS API keys to their
cloud**. That always bothered me: you're handing the keys to your entire domain
to a third party, and if their control plane goes down, your failover story
goes down with it (I've watched it happen).

GuardUp flips that around. The agent runs on **your own standby server**, talks
to your DNS provider directly, and your credentials never leave your machine
(stored chmod 600). The logic is a local state machine — no hosted dependency
in the critical path. It's open source (AGPL-3.0), so you can read every line
before trusting it with your DNS.

What it does:
- Health-checks your origin by **direct IP** (not through DNS, so it can't fool
  itself), requires 2 consecutive failures, then repoints DNS to your standby.
- Anti-flapping: cooldown + drift repair, so a flaky origin doesn't cause a
  ping-pong between IPs.
- Works with Cloudflare, Aliyun DNS, DNSPod, and Tencent Cloud — provider is
  auto-detected via DoH, no config guessing.
- Switches back automatically when your origin recovers.

Setup is three questions (your domain, your standby IP, one DNS token). There's
a `dry-run` so you can see what it *would* do before you let it touch anything.

The honest bit: ** failover is 1–10 minutes**, dominated by your DNS TTL. Some
providers' free tiers enforce a 600s floor. I don't sell "instant" — I sell
knowing the switch will actually happen when it matters.

Command to try it:
```
curl -fsSL https://guard.7zi.com/install.sh | sh
```

I also built a paid full-stack line (GuardUp Keep) on the same engine — file +
database + app takeover for people who need more than DNS. Happy to talk about
that if there's interest, but the DNS agent is free and self-hostable on its own.

This is v1 — I'd love brutal feedback on the failover logic, especially edge
cases around provider APIs and TTL handling. I'm here all day.

— songzuo
