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

The Mac is the sole production writer. The paused Cloud Run/Prefect monitor is
only a rollback path and must never be resumed while the local production
LaunchDaemon is active.

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
- **The coding agent owns readiness and scraper decisions.** There is no
  deterministic HTML/PDF verification layer between discovery and the agent.
- **The agent runs in an isolated worktree/branch.** It has no write path to
  the primary checkout, `main`, or a remote and no code path commits, pushes,
  merges, or deploys. A maintainer reviews and commits manually.
- **External content is untrusted.** The agent and monitor must respect
  authentication, robots/access controls, rate limits, and source terms.
  Permission to fetch a PDF does not grant redistribution rights.
- **Automatic external effects are bounded.** Date discovery and agent runs
  have separate budgets and cooldowns. Agent execution has a global
  concurrency limit and a systemic-failure circuit.
- **Local state has one writer.** `automation/control_state.py` remains under
  its expiring lease. No GCS job queue, Prefect work pool, or second scheduler
  may coordinate target-system work.
- **Persisted schemas are versioned.** Replacing the old schema requires a
  migration or backwards-compatible read and tests for replay, partial
  failure, and recovery.
- **Email is a report, not an authority path.** A notification failure cannot
  change a run disposition or cause the agent's worktree to be promoted.
- **Documents do not prove deployment.** Only executable wiring and an
  authorized live check establish that a component is active.

## Current and retired components

Currently reusable:

- deterministic baseline monitor and immutable snapshots;
- venue catalog/configuration and discovery request plumbing;
- Gemini adapter, after simplifying its target use to approximate dates;
- additive schema-v8 approximate-date schedules and the uninstalled
  `initialize_event_dates()` composition boundary;
- lease-protected SQLite repository and local due selector;
- marker-gated LaunchDaemon service and bounded local records;
- paused Cloud Run monitor as a rollback mechanism;
- SMTP and Resend transport implementations, until one is selected for the
  run-report path.

Retired from the target design:

- deterministic citation-shape resolution and HTML/PDF verification;
- case/reminder/fatigue-digest workflows;
- typed scrape-action/job dispatch and staging/execution pipeline;
- Prefect Mac worker and GCS-compatible job-result transport;
- treating Codex as a last-resort repair step after deterministic execution.

Some contracts, tables, fixtures, and imports for retired components remain in
the repository until the state migration is designed. They are compatibility
debt, not supported target capabilities. Their design history lives under
[`archive/`](./archive/README.md).
