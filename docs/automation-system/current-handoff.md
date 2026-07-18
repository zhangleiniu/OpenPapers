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
  gate is enabled and its private SQLite database is schema 11 (no schema
  change in this upgrade; the pre-flight audit required the same version
  going in as coming out).
- The local-control runtime is the manifest-verified 153-file candidate
  (`candidate-20260718T215017Z`), installed via `upgrade-enabled.zsh` at
  2026-07-18T21:5x UTC. Code and config byte-match commit `95a6b7c` exactly —
  built from a clean committed snapshot (working tree was clean at build
  time) and bound with `upgrade_safety candidate --expected-commit`, so
  unlike the prior candidate there is no manifest/commit mismatch. The
  upgrade ran end to end with no rollback: services stopped, backup created
  and schema-11-rehearsed, runtime/source swapped, bindings replaced, one
  bounded first wake completed (`no_due_work`, as expected — nothing was due
  at that exact instant), canaries unchanged, both services restarted
  (`agent_enabled_upgrade=complete`). The pinned no-remote agent source is a
  clean, remote-stripped checkout of the same commit. The dashboard was
  restarted on the same runtime; it remains a separately managed persistent
  service. Confirm each installed component rather than treating one Git
  revision as proof for all services; repository commits are not deployed
  merely by existing on `main`.
- This upgrade also shipped the dashboard's last/next-edition redesign: a
  year with an open `agent_schedule` row (not yet `"Collected"`) no longer
  reads as a finished "last edition" just because its calendar date has
  passed, and pre-empts the cadence guess as "next edition" instead — a
  loopback probe immediately after install confirmed AISTATS/IJCAI 2026 show
  "next edition: 2026" (not a fabricated `~2027`) with a waiting-for-PDF
  badge (`--metadata-root` was already being passed by the installed plist
  for an earlier, removed feature; this reuses that same flag), while ICML
  2026 — fully downloaded per the metadata scan — gets no false badge.
- The deterministic monitor registry covers all 15 catalog venues (18
  sources); the private monitor configuration matches
  (`expected_source_count=18`).
- The agent cohort is the 13 formulaic venues (ICCV/ECCV on their two-year
  cadence) plus the manually confirmed `extra_targets` entry NAACL 2027.
  ICLR/AAAI/CVPR/COLT/ACL 2026 were operator-marked completed (canonical
  scrapes predate enrollment); ICML/AISTATS/IJCAI 2026 remain active with
  `not_ready` rechecks pending archival proceedings. JMLR is now enrolled
  under recurring non-terminal success semantics (`continuous_targets.v1.json`
  → `agent_production.py::_register_continuous_targets`): the installed
  LaunchDaemon's own post-restart wake (independent of the upgrade script's
  manual verification wake, which itself saw `no_due_work`) already claimed
  and completed one full JMLR cycle — the live dashboard shows
  `status=Scheduled, last try: success` with `next_check_at` ≈ 30 days out,
  confirming the recurring-success path (status never reaches `"completed"`)
  works in production, not just in tests.
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
- The Cloud Scheduler job (`openpapers-monitor-daily`) and Cloud Run job
  (`openpapers-monitor`, project `llmcon`) that made up the retained rollback
  path were deleted on 2026-07-18, and the implementing code
  (`automation/prefect_flows.py`, `run_monitor_flow.py`,
  `automation/deployment/`, `automation/mac_worker/`) was removed from the
  repository in the same change — see `docs/automation.md`'s "Retired cloud
  rollback path". `automation/agent_activation.py::read_cloud_drain_proof`
  and the matching `--cloud-proof` requirement in `agent_status.py`'s status
  report were removed with it (2026-07-18, same day) rather than left as a
  permanently-vacuous check — there is no cloud path left to verify paused or
  resume, and no contract left requiring a proof file. Both canary worktrees
  are retained outside managed cleanup, with expected states tracked by the
  private proof workflow (this refers to the unrelated Codex/agent-source
  canary proof, not the removed cloud proof).
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

1. **Certificate renewal** (operator maintenance; see `operations.md`).

Resolved this cycle: the upgrade-safety hardening (`upgrade_safety.py`, the
operator wrapper's fault-injected gates) and continuous JMLR enrollment
(`.agent/plans/perpetual-scheduling-and-jmlr.md`, commit `302df18`) are both
now installed and running in the runtime described above — the wrapper ran
them for real during this upgrade, and JMLR's first live cycle already
completed successfully.

## Safe pickup procedure

Start every continuation with read-only repository inspection:

```bash
git status --short
git log --oneline --decorate -12
git log --oneline 383bee3..HEAD  # inspect each installed service separately
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
