# Current automation handoff

This page is the concise, zero-conversation handoff for the OpenPapers
automation system. It records the last verified deployment boundary and the
next development gates; it is not a substitute for live health evidence.

Before changing automation code, read the required documents in this order:

1. [`README.md`](./README.md)
2. [`architecture.md`](./architecture.md)
3. [`roadmap.md`](./roadmap.md)
4. [`development.md`](./development.md)
5. this handoff

Then inspect `git status`, commits since the installed revision, and only the
ExecPlan relevant to the requested task. Most plans under `.agent/plans/` are
completed historical records, including plans for the retired verification,
case, job, Prefect-worker, and staging architectures. Do not infer that they
are active from their presence.

## Last verified boundary

As of 2026-07-17, production has these properties:

- The sole writer is the hourly system LaunchDaemon
  `org.openpapers.local-control`. Its external-effects gate is enabled and its
  private SQLite database is schema 10.
- The installed runtime and pinned no-remote agent source are commit
  `eb0e762`. Repository commits after that revision are documentation only at
  this handoff; always confirm with `git log eb0e762..HEAD`.
- The coding-agent cohort contains all 14 annual catalog venues. October adds
  the following year and January advances the active window. Each wake still
  initializes at most one date or runs at most one due agent.
- Continuous JMLR is visible in the catalog/dashboard but is deliberately not
  enrolled. Terminal annual `success` semantics would miss later JMLR papers
  in the same year.
- The independent deterministic source monitor still has only the three
  configured 2026 sources: AISTATS, ICML, and IJCAI. Agent enrollment does not
  invent a deterministic monitor source.
- Codex device authentication, impersonated Google ADC, the Resend sender, and
  a two-recipient allowlist are configured for the dedicated role. Do not copy
  their values into a prompt, log, document, fixture, or commit.
- The retained Cloud Scheduler/Cloud Run path was last verified paused with
  zero active executions. That is historical evidence, not a current health
  claim; generate a fresh private proof before any production mutation.
- The original ICML 2026 canary and installed Codex canary worktrees are
  retained outside managed cleanup. Their expected dirty/clean states require
  the private proof workflow rather than an assumption that both are clean.
- The read-only venue dashboard runs as two additional LaunchDaemons. Its
  application listener is loopback-only; authenticated HTTPS is available on
  the NIU network or VPN at `https://archer.cs.niu.edu:8443/` with username
  `openpapers`. The operator holds the password.
- The dashboard uses a manually installed NIU-issued DigiCert wildcard leaf.
  It expires at 2026-12-03 23:59:59 UTC and is not automatically renewed by
  Caddy. Begin renewal with NIU DoIT by early November 2026.
- Exact pre-upgrade and pre-certificate rollback artifacts are retained in
  private production storage. Do not delete or restore them without separate
  operational authority.

The concrete deployed topology and operating history are in
[`../automation.md`](../automation.md). Only executable code/tests and fresh
host evidence can establish current behavior and health.

## Current development position

The agent-driven path is implemented and enabled: approximate dates schedule
work, Codex decides readiness and scraper action, worktrees and execution
artifacts are bounded, and every terminal run has durable Resend report state.
Dates and deterministic source changes remain scheduling hints, never
readiness proof. The abandoned strict verification/job/case design remains
retired.

There is no unfinished repository implementation required merely to keep the
service running. The next feature should be selected from an explicit user
outcome. Known follow-up gates are:

1. **First genuine production `success`.** Preserve production state and
   review the large-volume scrape, validation evidence, artifact bounds,
   worktree, and report delivery. Only after that acceptance should legacy
   schema simplification be considered. The relevant living record is
   `.agent/plans/agent-production-learning-lifecycle.md`.
2. **Continuous JMLR enrollment.** Design recurring non-terminal success
   semantics before adding JMLR; do not force it through the annual cohort.
3. **Dashboard product changes.** Preserve the immutable read-only state
   boundary, loopback application listener, authenticated HTTPS proxy, and
   absence of control endpoints. The completed deployment record is
   `.agent/plans/enabled-upgrade-dashboard-deployment.md`.
4. **Certificate renewal.** This is an operator maintenance task, not a reason
   to change the agent scheduler. Generate a new private key/CSR without
   overwriting the live key, send only the CSR, validate the returned leaf and
   chain, and perform a proxy-only atomic replacement.

## Safe pickup procedure

Start every continuation with read-only repository inspection:

```bash
git status --short
git log --oneline --decorate -12
git log --oneline eb0e762..HEAD
python postprocessing/generate_statistics.py --check
```

For an automation code change, use the validation floor from
[`development.md`](./development.md). Create or resume an ExecPlan only when
the requested work meets `.agent/PLANS.md` criteria. Update this handoff in the
same change when the installed revision, active venue scope, topology,
credential/recipient shape, certificate boundary, next development gate, or
production safety policy changes.

Repository inspection does not authorize production actions. Installation,
enabled-runtime replacement, activation/rollback, live Gemini/Codex/Resend
calls, cloud resume, IAM changes, deployment, push, and deletion of retained
worktrees or backups each require appropriate explicit authority.

## Reusable continuation prompt

Replace the bracketed outcome rather than asking the next agent to infer work
from history:

> Continue OpenPapers agent-driven automation for: **[concrete outcome]**.
> Read `AGENTS.md`, then
> `docs/automation-system/README.md`, `architecture.md`, `roadmap.md`,
> `development.md`, and `current-handoff.md` in order. Inspect the working
> tree, commits since installed revision `eb0e762`, executable behavior, and
> only the relevant ExecPlan. Preserve the current agent-driven design: dates
> are scheduling hints, Codex decides readiness and scraper actions, and the
> retired strict verification/job/case architecture stays retired. Proceed in
> order and pause only when user authority or input is genuinely required.
> Do not mutate production, run a live canary, resume cloud, change IAM,
> deploy, push, or delete retained backups/worktrees unless separately
> authorized.
