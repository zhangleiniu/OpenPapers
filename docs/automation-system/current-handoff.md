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

For operational work use [`operations.md`](./operations.md). Then inspect
`git status`, commits since the installed revision, and only the ExecPlan
relevant to the requested task. Most plans under `.agent/plans/` are
completed historical records; do not infer activity from their presence.

## Last verified boundary

As of 2026-07-18, production has these properties:

- The sole writer is the hourly system LaunchDaemon
  `org.openpapers.local-control` (role `_openpapers`). Its external-effects
  gate is enabled and its private SQLite database is schema 11. The migration
  preserved 15 event-date schedules, 16 date attempts, 13 agent schedules,
  and five each of run, artifact, report, and report-attempt records.
- The local-control runtime is the manifest-verified 167-file candidate built
  on source commit `0389f5e89db8`; the pinned no-remote agent source remains at
  that commit. The dashboard was restarted on the same runtime; it remains a
  separately managed persistent service and may be upgraded independently.
  Confirm each installed component rather than treating one Git revision as
  proof for all services; repository commits are not deployed merely by
  existing on `main`.
- The deterministic monitor registry covers all 15 catalog venues (18
  sources); the private monitor configuration matches
  (`expected_source_count=18`).
- The agent cohort is the 13 formulaic venues (ICCV/ECCV on their two-year
  cadence) plus the manually confirmed `extra_targets` entry NAACL 2027.
  ICLR/AAAI/CVPR/COLT/ACL 2026 were operator-marked completed (canonical
  scrapes predate enrollment); ICML/AISTATS/IJCAI 2026 remain active with
  `not_ready` rechecks pending archival proceedings. JMLR is visible but
  unenrolled.
- OpenReview credentials for the role live in the local-control plist's
  `EnvironmentVariables` (plist is 0600 for that reason). Codex/ADC/Resend
  credentials live in the dedicated role's private credential root. Do not
  copy any of their values anywhere.
- The read-only dashboard (loopback backend + authenticated NIU-private
  HTTPS proxy at `https://archer.cs.niu.edu:8443/`, username `openpapers`)
  shows per-venue edition/check countdowns, dispositions, and report state.
  It is not a canonical-data coverage surface; use `statistics.md` for that.
  A post-migration 2026-07-18 loopback probe confirmed the schema-2
  provenance, target-selection fixes, and America/Chicago edition page.
  Its DigiCert leaf expires 2026-12-03 and is renewed manually — start with
  NIU DoIT by early November 2026.
- The retained Cloud Scheduler/Cloud Run path was last verified paused with
  zero active executions; generate a fresh private proof before any
  production mutation. Both canary worktrees are retained outside managed
  cleanup, with expected states tracked by the private proof workflow.
- Exact pre-upgrade rollback packages are retained in private production
  storage. Do not delete or restore them without separate operational
  authority.

## Current development position

The agent-driven path is implemented and enabled end to end. Failed wakes
record a secret-free failure category and alert by email after three
consecutive failures; deliberately fail-closed states have tracked operator
exits (`automation.agent_operations`). Dates and monitor changes remain
scheduling hints, never readiness proof; the retired strict
verification/job/case design stays retired.

Known follow-up gates:

1. **Rotate the OpenReview credential.** A broad diagnostic earlier in the
   2026-07-18 maintenance session rendered the LaunchDaemon environment in
   session output. No value was copied into this repository, but rotation is
   still required; use a separately authorized credential-maintenance action
   and never print the replacement.
2. **Review the upgrade hardening.** Schema 11 state simplification is now
   installed and verified. The working tree adds tracked, fault-injected
   upgrade safety checks and the operator-owned wrapper delegates to them, but
   this hardening change has not itself been installed as a new runtime and
   the wrapper has not been rerun. Review it with the rest of the current
   working tree before the next upgrade.
3. **Continuous JMLR enrollment.** Design recurring non-terminal success
   semantics; do not force JMLR through the annual cohort.
4. **Certificate renewal** (operator maintenance; see `operations.md`).

## Safe pickup procedure

Start every continuation with read-only repository inspection:

```bash
git status --short
git log --oneline --decorate -12
git log --oneline 0389f5e89db8..HEAD  # inspect each installed service separately
python postprocessing/generate_statistics.py --check
```

For an automation code change, use the validation floor from
[`development.md`](./development.md). Create or resume an ExecPlan only when
the requested work meets `.agent/PLANS.md` criteria. Update this handoff in
the same change when the installed revision, venue scope, topology,
credential shape, certificate boundary, next gate, or safety policy changes.

Repository inspection does not authorize production actions. Installation,
enabled-runtime replacement, activation/rollback, live Gemini/Codex/Resend
calls, cloud resume, IAM changes, deployment, push, and deletion of retained
worktrees or backups each require appropriate explicit authority.

## Reusable continuation prompt

Replace the bracketed outcome rather than asking the next agent to infer
work from history:

> Continue OpenPapers agent-driven automation for: **[concrete outcome]**.
> Read `AGENTS.md`, then
> `docs/automation-system/README.md`, `architecture.md`, `roadmap.md`,
> `development.md`, and `current-handoff.md` in order. Inspect the working
> tree, commits since the installed revision named in `current-handoff.md`,
> executable behavior, and only the relevant ExecPlan. Preserve the current
> agent-driven design: dates are scheduling hints, Codex decides readiness
> and scraper actions, and the retired strict verification/job/case
> architecture stays retired. Proceed in order and pause only when user
> authority or input is genuinely required. Do not mutate production, run a
> live canary, resume cloud, change IAM, deploy, push, or delete retained
> backups/worktrees unless separately authorized.
