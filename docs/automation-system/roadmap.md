# Automation roadmap

This roadmap describes the short path from the current local monitor to the
agent-driven workflow. It is not a claim that planned components exist. The
abandoned P0-P6 roadmap and thread-sized work packages are under
[`archive/`](./archive/README.md); do not resume their numbering or dependency
graph.

## Status

| Phase | Scope | Status |
|---|---|---|
| Baseline monitor | Deterministic source monitor, local LaunchDaemon, SQLite, change/error email | Implemented |
| Local due selection | Lease-protected selection from persisted `next_check_at`; no external effect | Implemented |
| Date initialization | One approximate event-date lookup for an explicitly registered venue/year | Implemented |
| Due-state policy | Sleep before the estimate; agent retry/backoff/stop transitions afterward | Implemented |
| Agent execution | Coding agent in an isolated worktree, with broad scraper judgment and narrow authority | In progress |
| Run notification | One replay-safe email per agent run | Planned |
| State simplification | Migrate away from vestigial verification/case/job/notification schema | Planned after target run state is fixed |

Valid phase statuses are `Planned`, `In progress`, `Implemented`, and
`Paused`, with a dated reason when paused.

## Baseline monitor (Implemented)

`automation/monitor.py` checks registered OpenReview, official HTML, and PMLR
sources for content-hash/count changes and saves immutable snapshots. The Mac
LaunchDaemon wakes hourly; once daily at or after 08:00 America/Chicago its
production effect runs the monitor and sends TLS SMTP change/error events.

This baseline is useful operational coverage but is not the target readiness
decision. A monitor change does not dispatch an agent, and the target system
does not need to register a deterministic source before an agent may inspect a
venue.

## Local due selection (Implemented)

`automation/local_scheduler.py` takes the existing single-writer lease and
selects bounded due conference state from SQLite. The installed service invokes
that selection on each wakeup. Today it completes the selection without a
discovery or agent effect.

This polling shape is retained: an hourly local SQLite query is cheap, while
network/model calls occur only for records whose `next_check_at` is due.

## Date initialization (Implemented, uninstalled)

`automation/event_dates.py` now provides a provider-neutral
`initialize_event_dates()` boundary. Schema version 8 stores one current
schedule and immutable numbered attempts per venue/year. A new target is due
once, a successful date becomes its future `next_check_at`, and exact/pre-date
replay makes no second provider call. Expected provider/no-date outcomes retry
after 30 days; an unexpected exception leaves a durable active ambiguity that
blocks automatic replay.

`GeminiEventDateProvider` performs one loose Google Search call and
does not require a catalog-matched citation or claim paper/PDF readiness. Its
SDK behavior is fake-tested. An isolated live canary on 2026-07-15 estimated
ICML 2026's main-conference start as July 7, matching the official July 7–9
[schedule](https://icml.cc/Conferences/2026/Dates). Initial canary attempts
exposed and removed an unsupported Google
Search/response-schema combination and made the nonessential explanation
field optional. No target-cohort generator, budget-ledger connection,
installed caller, or production migration exists.

Deliverables:

- accept explicit configured venue/year records without a valid estimate;
- ask a cheap web-search/LLM provider for an approximate event date, equivalent
  to a maintainer searching "ICML 2026 date";
- persist the estimate, observation time, and `next_check_at`;
- do not require a catalog-matched citation or proof of paper/PDF readiness;
- do not query that venue/year again before the estimate merely to refresh it;
- handle missing/ambiguous dates with a long bounded retry and visible reason,
  never a tight loop.

`automation/discovery.py` and `automation/providers/gemini.py` already provide
budget, cache, provider, and manual-live plumbing. Their current evidence-
strict response is not yet the target approximate-date interface.

Acceptance met: fake-provider/fake-clock tests prove one lookup initializes one
venue/year, exact replay causes no second call, future estimates sleep, and
failure schedules a bounded later attempt. No live provider call is part of
ordinary tests.

## Due-state policy (Implemented, uninstalled)

`automation/due_policy.py` and schema version 9 implement this boundary
without invoking an agent. A successful date estimate creates an
`agent_schedule`; from that handoff onward its nullable `next_check_at` is the
executable clock, while the schema-8 value remains immutable date provenance.
Claims are protected by the existing local lease and one global active-run
index. Immutable run history supplies the monthly usage and systemic-failure
evidence without a second mutable ledger.

Deliverables:

- `next_check_at` is the only due-work clock;
- before the estimated date, no discovery, web, or agent call occurs;
- `success` clears due work and records completion;
- `not_ready` uses a valid agent-suggested retry time or a default delay of a
  few days;
- `needs_human` stops automatic attempts and requests review;
- `failed` uses bounded backoff and pauses after a configured consecutive-
  failure limit;
- one global agent slot, per-venue cooldown, a soft monthly budget ceiling,
  and a systemic-failure circuit prevent runaway execution.

The policy may be implemented as a small state reducer around
`next_check_at`; it must not revive the old action/job/case hierarchy.

Acceptance: fixture/fake-clock tests cover every disposition, suggested and
default retry dates, duplicate wakeups, concurrency exclusion, budget pause,
failure backoff, and recovery.

Acceptance met: fake-clock tests cover all four dispositions, pre-date sleep,
valid and rejected suggestions, default retry, three-step failure pause and
explicit recovery, active-run exclusion, UTC-month budget deferral, and a
24-hour distinct-venue failure circuit. Schema-8 migration seeds the new due
state without losing date history. No installed caller or external effect
exists.

## Agent execution (In progress, uninstalled)

`automation/codex_agent.py` now creates a dedicated branch/worktree, invokes a
replaceable Codex boundary, validates the four-field result, inventories
worktree changes, verifies primary HEAD/status invariance, and closes timeout,
nonzero, oversized, or malformed output as `failed`. The real invoker fixes
Codex to `workspace-write`, approval `never`, ephemeral state, ignored user
config/rules, disabled MCP servers, cached web search, and the tracked output
schema. Temporary-repository tests use real Git plus a fake Codex invoker.

The worktree inventory is currently returned to the caller and the worktree is
preserved; a dedicated durable execution-artifact row, bounded retention,
notification composition, and an authorized real Codex canary remain before
this phase can be marked Implemented.

Deliverables:

- create an isolated branch/worktree per run without modifying the primary
  checkout or any remote;
- give the agent a stable prompt containing the venue/year, repository
  completion contract, permission to investigate and edit within the
  worktree, and explicit prohibitions on commit/push/merge/deploy;
- allow the agent to choose sources, reuse existing scraper patterns, add or
  repair venue-specific code, run tests, scrape, and validate;
- supervise the CLI subprocess with timeout/cancellation and bounded,
  secret-safe capture;
- retain disposition, explanation, optional suggested retry time, changed-file
  inventory, and worktree path for review;
- preserve a failed or timed-out worktree when it is useful for diagnosis,
  under a bounded retention policy.

Acceptance: fake-subprocess tests prove worktree isolation, primary-checkout
invariance, timeout/cancellation, invalid-result closure, diff capture, and
the absence of commit/push/merge/deploy code paths. A separately authorized
manual live run against one real venue/year is required before the phase is
marked `Implemented`.

## Run notification (Planned)

Deliverables:

- one email for every terminal agent-run record, including disposition,
  explanation, venue/year, changed files, worktree location, and retry state;
- select either the existing SMTP approach or the Resend HTTPS adapter for
  this production path; do not maintain two active transports;
- durable idempotency so replay never sends the same report twice;
- notification failure remains retryable delivery state and never changes the
  underlying run outcome.

Acceptance: fake-transport tests prove exact-once logical delivery, replay
suppression, transient retry, permanent failure visibility, and secret-safe
content.

## State simplification (Planned)

Once the run and notification records above are stable, migrate
`automation/control_state.py` away from the inherited verification, case,
reminder, typed-job, and old notification tables/imports. Do this once rather
than repeatedly reshaping persisted state during earlier phases.

Acceptance: a future cleanup migration opens representative schema-version-8
state, preserves approximate-date and still-needed scheduler ownership data,
creates the simplified records deterministically, and rejects corrupt or
ambiguous state.
