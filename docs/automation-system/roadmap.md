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
| Agent execution | Coding agent in an isolated worktree, with broad scraper judgment and narrow authority | Implemented |
| Run notification | One replay-safe email per agent run | Implemented |
| Production composition | Explicit cohort/config plus one bounded agent wakeup effect | Implemented |
| Installation | Private config v2, production backup/migration, and LaunchDaemon switch | Implemented |
| Post-install operations | Private credentials, isolated live-canary commands, disabled refresh, read-only status, and local dashboard | Implemented |
| Activation and rollback | Read-only readiness, marker-last transition, exact recovery, disabled rehearsal, and enabled production gate | Implemented |
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
that selection on each wakeup. Due selections now feed the enabled bounded
date/agent/report composition.

This polling shape is retained: an hourly local SQLite query is cheap, while
network/model calls occur only for records whose `next_check_at` is due.

## Date initialization (Implemented, production enabled)

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
field optional. The explicit venue cohort, independent monthly lookup ceiling,
installed caller, and schema migration now exist. The repository target policy
also adds the following year each October and advances the active window each
January without expanding the venue allowlist or reopening durable old rows.
That annual policy is fake-clock tested but has not yet been refreshed into the
enabled installed runtime.

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

## Due-state policy (Implemented, production enabled)

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
state without losing date history. Its installed caller is now enabled but
cannot itself invoke an external adapter.

## Agent execution (Implemented, production enabled)

`automation/codex_agent.py` now creates a dedicated branch/worktree, invokes a
replaceable Codex boundary, validates the four-field result, inventories
worktree changes, verifies primary HEAD/status invariance, and closes timeout,
nonzero, oversized, or malformed output as `failed`. The real invoker fixes
Codex to `workspace-write`, approval `never`, ephemeral state, ignored user
config/rules, disabled MCP servers, cached web search, and the tracked output
schema. Temporary-repository tests use real Git plus a fake Codex invoker.

Schema version 10 registers an active execution artifact before Codex starts
and atomically finalizes its bounded changed-file/process inventory with the
due result and pending run report. `automation/agent_worktree_retention.py`
applies explicit age and count limits only to terminal registered worktrees
beneath the exact configured root. Removal failures are durable and retryable;
unregistered worktrees are never selected.

An authorized ICML 2026 canary on 2026-07-15 made four starts. It
found and fixed global CLI flag placement and unsupported conditional JSON
Schema keywords. The final Codex process exited successfully with no checkout
changes, but its `not_ready` result included a nonessential reason category
that the local parser rejected. The parser now safely ignores that optional
field for non-failure dispositions. The final call exited successfully and was
accepted as `not_ready`: Codex reported that OpenReview content was provisional
and the archival PMLR proceedings were not yet available. It made no edits;
both checkout HEADs and statuses remained unchanged.

Later production observation showed why structured retry guidance matters: an
ICML run explained the likely post-conference publication sequence but returned
no suggestion, so the controller correctly used its configured fallback. The
repository prompt now gives Codex the exact minimum/maximum accepted window,
asks it to investigate a concrete next publication signal, encourages timely
checks during active or partial release, and explicitly says not to suppress
normal use merely to save calls. The portable result schema documents that
contract, and parsing rejects timezone-free suggestions before durable
completion. Null and the existing fallback remain compatible when no credible
time exists. This refinement is fake-tested but is not in the currently
installed runtime until a separately authorized enabled-runtime upgrade.

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

Acceptance met: temporary-Git/fake-Codex tests cover isolation, terminal
artifact persistence, timeout and invalid-output closure, bounded inventory,
count retention, retryable removal failure, and preservation of unregistered
worktrees. The separately authorized ICML 2026 run supplied the required live
agent/sandbox evidence. No retention effect or schema migration ran against
that canary or production state.

## Run notification (Implemented, production enabled)

`automation/agent_run_notifications.py` composes one bounded report from the
terminal run, its schema-10 artifact, and a snapshot of retry/stop state.
Terminal runner completion creates the pending report in the same transaction
as the run outcome. Delivery claims use the existing single-writer lease and
the selected Resend HTTPS adapter receives a stable provider idempotency key.
Delivered, permanent-failure, and ambiguous in-flight records suppress blind
replay; typed transient failures create a numbered retryable attempt. The
installed baseline monitor continues to use SMTP for its separate source
change/error path; SMTP is not an agent-run transport.

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

Acceptance met: fake transports cover successful delivery and replay
suppression, transient failure followed by a second numbered attempt,
permanent failure suppression, stable idempotency identity, and bounded
secret-checked content. Live recipient provisioning and the installed caller
were added later under separate operational authorities.

## Production composition (Implemented, production enabled)

`automation/agent_production.py` composes the target path behind injected
provider, Codex, and notification boundaries. The tracked
`automation/config/agent_targets.v1.json` keeps an explicit allowlist of all 14
annual catalog venues and is deliberately independent of the deterministic
monitor registry. JMLR remains visible in the catalog but is excluded because
continuous publication needs recurring, non-terminal success semantics.
Starting from 2026, October includes the following year for one-time date
initialization and January moves the active window forward.
Existing persisted rows remain authoritative and are not deleted or
reactivated by the calendar. Private configuration pins the cohort fingerprint,
Gemini identity, absolute Codex binary, separate monthly date-lookup and
agent-run budgets, worktree age/count/per-wakeup removal bounds, and a bounded
approved-recipient fingerprint allowlist; credentials and email addresses
remain in a separate non-repr runtime object. Legacy single-recipient schema 2
remains readable, while interactive replacement upgrades both policy and
secrets to schema 3.

Each wake performs at most one missing-date lookup. A lookup attempt ends agent
processing for that wake even when it produces an immediately due date, so the
date remains a scheduling hint rather than readiness authority. Later wakes
may claim one due agent run, attempt one pending/retryable report, and remove at
most the configured number of registered terminal worktrees. Fake composition
tests cover initialization, next-wake execution, report retry with the same
idempotency key, idle replay, primary-checkout isolation, and the hard
retention bound. The separate monthly date ceiling defers a due lookup to the
next UTC month without calling the provider.

The repository composition now also contains a scheduling-only bridge for the
trusted baseline monitor. Changed available events are deduplicated to
venue/year/time in a bounded table inside the existing production wakeup
journal; URLs, details, hashes, counts, and snapshot paths are not copied. At
the end of an enabled wake, one configured existing future agent schedule may
move forward to the minimum cooldown. Missing schedules remain pending until
date initialization, unconfigured or terminal targets are ignored, and an
agent run at or after the observation supersedes the hint. Application occurs
after ordinary agent work, so only a later wake can claim; that claim still
passes monthly budget, global concurrency, venue cooldown, and systemic circuit
checks. Cross-database replay is idempotent and closed journal rows are bounded.
Fake monitor/composition/state tests cover these properties. The enabled
production runtime now contains this bridge.

Annual cohort expansion is installed in enabled production.
Fake-date tests cover exact equality with the catalog's 14 annual venues,
continuous-JMLR exclusion, the September/October/January boundary,
registration of the full expanded cohort, and the one-date-attempt-per-wake
bound.

The installed filesystem layout keeps the validated no-remote source at
`<external>/agent-source` and managed worktrees at the sibling
`<external>/agent-runs`. Isolation checks allow that safe shape while
rejecting an execution root inside the source and any source overlap with the
managed runs root.

`automation/control_state_migration.py` provides read-only safe-summary audit,
new-file SQLite backup, and isolated-copy schema rehearsal. Schema-9 fixtures
migrate to schema 10 without changing the source bytes or retained row counts.
The dedicated-role production database passed read-only audit and isolated
rehearsal, then migrated from schema 6 to schema 10 during the authorized
installation. The production external-effects gate is now enabled after the
repaired activation path passed its bounded first wake.

## Installation (Implemented, external effects enabled)

The dedicated role passed the schema-6 audit and isolated schema-10 rehearsal.
An authorized installation retained a fresh private backup and previous
runtime, migrated production state to schema 10, installed private v2
configuration, a pinned clean no-remote `agent-source`, and a role-executable
Codex binary, then reloaded the unchanged fixed LaunchDaemon. The accepted
wake returned `no_due_work`, created zero target rows, left the baseline monitor
bytes unchanged, and kept cloud scheduling paused. The v2 gate was
`external_effects_enabled=false` at installation. After credential canaries,
an initial failed activation/rollback, the `a17f9c5` source-layout repair, and
a disabled refresh, a newly authorized activation enabled the installed gate.
An authorized marker-last enabled upgrade later installed commit `eb0e762`,
registered all 14 annual venues in one transaction, and passed its bounded
one-selection first wake while retaining exact rollback packages.

## Post-install operations (Implemented; credentials provisioned)

`automation/agent_credentials.py` prepares and validates a fixed private
credential layout beneath the dedicated role's internal root. It can hand an
interactive terminal to Codex login or Google ADC login without placing a key
in arguments or output, and can marker-last install Resend values while the
service is stopped. The installed builder passes the private Codex home and
explicit ADC path to their adapters instead of relying on the role account's
`/var/empty` HOME or a maintainer login.

`automation/agent_canary.py` exposes three subcommands. Each requires a
different exact live flag and constructs only its selected adapter. Gemini
returns a bounded date summary, Codex retains a dedicated no-remote canary
checkout, and Resend returns only delivery status. None changes
`external_effects_enabled` or production scheduler state.

`automation/agent_status.py` adds two read-only commands. `canary-proof`
compares the original ICML and installed Codex canaries against a private
baseline containing their expected branch, HEAD, status digest, remote count,
and unprinted paths. `report` consumes that fresh address-free proof plus the
strict paused/drained cloud proof, then summarizes enabled configuration,
schema/idle state, the last three service wakes, current venue schedules,
latest attempts/reports/artifacts, and canary drift. SQLite opens with
`mode=ro&immutable=1`; output excludes explanations, changed filenames,
private paths, addresses, receipts, and credentials. Fake Git/SQLite tests
cover an intentionally dirty expected canary, later drift, stale/inconsistent
proof rejection, bounded output, and unchanged state bytes. The capability is
installed in enabled production.

`automation/agent_dashboard.py` adds a separate loopback-only view over the
safe SQLite target summary. It joins all 15 validated catalog venues to any
persisted venue/year state and displays monitor registration, lifecycle,
enrollment, last schedule update, next attempt, latest agent disposition, and
report state. It does not consume private cloud/canary proofs because it makes
no overall production-health claim. Every request reopens SQLite immutable and
read-only; fake state/HTTP tests prove all-catalog rendering, escaped content,
security headers, method rejection, non-loopback refusal, and unchanged state
bytes. The backend is installed as a loopback LaunchDaemon. A separate
unprivileged Caddy LaunchDaemon provides local-CA HTTPS and Basic Auth on the
host's fixed NIU private address; unauthenticated requests return 401.

`replace_disabled_agent_production_root` stages canonical private files,
fsyncs them, and replaces the marker last. It rejects enabled current or
candidate configurations, so refresh permission cannot become activation
permission; an interrupted set fails validation closed. Fake filesystem tests
cover successful replacement, activation rejection, Resend-only secret
provisioning, and partial replacement.

Acceptance met in repository tests. Dedicated-role Codex device authentication
and impersonated Google ADC have now passed the private-file status gate. A
rotated Resend key and schema-3 two-recipient allowlist were installed while
disabled; one separately authorized live canary used a single provider request,
and the operator confirmed delivery to both recipients. Activation remains a
separate operator action. The first
authorized installed Gemini canary attempt
failed at the SDK import before any provider request, which exposed the need
for an explicit installed-automation-dependency gate. After the fixed service
venv was repaired from tracked requirements, a newly authorized installed
Gemini canary completed and returned the ICML 2026 date hint `2026-07-07`.
A separately authorized installed Codex canary then completed with
`needs_human`; review accepted only its passing regression test for the
existing provisional OpenReview fallback, made no scraper-logic or readiness
claim, and retained the isolated canary worktree. None of these results is
permission for further actions.

## Activation and rollback (Implemented; external effects enabled)

`automation/agent_activation.py` adds four deliberately separate operator
commands. `audit` is read-only. `rehearse-disabled` exercises exact backup,
marker-last replay, and restore without setting the gate true. `activate`
requires its own exact authorization plus a stopped fixed LaunchDaemon and
changes only `external_effects_enabled`. `rollback` requires a stopped service
and restores the retained exact disabled binding; it does not require a model,
email provider, or resumed cloud path.

Readiness requires schema 10 with quick-check healthy, local ownership, no
active event-date or agent attempt and no in-flight report; private Codex and
Google credentials; configured Resend secrets whose 1-10 recipients match the
address-free policy fingerprints; the pinned clean no-remote source; configured
minimum execution-volume free space; the expected fixed-service state; and a
private cloud proof no more than 15 minutes old showing the exact external
schedule paused and zero active executions. The ignored host wrapper owns the
external GCP query and exact resource identifiers; tracked code validates only
the bounded proof.

`automation/local_service/agent_control.py` retains an exact private disabled
backup before activation, stages config/secrets/marker privately, replaces the
marker last, fsyncs the directory, and validates the result. In-process failure
restores the backup. Explicit rollback can restore that backup even when a
partial transition made the current marker invalid. Tests use temporary roots
and injected service/disk evidence; they never construct a live adapter.

Acceptance met in repository tests: safe audit output is address/path/secret
free and leaves state/config bytes unchanged; stale/non-drained cloud,
service-state mismatch, schema/active-state, credential, source, and disk
defects block; activation authorization is checked before audit/replacement;
the marker is last; interrupted replacement restores exact disabled bytes;
and disabled rehearsal ends byte-equivalent with effects false. Operational
acceptance also passed: the 203-file `a09aac9` runtime/source refresh completed,
stopped-service rehearsal ended disabled, the reloaded service returned
`no_due_work`, the loaded-service audit reported every readiness gate healthy,
cloud remained paused/drained, and both canary worktrees were preserved. This
is not activation. A later separately authorized activation passed readiness
and marker-last transition, but the first wake exposed an overly broad path
check before any date, agent, or report attempt. The host wrapper restored the
exact disabled backup and reloaded the service; cloud and both canaries
remained unchanged. Commit `a17f9c5` repaired the isolation predicate, and a
disabled refresh/rehearsal again passed every readiness gate. A new separately
authorized activation then completed: its first bounded wake recorded exactly
one event-date attempt, zero agent-run attempts, and zero report attempts.
The fixed service exited successfully and remains loaded, cloud remains paused
with zero active executions, both canaries are unchanged, and a fresh exact
disabled rollback backup is retained. The production gate is enabled.

## State simplification (Planned)

Once the run and notification records above are stable, migrate
`automation/control_state.py` away from the inherited verification, case,
reminder, typed-job, and old notification tables/imports. Do this once rather
than repeatedly reshaping persisted state during earlier phases.

Acceptance: a future cleanup migration opens representative schema-version-10
state, preserves approximate-date and still-needed scheduler ownership data,
creates the simplified records deterministically, and rejects corrupt or
ambiguous state.
