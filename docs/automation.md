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
3. On every wakeup the agent composition reads due state under the local
   SQLite single-writer lease. When persisted work is due, it may initialize one event
   date, run one coding agent, attempt one durable report, and apply
   bounded worktree retention.
4. A failed wake records a bounded, secret-free failure category; three
   consecutive failures email the monitor recipients, then about daily
   while broken.

External effects are gated by the installed agent-control configuration and
each carry separate budgets, cooldowns, a global concurrency slot, and a
systemic-failure circuit. The Mac LaunchDaemon is the sole production
writer; there is no other writer to coordinate with (see "Retired cloud
rollback path" at the end of this page).

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

`automation/control_state.py` holds the single-writer lease; `event_dates.py`
and `due_policy.py` select only records whose persisted `next_check_at` is due — a frequent wakeup does
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
  enabled production.
- The read-only dashboard: loopback backend (`automation.agent_dashboard`)
  behind an authenticated NIU-private HTTPS Caddy proxy at
  `https://archer.cs.niu.edu:8443/` (username `openpapers`; password
  operator-held). It shows one perpetual-cycle row per catalog venue: the
  last held edition and next expected edition (curated dates merged with the
  control state's own estimates; `~` marks a cadence approximation), the
  scheduler's next attempt, and a color-coded countdown that rolls to the
  next edition once a collection completes. Timestamps default to
  America/Chicago with a client-side timezone selector (the page's single
  inline script; no external resource is ever loaded). It exposes no
  control methods, paths, addresses, or credentials. Its DigiCert leaf
  expires 2026-12-03 and is renewed manually.

  The venue monitor badge means only “present in the tracked registry”; it is
  not live monitor-health evidence. Schema-2 date provenance and the
  target-selection fixes were deployed with the 2026-07-18 schema-11 runtime.

None of these surfaces can claim work or change a gate. Do not weaken the
private SQLite file permissions or copy private paths, credentials,
recipient addresses, or agent explanations into a public log.

## Date estimation

`automation/providers/gemini.py::GeminiEventDateProvider` is the production
date estimator (`automation/event_dates.py` calls it once per venue/year
before it is due). A stricter citation-backed discovery pipeline with its
own budget ledger, artifact store, and two-provider escalation used to live
alongside it (`automation/run_discovery.py`) but was never wired into
scheduling and was removed on 2026-07-18; `automation/discovery.py` now
holds only the small request/error contract both `event_dates.py` and the
Gemini provider still share. This is separate from Gemini track
classification in some core scrapers
([`GOOGLE_CLOUD_SETUP.md`](./GOOGLE_CLOUD_SETUP.md)).

## Email boundaries

The monitor and the wake-failure alert use TLS SMTP. Agent-run reports use
the Resend HTTPS adapter with a provider idempotency key and an explicit
recipient allowlist (policy stores only address fingerprints). Notification
failure is retryable delivery state and never changes a run outcome.

## Retired cloud rollback path

An earlier design ran the deterministic monitor on a paused Cloud Scheduler
job (`openpapers-monitor-daily`) triggering a Cloud Run job
(`openpapers-monitor`) in the `llmcon` GCP project, kept solely as a
rollback path while the local LaunchDaemon was unproven. Once the local
service was the proven, sole production writer this path had no remaining
purpose, so on 2026-07-18 both cloud resources were deleted and the
implementing code (`automation/prefect_flows.py`, `run_monitor_flow.py`,
`automation/deployment/`, `automation/mac_worker/`) was removed from the
repository. The code is still recoverable from git history if a cloud path
is ever needed again; see `docs/automation-system/local-first-decision.md`
for why local-first was chosen in the first place.

## Security posture

This system runs on one Mac that only its maintainer has account or physical
access to, with no other writer and no adversarial network — the dev
machine is the production machine. A recurring theme in the codebase's early
history was security/integrity machinery sized for a multi-tenant or
adversarial-network deployment, carried forward past the point it matched
that reality. On 2026-07-19 a deliberate pass reassessed and cut three
mechanisms whose only job was catching *tampering*, a threat model with no
realistic actor here:

- **`automation/upgrade_safety.py`** (deleted): byte-exact whole-runtime-tree
  SHA256 verification against a manifest, plus a 7-stage `UpgradeStage`
  state machine with enforced single-step transitions and a `rollback-plan`
  generator. `upgrade-enabled.zsh` (untracked,
  `data/automation/agent-upgrade/`) now does backup → swap → restart → a
  real post-restart health check (a fresh bounded wake actually completing
  healthily, inlined directly rather than going through a separate module)
  instead. The git-commit/clean-worktree check on the agent-source
  candidate stayed — it answers "is this actually the commit I think it
  is," a different and still-useful question from "did someone tamper with
  it."
- **The `.production-control.v1.json` / `.agent-production-control.v2.json`
  integrity-marker chain** (removed from `local_service/production.py` and
  `local_service/agent_control.py`): each marker recorded a SHA-256 hash of
  the config/secrets bytes it was written together with, and every read
  path recomputed and compared. Editing any file in the chain by hand
  invalidated everything above it — this was the direct cause of the
  2026-07-17 production incident that `agent_operations.py`'s now-deleted
  `repair-markers` command existed to recover from. Config and secrets
  files are now validated independently against their own schema (type,
  shape, range) with no cross-file or baseline check. Concrete, accepted
  trade-off: hand-editing `external_effects_enabled` directly in a
  well-formed config file now takes effect on the next read, the same as
  going through the dedicated activation function — see
  `test_hand_editing_the_effects_bit_now_takes_effect_directly` in
  `automation/tests/test_agent_control.py`. An interruption between the
  (now two, not three) file replaces during a write can also leave config
  and secrets from different "generations" without failing validation
  closed; in practice this is low-risk because secrets content rarely
  changes across a normal replacement.
- **The `agent_status.py` two-worktree canary drift proof**
  (`create_canary_proof`/`read_canary_proof`, a `--canary-proof` flag on
  `report`, and the `canary-proof` subcommand): compared the
  `codex_installed`/`icml_2026` worktrees' HEAD/branch/status/remote-count
  against a private baseline to catch the installed runtime being modified
  outside the deploy pipeline.

Kept, deliberately, because they answer a different question than "was this
tampered with":

- **The coding agent's own sandbox boundaries** — isolated worktree, no
  push/commit/merge/deploy, `workspace-write` Codex sandbox, no sudo. This
  bounds what an LLM agent running with some autonomy can do to the host,
  independent of who else has access to the machine.
- **External-effect budgets, cooldowns, and the systemic-failure circuit**
  on Gemini/Codex/Resend calls — cost control and being a considerate web
  citizen, not security theater.
- **Backup-before-upgrade and `control_state_migration.py`'s
  rehearse-on-a-copy-first pattern** — cheap (a `cp` and a rerun of the
  migration against the copy) and guards against a real, still-plausible
  mistake (a schema-migration bug), not against an adversary.
- **Secret-scrubbing on read-only status output**
  (`domain.assert_secret_free`, no paths/addresses/credentials in
  `agent_status`/dashboard output) — this protects against exactly the kind
  of exposure that happens when status output gets pasted into a chat, a
  ticket, or an AI coding session, which is a real and current usage
  pattern, not a hypothetical one.

The staff-group read loosening on `/var/db/openpapers-production` (see
[`current-handoff.md`](./automation-system/current-handoff.md)) was the
first pass at this; the "zero group/other permission bits" pattern it left
behind in `agent_canary.py`, `agent_status.py`,
`control_state_migration.py`, `source_change_hints.py`,
`local_service/records.py`, `dashboard_deployment.py`,
`agent_success_rehearsal.py`, `local_service/agent_control.py::
validate_agent_source`, and two more spots inside `local_service/
production.py`/`local_service/service.py` found in this pass were all
loosened to the same group-read-allowed/group-write-and-other-forbidden
standard in the same change.

## Historical material

The abandoned deterministic-verification design (P0–P6) is archived under
[`automation-system/archive/`](./automation-system/archive/README.md).
Deployment history lives in git history and dated ExecPlans, not in this
page. Ignored `docs/local-p4*-operations.md` files on the production Mac are
private operational records, not public guidance.
