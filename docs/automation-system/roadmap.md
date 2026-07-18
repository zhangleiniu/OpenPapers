# Automation roadmap

Phase status for the agent-driven control plane. The abandoned P0-P6
roadmap and its work packages are under [`archive/`](./archive/README.md);
do not resume their numbering. Detailed delivery history lives in git log
and the dated ExecPlans under `.agent/plans/`, not here.

Valid phase statuses are `Planned`, `In progress`, `Implemented`, and
`Paused` (with a dated reason when paused).

## Status

| Phase | Scope | Status |
|---|---|---|
| Baseline monitor | Deterministic source monitor, local LaunchDaemon, SQLite, change/error email | Implemented |
| Local due selection | Lease-protected selection from persisted `next_check_at`; no external effect | Implemented |
| Date initialization | One approximate event-date lookup per registered venue/year | Implemented |
| Due-state policy | Sleep before the estimate; retry/backoff/stop transitions afterward | Implemented |
| Agent execution | Codex in an isolated worktree, broad scraper judgment, narrow authority | Implemented |
| Run notification | One replay-safe email per agent run | Implemented |
| Production composition | Explicit cohort/config plus one bounded agent wakeup effect | Implemented |
| Installation, activation, rollback | Private config, marker-last transitions, exact recovery, enabled gate | Implemented |
| Post-install operations | Credentials, canaries, read-only status, dashboard, operator commands, failure alerting | Implemented |
| State simplification | Migrate away from vestigial verification/case/job/notification schema | Deployed and verified in production (2026-07-18) |
| JMLR enrollment | Recurring, non-terminal success semantics for a continuous journal | Planned |

## Phase notes

Each phase's acceptance was met with fixture/fake tests (no live provider
call in ordinary tests) plus, where external effects are involved, one or
more separately authorized live canaries. Normative behavior and safety
invariants live in [`architecture.md`](./architecture.md); operational
procedures in [`operations.md`](./operations.md). Points that remain
decision-relevant:

- **Baseline monitor.** Registered sources cover all 15 catalog venues. A
  monitor change or a date estimate never authorizes an agent run; the
  scheduling-only hint bridge can advance an existing future check to its
  cooldown boundary, nothing more.
- **Date initialization.** A new target is due once; success stores an
  08:00 America/Chicago check time; expected no-date/provider failures
  retry after 30 days; an unexpected interruption leaves a durable active
  ambiguity that blocks automatic replay until
  `agent_operations recover-event-date` closes it.
- **Due-state policy.** `next_check_at` is the only due-work clock. One
  global agent slot, venue cooldown, a monthly ceiling, and a 24-hour
  distinct-venue failure circuit bound execution. Three consecutive
  failures pause a schedule until explicit recovery.
- **Agent execution.** The runner is Codex-only (`workspace-write` sandbox,
  ephemeral state, ignored user config, no MCP). Command network is enabled
  through Codex's proxy only for the claimed venue's cataloged official and
  archival domains; no wildcard or local/private destination is allowed. It
  uses a portable
  four-field result schema. The standing prompt supplies the accepted
  retry window and asks `not_ready` runs for a concrete evidence-based UTC
  retry; null remains valid.
- **Production composition.** The tracked target policy (schema 3) is an
  explicit 13-venue allowlist with annual October/January rollover;
  ICCV/ECCV are scheduled only for years satisfying their
  `interval_years`/`cycle_anchor_year` cadence, and venues with no
  reliable formula enter only through manually confirmed `extra_targets`
  entries (currently NAACL 2027). Each wake performs at most one missing-
  date lookup or one due agent run. Durable rows for earlier years are
  never deleted or reactivated by the calendar; an already-scraped
  venue/year is closed with `agent_operations mark-completed`.
- **Operations hardening.** Failed wakes carry a bounded secret-free
  failure category; three consecutive failures alert by email. The private
  monitor configuration and its two chained integrity markers are updated
  only through `agent_operations update-monitor-config` /
  `repair-markers`.

## State simplification (Deployed)

Schema 11 migrates `automation/control_state.py` away from the inherited
verification, case, reminder, typed-job, generic-notification, and old
scheduler tables. Regression and isolated-copy tests open populated schema-10
state, preserve every active ownership/date/agent/artifact/report row, remove
all 15 retired tables, and reject corrupt or ambiguous state. The legacy
modules, schemas, fixtures, configs, and scheduler service mode were removed.
The authorized stopped-service deployment on 2026-07-18 retained exact
rollback copies, proved the schema-10 source unchanged in an isolated schema-11
rehearsal, migrated production, and completed a healthy bounded `no_due_work`
wake. All active row counts were preserved; local-control and the dashboard
were restarted on the manifest-verified runtime while cloud remained paused.

The first authorized rehearsal on 2026-07-18 did not satisfy this gate: it
proved terminal-artifact and delivered-report behavior but ended `failed`
because the then-current workspace sandbox left scraper network disabled.
The corrected second rehearsal produced and independently validated all 42
COLT 2011 papers/PDFs with a terminal `success`. Its report hit a
cross-database Resend idempotency collision with the first rehearsal and is
retained as `permanent_failure/protocol_error`. The explicit recovery became
report attempt 2, revalidated all 42 papers/PDFs, delivered successfully, and
returned `proved_success=true`; the evidence gate is now satisfied.
