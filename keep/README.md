# GuardUp Keep — full-stack hot standby (P2 prototype)

> Commercial line of the GuardUp family. The free open-source `guardup/` agent
> stays a DNS failover tool; **Keep** is the paid full-stack line: file + database
> + application takeover + DNS, driven by declarative profiles and zero guessing.

**Status: P4 + self-system upgrade (P0 + P1 done).** Core logic is real and tested
(69/69 across 9 suites). The three gaps from the v2 plan are covered:
dynamic-app takeover (`app.py`), real DB replica (`db.py`), and de-singled
control plane with multi-region voting (`quorum.py`). Plus a payment adapter
(`checkout.py`). `bidir.py` adds failback (two-way sync). Two modules flow product
capability back into our own battle-tested failover system: `db_live_sync.py`
(P0 — DB replica, our `SYNC_DB` was never enabled) and `quorum_live.py`
(P1 — the missing transport layer for multi-region voting: shared SQLite lease +
probe aggregation + fencing gate). See `FULL-STACK-PLAN-v2.md`,
`../SELF-SYSTEM-ASSESSMENT.md`, `QUORUM-LIVE.md`.

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
| `db_live_sync.py` | **Self-system P0 upgrade**: consistent DB snapshot + verify + PITR + restore drill; dry-run by default | flows `db.py` back to `sync.sh` |
| `quorum_live.py` | **Self-system P1 upgrade**: shared SQLite lease (CAS fencing) + multi-bot probe aggregation + go/no-go gate; state persists across timer rounds | fills `quorum.py`'s missing transport layer |
| `app.py` | Next.js/dynamic-site takeover: artifact sync → versioned swap → real HTTP healthcheck → same-port rollback | new |
| `db.py` | PG/MySQL real replica: consistent dump → verify → **replication-lag gate** → PITR catalog → apply/rollback | `--allow-stale` gate |
| `quorum.py` | De-single-point: multi-region vote (quorum/anti-flap/drift-repair) + dual control-plane lease with fencing | new |
| `checkout.py` | Payment adapter: Creem primary / Dodo fallback, single source of pricing truth, no money/keys touched | new |
| `bidir.py` | **Bidirectional sync / failback**: change tracking (shadow table + triggers), 3-way merge (base+local+remote), conflict policy, tombstone deletes, cross-host changeset bridge | new |
| `keep.py` | Orchestration CLI (`init` / `plan` / `sync` / `watch` / `db` / `quorum` / `checkout` / `bidir`) | new |

### Bidirectional sync (failback) — why it matters

One-way sync handles *failing over*, but not *failing back*. When the primary
goes down and the standby takes traffic, the standby produces **real new data**
(orders, signups, config). When the primary comes back with stale data, copying
it over the standby would silently lose everything written during the outage.
`bidir.py` reverse-merges the standby's incremental changes back into the
recovered primary using a three-way merge against a failover-time `base`
snapshot. See `BIDIRECTIONAL.md`.

## Quick start (prototype)

```bash
python keep.py init                 # generate profile.json (defaults cover 95%)
python keep.py plan                 # auto-discover docroots + DB, print a human plan
python keep.py sync --dry-run       # snapshot + verify, show what WOULD change
python keep.py sync                 # really swap + healthcheck (auto-rollback on fail)
python keep.py watch --save         # record golden snapshots
python keep.py watch                # check for degradation
python keep.py db gate --lag 5      # replication-lag gate (auto-allow if within SLA)
python keep.py db pitr --dir ./pitr # list point-in-time snapshots
python keep.py quorum               # demo: multi-region vote + lease fencing
python keep.py checkout --plan guard # 3-tier pricing + dual checkout links
# bidirectional failback (see BIDIRECTIONAL.md)
python keep.py bidir snapshot --db app.db --out base.db   # failover-time base
python keep.py bidir track --db standby.db --table orders          # one-time
python keep.py bidir export --db standby.db --table orders --out ch.json
python keep.py bidir merge --base base.db --local app.db --changeset ch.json --dry-run
```

`healthcheck` is injected by the orchestration layer (real deployment wires in
`curl` + `systemctl`); the prototype defaults to "DB opens and passes
`integrity_check`". Drill the rollback path with `--health-fail`.

## Test

```bash
python tests/test_keep.py       # 9  core gate/discover/watch/profile
python tests/test_app.py        # 7  dynamic-app takeover
python tests/test_db.py         # 7  PG/MySQL replica + lag gate + PITR
python tests/test_quorum.py     # 7  multi-region vote + lease fencing
python tests/test_checkout.py   # 6  payment adapter
python tests/test_bidir.py      # 9  bidirectional failback (real triggers)
python tests/test_db_live_sync.py # 9 self-system P0 DB upgrade (backup API + drill)
python tests/test_quorum_live.py # 9 self-system P1 vote transport (shared lease + gate)
# plus ../tests/test_guardup.py # 6  open-source DNS engine
```

Real SQLite in WAL mode, real backup API (incl. under concurrent writes),
deliberately corrupted DB, forced healthcheck failure, real restore drill,
cross-round vote state persistence, cross-process fencing CAS —
no mocked core logic. **69/69 passing** across 9 suites.

## Boundaries (deliberate)

- **Website-level**, not server-level: no Windows/K8s whole-machine DR. That is a
  different company's cost structure.
- **"Zero-touch" means zero-touch + one assisted onboarding** ($99): self-hosted
  still needs one install command on each of two machines. Don't promise
  "click three times and everything is done".
- **Open-source line stays DNS-only.** Full-stack power does not go into the
  public agent repo — otherwise personal users can't run it and blame the
  product, and the moat is given away for free.
