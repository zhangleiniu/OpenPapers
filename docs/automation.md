# Automation and monitoring

This page describes the deployed automation boundary. The target design and
its component reference are in [`automation-system/`](./automation-system/README.md);
day-to-day procedures are in
[`automation-system/operations.md`](./automation-system/operations.md).
Repository documentation is not proof of live health: inspect the actual
LaunchDaemons, bounded records, and cloud schedule before an operational
change. The dated deployment snapshot lives in
[`automation-system/current-handoff.md`](./automation-system/current-handoff.md).

## What runs in production

One marker-gated system LaunchDaemon on the maintainer's Mac
(`org.openpapers.local-control`, dedicated role `_openpapers`) is the sole
production writer. It wakes hourly and exits after a bounded invocation:

1. It validates the private production configuration, its integrity-marker
   chain, restored monitor state, and the external-volume safety gate.
2. Once daily at or after 08:00 America/Chicago it runs the deterministic
   source monitor and sends one TLS SMTP email per source change or error.
   Exact daily replay does not repeat the monitor or its notifications.
3. On every wakeup it runs the local SQLite due selector. When persisted
   work is due, the enabled bounded composition may initialize one event
   date, run one coding agent, attempt one durable report, and apply
   bounded worktree retention.
4. A failed wake records a bounded, secret-free failure category; three
   consecutive failures email the monitor recipients, then about daily
   while broken.

External effects are gated by the installed agent-control configuration and
each carry separate budgets, cooldowns, a global concurrency slot, and a
systemic-failure circuit. The retained Cloud Scheduler/Cloud Run monitor is
paused and exists only as a rollback path; it must never run concurrently
with the local service (rollback order and the single-writer rule are at the
end of this page).

## Deterministic source monitor

`automation/conferences.json` is the versioned source registry; every
catalog venue has at least one registered source. Runtime hashes, counts,
and immutable snapshots are stored separately (under
`$SCRAPER_DATA_ROOT/monitor/` for manual use; the installed service uses its
private monitor tree):

```bash
python automation/monitor.py
python automation/monitor.py --venue icml --year 2026
python automation/monitor.py --no-write
```

Detectors: `openreview_api` (accepted-note IDs for an invitation, filtered
by venueid), `official_html` (CSS-selected item count on a page), and
`pmlr_volume` (a matching proceedings listing). JMLR's entry is a loose
continuous-volume-size proxy rather than a discrete availability signal.

The monitor is cheap operational coverage, not readiness authority: a change
or a date is only a scheduling hint. A changed available source may advance
an existing configured future agent check to its cooldown boundary; it can
never create work, claim an agent, or bypass the due-policy gates.

## Scheduling and the coding agent

`automation/local_scheduler.py` holds the single-writer lease and selects
only records whose persisted `next_check_at` is due — a frequent wakeup does
not mean venues are checked frequently. For each enrolled venue/year:

1. One Gemini Search call estimates the approximate event date (a
   scheduling hint, never readiness proof).
2. At or after that date, Codex runs in an isolated no-remote worktree,
   decides readiness itself, may repair and run the scraper, and returns
   `success`, `not_ready` (with an optional evidence-based UTC retry),
   `needs_human`, or `failed`. It never commits, pushes, merges, or deploys;
   a human reviews the worktree.
3. Every terminal run composes one replay-safe report email through the
   Resend adapter with durable idempotent delivery state.

The tracked cohort (`automation/config/agent_targets.v1.json`, schema 3)
lists the 13 formulaic annual/periodic venues plus manually confirmed
one-off editions in `extra_targets` (currently NAACL 2027). ICCV and ECCV
carry `interval_years`/`cycle_anchor_year` so they are only scheduled for
years they occur in. Continuous JMLR is visible but not enrolled; it needs
recurring, non-terminal success semantics. A venue/year whose canonical
scrape predates enrollment can be closed with
`automation.agent_operations mark-completed` (see the operations runbook).

## Status surfaces

- Change/error and run-report emails, plus the wake-failure alert.
- `automation.agent_status`: a secret-free read-only summary CLI for
  enabled production, including the strict private two-canary proof format.
- The read-only dashboard: loopback backend (`automation.agent_dashboard`)
  behind an authenticated NIU-private HTTPS Caddy proxy at
  `https://archer.cs.niu.edu:8443/` (username `openpapers`; password
  operator-held). It shows one row per catalog venue with a color-coded
  countdown to the next check, the last real download date from the local
  dataset tree, disposition, and report state. It exposes no control
  methods, paths, addresses, or credentials. Its DigiCert leaf expires
  2026-12-03 and is renewed manually.

None of these surfaces can claim work or change a gate. Do not weaken the
private SQLite file permissions or copy private paths, credentials,
recipient addresses, or agent explanations into a public log.

## Discovery adapter

```bash
python -m automation.run_discovery --venue icml
python -m automation.run_discovery --live --venue icml --year 2026
```

The ordinary command uses fixtures; `--live` requires explicit
authorization and Application Default Credentials. `GeminiEventDateProvider`
is the production date estimator; the stricter citation-backed discovery
output is not wired into scheduling. This use is separate from Gemini track
classification in some core scrapers
([`GOOGLE_CLOUD_SETUP.md`](./GOOGLE_CLOUD_SETUP.md)).

## Email boundaries

The monitor and the wake-failure alert use TLS SMTP. Agent-run reports use
the Resend HTTPS adapter with a provider idempotency key and an explicit
recipient allowlist (policy stores only address fingerprints). Notification
failure is retryable delivery state and never changes a run outcome.

## Cloud rollback path

`automation/prefect_flows.py` and `automation/run_monitor_flow.py` implement
the retained Cloud Run monitor (deployment assets:
[`automation/deployment/README.md`](../automation/deployment/README.md)).
Strict rollback order: stop and verify the local LaunchDaemon; resume only
the exact retained cloud schedule; verify cloud recovery. Local activation
uses the inverse order. Never run both writers.

## Historical material

The abandoned deterministic-verification design (P0–P6) is archived under
[`automation-system/archive/`](./automation-system/archive/README.md).
Deployment history lives in git history and dated ExecPlans, not in this
page. Ignored `docs/local-p4*-operations.md` files on the production Mac are
private operational records, not public guidance.
