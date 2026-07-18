# Automation architecture

This page defines the stable component boundaries and safety invariants for
the agent-driven system described in [`README.md`](./README.md). The archived
deterministic-verification design is not a prerequisite for this architecture.

## Component boundaries

```text
hourly LaunchDaemon wakeup
          |
          v
local SQLite due selector ------------------------------------+
          | nothing due: exit                                 |
          |                                                   |
          +-- missing estimate --> cheap date discovery ------+
          |                         persist next_check_at; exit
          |
          +-- agent check due --> budget/concurrency gate
                                      |
                                      v
                         coding agent in isolated worktree
                         - receives venue/year + standing prompt
                         - investigates web and repository
                         - may edit/test/run scrapers
                         - never commits, pushes, merges, deploys
                                      |
                                      v
                           disposition + explanation + diff
                                      |
                         +------------+------------------+
                         |                               |
                  update next_check_at             one-shot email
```

The scheduler waking hourly does not mean conferences are checked hourly.
Before a persisted `next_check_at`, a venue/year causes no discovery request,
web fetch, or agent invocation.

The trusted baseline monitor may persist a changed-and-available venue/year as
a scheduling hint. The hint contains no source URL, content, snapshot path, or
readiness assertion. After all ordinary work in that wake, it may only move an
existing configured future `next_check_at` forward to the cooldown boundary;
therefore no agent is claimed from the hint until a later wake re-applies every
normal budget, concurrency, cooldown, and systemic-failure gate.

The Mac is the sole production writer. An earlier Cloud Run/Prefect monitor
existed only as a rollback path and was fully decommissioned on 2026-07-18
(cloud resources deleted, implementing code removed) once the local
LaunchDaemon was proven — see `docs/automation.md`'s "Retired cloud rollback
path".

## Where strict interfaces belong

The control plane is strict about authority and lifecycle:

- stable venue/year and run identity;
- one durable `next_check_at` and exactly recorded outcomes;
- worktree creation/removal and primary-checkout isolation;
- process timeout, cancellation, and bounded output capture;
- concurrency, cooldown, budget, and failure-circuit enforcement;
- replay-safe notification delivery;
- explicit prohibition on commit, push, merge, and deployment.

It is intentionally permissive about the agent's reasoning. The control plane
does not enumerate which source type, scraper class, website, or repair action
the agent may choose. The agent can use the repository's existing OpenReview,
official-site, PMLR, and other scraper patterns, browse for a new source, and
implement a venue-specific repair inside its worktree.

The Codex process keeps the `workspace-write` sandbox while enabling command
network only through a per-venue proxy allowlist derived from
`venue_catalog.v1.json` (`official_domains` plus `archival_domains`). It gets
no global wildcard, local/private-network access, MCP server, or write access
outside the managed worktree. Cached web search alone does not let scraper
subprocesses fetch proceedings or PDFs.

The result boundary should remain small:

| Disposition | Meaning | Scheduling consequence |
|---|---|---|
| `success` | The requested scrape and required validation completed | Clear `next_check_at`; notify for review |
| `not_ready` | The venue exists but usable papers are not published yet | Use suggested retry time or a bounded default delay |
| `needs_human` | Policy, access, ambiguity, or review blocks autonomous work | Clear automatic retry; notify immediately |
| `failed` | The run failed operationally or structurally | Bounded backoff; notify and pause after the configured limit |

The agent's natural-language explanation and worktree diff are primary review
artifacts. Do not recreate the abandoned case/action/job hierarchy merely to
encode its reasoning.

For `not_ready`, the standing prompt should expose the policy's accepted retry
window and ask the agent for a concrete UTC time when web/repository evidence
supports one. An active conference or partial/rapid official release warrants
a timely check; a stable prepublication state may use an announced revision or
proceedings date. Null remains valid when there is no defensible time, and the
controller uses its bounded fallback. The controller never parses prose to
invent a date.

## Scheduling state

The target durable state for each venue/year is deliberately small:

- approximate event date and how/when it was estimated;
- `next_check_at`;
- most recent agent disposition and run time;
- consecutive failure count;
- completion or human-intervention state;
- optional agent-suggested retry time.

An uninitialized venue/year gets one approximate-date lookup. A valid future
estimate sleeps without periodic refresh. When it becomes due, the coding
agent—not the discovery provider—checks actual publication readiness.

The tracked cohort policy preserves an explicit allowlist containing every
venue in the catalog with a reliable annual or fixed-period cadence. It does
not infer enrollment from deterministic monitor coverage and does not force
the continuous JMLR journal through a terminal conference-success lifecycle.
In the America/Chicago calendar, October adds the following year to
initialization; January advances the active window to that year. Expansion
registers all targets idempotently but still attempts at most one missing
date per wake. Durable rows for earlier years are neither deleted nor
reopened: their persisted terminal or retry state continues to govern them.
Calendar dates and the rollover remain scheduling hints, never readiness
evidence.

A venue whose catalog `lifecycle` carries an `interval_years`/
`cycle_anchor_year` pair only receives a target for a rollover year that
satisfies `(year - cycle_anchor_year) % interval_years == 0`; every other
allowlisted venue is treated as occurring every year, exactly as before. ICCV
(anchored on 2025, interval 2) and ECCV (anchored on 2024, interval 2) are
the catalog's only two periodic venues today, reflecting that ICCV is held
in odd years and ECCV in even years — the cohort no longer schedules a
Codex run against a year in which one of these conferences does not occur.
NAACL has no reliable calendar formula (see `docs/naacl.md`) and stays
outside the cohort allowlist entirely, the same treatment JMLR receives for
the same underlying reason. Unlike JMLR, NAACL is a discrete annual-lifecycle
venue that does eventually get a real, independently confirmable next
edition — `agent_targets.v1.json` may additionally carry a small
`extra_targets` list (schema version 3) of manually curated
`{venue_id, year}` entries for exactly this case: once a venue's next edition
is confirmed by some means external to the calendar formula (a web search, an
official announcement), it is added there and flows through the identical
downstream pipeline as any cohort target — Gemini still confirms the specific
date, Codex still independently decides readiness. `extra_targets` is not a
weaker or shortcut path; it only changes how the `(venue_id, year)` pair
enters the target list, never what happens to it afterward. A future
irregular edition still requires a new entry to be added deliberately; there
is no automatic "keep probing years until one confirms" mechanism.

A new discovery call is justified only when the estimate is absent or invalid,
the venue/year was explicitly rescheduled, or an agent result reports that the
date changed. Budget limits and provider failures must move `next_check_at`
forward or require intervention; they must not create a tight retry loop.

## Safety invariants

- **Automation remains optional.** Core scraper installation and execution do
  not depend on the control plane.
- **Discovery schedules; it does not authorize.** An approximate date may
  cause a future agent run only after the local due and budget gates. It is
  not treated as proof that papers or PDFs are available.
- **Monitor changes schedule; they do not authorize.** Only changed available
  events from the validated deterministic registry may advance an existing
  configured future check. They cannot create a cohort, reactivate terminal
  work, claim an agent in the same wake, or override a run completed after the
  observation.
- **The coding agent owns readiness and scraper decisions.** There is no
  deterministic HTML/PDF verification layer between discovery and the agent.
- **The agent runs in an isolated worktree/branch.** It has no write path to
  the primary checkout, `main`, or a remote and no code path commits, pushes,
  merges, or deploys. A maintainer reviews and commits manually.
- **Installed source and managed runs are siblings.** The validated read-only
  `agent-source` may share an external-volume parent with `agent-runs`, but the
  execution root may not equal or sit inside the source and the source may not
  equal or sit inside the managed runs root.
- **External content is untrusted.** The agent and monitor must respect
  authentication, robots/access controls, rate limits, and source terms.
  Permission to fetch a PDF does not grant redistribution rights.
- **Automatic external effects are bounded.** Date discovery and agent runs
  have separate budgets and cooldowns. Agent execution has a global
  concurrency limit and a systemic-failure circuit.
- **Local state has one writer.** `automation/control_state.py` remains under
  its expiring lease. No GCS job queue, Prefect work pool, or second scheduler
  may coordinate target-system work.
- **Persisted schemas are versioned.** Schema 11 deliberately drops the
  retired verification/case/job/scheduler tables while preserving all active
  rows. Any future replacement likewise requires a migration or compatible
  read plus replay, partial-failure, and recovery tests.
- **Email is a report, not an authority path.** A notification failure cannot
  change a run disposition or cause the agent's worktree to be promoted.
- **Cross-database report identity is explicitly scoped.** Production uses one
  state database, but isolated rehearsals add their authorization namespace to
  notification sources so identical venue/year/attempt identities cannot
  collide in Resend's global idempotency scope. Permanent failures remain
  closed by default; explicit recovery is limited to `protocol_error` with a
  fresh bounded namespace and cannot reopen delivered, in-flight,
  authentication, recipient, or payload outcomes.
- **Documents do not prove deployment.** Only executable wiring and an
  authorized live check establish that a component is active.
- **Status evidence is non-authoritative and secret-free.** Read-only status
  may summarize lifecycle, service, cloud, and canary evidence, but it cannot
  claim work or change a gate. It excludes credentials, recipient addresses,
  private paths, agent explanations, changed filenames, and provider receipts.
  A canary is compared with its private expected Git state rather than assumed
  clean.
- **The dashboard is a view, not a controller.** Its application HTTP listener
  is numeric loopback-only, rereads SQLite through the immutable safe-summary
  boundary, and offers no mutation method. All rendered content is escaped
  and the page loads no external resource of any kind. One deliberate
  exception to "no active content" exists (decided 2026-07-18 at the
  maintainer's direction): a single inline script implements the client-side
  timezone selector, with CSP still blocking every external script, style,
  image, and connection. The installed remote endpoint is a separate
  authenticated HTTPS proxy bound only to the host's fixed private address;
  it adds no control route.
- **Curated dashboard dates are auditable data.** Each tracked date records an
  official HTTPS source, the date it was verified, and whether it represents
  an event, main-program, or journal-volume start. The dashboard validates but
  never fetches or renders this provenance. Control-state estimates remain
  scheduling hints and cannot overwrite a curated date for the same year.
- **Canary authority is adapter-specific.** Gemini, Codex, and Resend live
  canaries are distinct commands and permissions. No canary permission enables
  the automatic production composition or another adapter.
- **Email recipients are an explicit allowlist.** Private schema-3 Resend
  configuration accepts 1-10 unique plain addresses, while tracked/private
  policy stores only their sorted SHA-256 fingerprints. Changing the allowlist
  requires a stopped-service marker-last replacement and never implies a send.
- **Activation is a separate authority.** Readiness, canary, refresh,
  rehearsal, and rollback permissions cannot open the production gate.
  Activation requires the fixed service stopped, exact current schema with no
  active/in-flight work, valid credentials and recipient binding, sufficient
  disk, a safe pinned source, and a fresh proof that cloud is paused and
  drained.
- **Activation replacement is marker-last and recoverable.** A fresh exact
  disabled backup is retained before changing only the effects bit. The marker
  binding config/secrets is installed last; interruption therefore fails
  validation closed. Rollback restores the exact disabled files marker-last
  while the service remains stopped and never resumes cloud.

## Current and retired components

The implemented component map is maintained in [`README.md`](./README.md);
current behavior is defined by the code and tests.

Retired from the target design (do not resurrect without a recorded
decision):

- deterministic citation-shape resolution and HTML/PDF verification;
- case/reminder/fatigue-digest workflows;
- typed scrape-action/job dispatch and staging/execution pipeline;
- Prefect Mac worker and GCS-compatible job-result transport;
- treating Codex as a last-resort repair step after deterministic execution.

Some contracts, tables, fixtures, and imports for retired components remain in
the repository until the state migration is designed. They are compatibility
debt, not supported target capabilities. Their design history lives under
[`archive/`](./archive/README.md).
