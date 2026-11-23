# GuardUp Keep — full-stack hot standby (P2 prototype)

> Commercial line of the GuardUp family. The free open-source `guardup/` agent
> stays a DNS failover tool; **Keep** is the paid full-stack line: file + database
> + application takeover + DNS, driven by declarative profiles and zero guessing.

**Status: P2 prototype.** Core logic is real and tested (9/9 with a real SQLite
WAL database), but the transport layer (sync across two hosts) and the hosted
control plane are not built yet. See `FULL-STACK-PLAN-v2.md` for the roadmap.

## Why this exists

The hard part of "zero-touch hot standby" is not switching DNS — it's the
boring, battle-scarred details:

- finding the real document root by **asking nginx**, not hard-coding a guess
  (and never picking the ACME challenge dir as the site root — that once turned
  a real site into a 2,988-byte placeholder);
- taking a **consistent SQLite snapshot** with the backup API, never copying a
  live `.db` file in WAL mode;
- refusing to install a bad snapshot — `integrity_check` + non-empty business
  tables, because a broken DB must never overwrite the working one;
- detecting "no real change" by **logical fingerprint** rather than file bytes,
  so a backup that only reshuffles pages doesn't cause a needless service restart;
- watching content with a golden snapshot so a bad deploy can't silently degrade.

Those are the moat. `keep/` turns them from one-off shell scripts into reusable
modules.

## Modules

| Module | Role | Battle-tested origin |
|---|---|---|
| `profile.py` | Declarative service tiers (static / warm / stateful) — users fill JSON, never write scripts | `standby-services.json` |
| `discover.py` | Auto-discover nginx docroots + SQLite DB paths; server-level roots only, ACME excluded | `discover_roots.py` |
| `gate.py` | Consistent snapshot → verify → logical-change check → swap → healthcheck → auto-rollback | `sync_apigw.sh` |
| `watch.py` | Golden-snapshot content guard against degradation | `guard_mainlander.sh` |
| `keep.py` | Orchestration CLI (`init` / `plan` / `sync` / `watch`) | new |

## Quick start (prototype)

```bash
python keep.py init                 # generate profile.json (defaults cover 95%)
python keep.py plan                 # auto-discover docroots + DB, print a human plan
python keep.py sync --dry-run       # snapshot + verify, show what WOULD change
python keep.py sync                 # really swap + healthcheck (auto-rollback on fail)
python keep.py watch --save         # record golden snapshots
python keep.py watch                # check for degradation
```

`healthcheck` is injected by the orchestration layer (real deployment wires in
`curl` + `systemctl`); the prototype defaults to "DB opens and passes
`integrity_check`". Drill the rollback path with `--health-fail`.

## Test

```bash
python tests/test_keep.py
```

Real SQLite in WAL mode, real backup API, deliberately corrupted DB, forced
healthcheck failure — no mocked core logic. 9/9 passing.

## Boundaries (deliberate)

- **Website-level**, not server-level: no Windows/K8s whole-machine DR. That is a
  different company's cost structure.
- **"Zero-touch" means zero-touch + one assisted onboarding** ($99): self-hosted
  still needs one install command on each of two machines. Don't promise
  "click three times and everything is done".
- **Open-source line stays DNS-only.** Full-stack power does not go into the
  public agent repo — otherwise personal users can't run it and blame the
  product, and the moat is given away for free.
