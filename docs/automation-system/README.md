# Automation system

This directory is the zero-context entry point for OpenPapers' optional
automation control plane. Read this page before changing `automation/`, then
[`architecture.md`](./architecture.md) for the safety boundaries and
[`roadmap.md`](./roadmap.md) for phase status. A returning agent reads
[`development.md`](./development.md) next and then
[`current-handoff.md`](./current-handoff.md) for the deployed snapshot and
next gates. Operational procedures live in
[`operations.md`](./operations.md).

The core scrapers remain independently installable and runnable. Prefect,
GCP, an LLM provider, email, and a coding-agent CLI are never core
dependencies.

## Product goal

The maintainer already handles a newly published conference by giving Codex
or Claude Code a venue and year. The agent inspects the repository and the
web, decides whether papers are available, reuses or repairs a scraper, runs
it, and explains the outcome. The automation system decides *when that
existing workflow is worth running* and invokes it in a contained
environment; it does not reproduce the agent's reasoning with venue-specific
verification code.

For each configured `venue/year`, the flow is:

1. Obtain one approximate event date from a cheap web-search/LLM discovery
   call ("ICML 2026 date"). This is a scheduling hint, not proof that
   papers are downloadable.
2. Persist the estimate as `next_check_at` and do no more network or model
   work for that venue/year before it is due.
3. When due, invoke a coding agent in an isolated git worktree with the
   repository, venue, year, and a standing prompt. The agent may
   investigate the web, edit scrapers, run tests, and attempt the scrape.
4. Capture a machine-readable disposition plus explanation and worktree
   changes: `success`, `not_ready`, `needs_human`, or `failed`.
5. On `not_ready`, use the agent's suggested retry time or a bounded
   default. On success or human intervention, stop automatic retries. Send
   one email per run outcome.

A wrong date estimate costs at most one contained agent run.

## Scheduling semantics

`next_check_at` is the source of truth for due work. There is no weekly
pre-date discovery loop and no daily check of every conference:

```text
date missing -> discover approximate event date once -> sleep until due
                                                      |
                                                      v
                                              run coding agent
                                 +--------------------+------------------+
                                 |                    |                  |
                              success             not_ready       needs_human/failed
                                 |                    |                  |
                         clear next_check_at   retry in a few days   email and stop or
                                                                    bounded backoff
```

A date may be rediscovered only when it is missing, invalid, explicitly
superseded, or a later agent result says the estimate changed. Adding a new
conference year does not reactivate older completed years.

## Component map

All of the following are implemented, tested, and — except where noted —
installed in enabled production. Current behavior is defined by the code and
tests, not this list.

- `automation/monitor.py` + `automation/conferences.json`: deterministic
  source monitor (OpenReview/official-HTML/PMLR detectors, immutable
  snapshots). Cheap coverage, never readiness authority.
- `automation/local_service/`: the installed Mac LaunchDaemon composition —
  bounded hourly wakes, daily monitor email, secret-free failure categories
  on failed wakes, and a consecutive-failure email alert. `production.py`
  guards the marker-bound private monitor configuration;
  `agent_control.py` guards the v2 agent configuration and the
  external-effects gate.
- `automation/local_scheduler.py` + `automation/control_state.py`:
  lease-protected due selection over versioned SQLite (schema 10). Old
  verification/case/job/notification tables remain as vestigial
  compatibility surface only.
- `automation/event_dates.py` + `providers/gemini.py
  ::GeminiEventDateProvider`: one-time approximate-date initialization with
  bounded retries and a monthly ceiling.
- `automation/due_policy.py`: effect-free due/retry/backoff/budget state
  machine — one global agent slot, venue cooldown, monthly ceiling,
  systemic-failure circuit.
- `automation/codex_agent.py`: the Codex-only isolated-worktree runner —
  sandboxed subprocess, structured four-field result, primary-checkout
  invariance, bounded artifacts.
- `automation/agent_worktree_retention.py`: bounded terminal-worktree
  cleanup; unregistered worktrees are never candidates.
- `automation/agent_run_notifications.py` +
  `automation/resend_notifications.py`: one replay-safe Resend report per
  terminal run with durable idempotent delivery state.
- `automation/agent_production.py` +
  `automation/config/agent_targets.v1.json`: the production composition and
  target policy — a 13-venue annual/periodic cohort (ICCV/ECCV carry
  `interval_years`/`cycle_anchor_year`) plus manually confirmed
  `extra_targets`; JMLR excluded pending recurring-success semantics.
- `automation/agent_credentials.py`, `automation/agent_canary.py`,
  `automation/agent_activation.py`: private credential layout, three
  separately authorized live canaries, and the read-only
  audit/rehearsal/activation/rollback boundary for the effects gate.
- `automation/agent_operations.py`: operator commands for deliberately
  fail-closed states — recover an interrupted date attempt, mark an
  already-scraped venue/year completed, update the monitor registry
  configuration with its full integrity-marker chain, repair a broken
  chain. See [`operations.md`](./operations.md).
- `automation/agent_status.py`: secret-free read-only production summary
  and the private two-canary proof format.
- `automation/agent_dashboard.py`: the loopback read-only venue dashboard —
  one perpetual-cycle row per venue (last/next edition from the curated
  `config/venue_editions.v1.json` merged with the control state's own date
  estimates, next attempt, color-coded countdown), timestamps in
  America/Chicago with a client-side timezone selector — served through an
  authenticated NIU-private HTTPS proxy.
- `automation/source_change_hints.py`: the scheduling-only bridge from
  monitor changes to an advanced future check.
- `automation/control_state_migration.py`: read-only audit, backup, and
  isolated-copy schema rehearsal.
- `automation/prefect_flows.py`, `automation/run_monitor_flow.py`,
  `automation/deployment/`: the paused Cloud Run monitor, retained solely
  as a rollback path.

Not yet built: migration of `control_state.py` away from its vestigial old
schema (planned after the target run state is proven by real successes),
and JMLR recurring-success enrollment.

## Documentation map

- [`architecture.md`](./architecture.md): target components and invariants.
- [`roadmap.md`](./roadmap.md): phase status and acceptance criteria.
- [`development.md`](./development.md): development and validation workflow.
- [`operations.md`](./operations.md): production runbook.
- [`current-handoff.md`](./current-handoff.md): dated deployment snapshot
  and next gates.
- [`installation-readiness.md`](./installation-readiness.md): audit and
  installation gates.
- [`local-first-decision.md`](./local-first-decision.md): why production is
  a single local Mac service.
- [`../automation.md`](../automation.md): deployed behavior summary.
- [`archive/README.md`](./archive/README.md): the abandoned
  deterministic-verification design.

## Sources of truth

- Current behavior: executable code and tests.
- Target design: this directory's non-archive documents.
- Deployed topology: [`../automation.md`](../automation.md), checked against
  actual Mac/GCP state.
- Last verified snapshot: [`current-handoff.md`](./current-handoff.md).
- Canonical dataset coverage: `statistics.md`.
- History: git log, dated ExecPlans under `.agent/plans/`, and `archive/`.
