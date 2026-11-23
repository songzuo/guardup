# Product Hunt — launch kit (ready to paste)

## Tagline (max 60 chars)

```
Automatic DNS failover, with your keys on your server
```

## Short description (max 260 chars)

```
GuardUp watches your origin and switches DNS to a standby the moment it dies. Your DNS credentials stay on your own machine, never uploaded. One command, 3 questions, works with Cloudflare/Aliyun/DNSPod/Tencent.
```

## First comment

```
Hey Product Hunt 👋 — I'm songzuo, the maker of GuardUp.

I built this after one too many 3am pages. The thing that bugged me about
existing failover tools: they make you upload your DNS API keys to their cloud.
You're trusting a third party with control of your entire domain — and when
their control plane hiccups, your failover disappears right when you need it.

GuardUp runs on your own standby server. It checks your origin by direct IP,
and when it fails twice in a row, repoints your DNS automatically. Your keys
never leave your machine (chmod 600, open-source agent you can audit).

Setup really is three questions — domain, standby IP, one DNS token — and there's
a dry-run mode so nothing touches your records until you're ready.

Honest note: failover takes 1–10 min depending on your DNS TTL (some free tiers
force a 600s floor). No "instant" magic, just certainty when it counts.

I'll be here all day — Ask me anything, especially about the failover edge cases.
```

## Gallery / screenshots to capture (from the real console)

1. The 3-question install wizard (`guardup.py` interactive).
2. `guardup.py status` showing origin/standby state.
3. A real dry-run failover output (green, no changes applied).
4. The comparison table from guard.7zi.com/#why.
5. Logo / OG image in `assets/`.

## Links

- Site: https://guard.7zi.com
- GitHub: https://github.com/songzuo/guardup
- Install: https://guard.7zi.com/install.sh

## Launch notes

- Post in the **Developer Tools** / **Infrastructure** category.
- Best time: Tue–Thu, 00:01 PT (PH day reset) — schedule via PH dashboard.
- Reply to every comment within the first 2h (PH ranking weights maker replies).
- The paid full-stack line (Keep) is deliberately **not** the PH hook — lead with
  the free, auditable DNS agent to earn trust; mention Keep only if asked.
