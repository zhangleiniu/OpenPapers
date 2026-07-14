# Phase 4 local execution-plane and cutover review — 2026-07-14

This is the durable, sanitized review record for the Phase 4 local execution
plane, P4.LS host shadow, and P4.LC production-writer cutover. It contains no
credential, remote object generation, private state content, host-user path,
or notification address. Private backups, state, markers, scripts, and the
more detailed operations ledger remain outside Git.

## Boundary reviewed

P4.1 through P4.4 established typed job identity, fake-only submission and Mac
receiving boundaries, local locking/supervision, and immutable result
contracts. They did not install a worker or authorize a command. P4.O then
stopped at its feasibility gate before creating a Prefect pool, queue,
deployment, result bucket, or IAM grant because the acceptable hosted plan did
not support the required hybrid process pool.

The accepted local-first path preserved those contracts while replacing the
transport:

- P4.L1 created immutable local ownership and a bounded due-work scheduler;
- P4.L2 composed accepted domains only through injected fake effects;
- P4.L3 rendered a credential-free one-shot service boundary and exact
  rollback scope;
- P4.LS installed and drilled a separately marked scheduler-only shadow with
  no production authority; and
- P4.LC transferred the existing deterministic monitor and notification
  writer to the Mac without adding scraper, validator, typed-job, result,
  promotion, or arbitrary-command execution.

The production adapter retains the legacy monitor state separately from the
schema-version-6 local-control state. Its private configuration is allowlisted
and bound to the reviewed registry, backup fingerprint, and remote state
generation. Its credentials are supplied only through role-private files and
do not enter the plist, process arguments, health/run records, tests,
documentation, or Git.

## P4.LS host-shadow evidence

The authorized shadow installation used a root-owned read-only runtime, an
isolated Python environment, a dedicated non-login role, private internal
state, and private external execution storage. The exact service label ran
only the isolated bounded scheduler effect.

The following drills passed without production authority:

- exact duplicate wakeup suppression;
- missing-volume fail-closed behavior without mounting or unmounting shared
  storage;
- ambiguous-wakeup retention plus archive/new-root recovery;
- scoped rollback and reinstall;
- SSH disconnect survival and reboot resumption; and
- pre/post co-resident health with all five expected service labels.

The cloud monitor remained authoritative for the entire P4.LS review. No
notification, network monitor, job, command, result, or production-state
effect was reachable from the shadow marker.

## Backup and cutover evidence

Before production transfer, six remote monitor objects were downloaded to new
private staging. Pre/post manifests were identical, every generation and size
matched the local copy, SQLite quick-check and required-schema checks passed,
and the database contained six unique source rows. The retained fingerprints
were:

- initial manifest SHA-256:
  `8924b87931de266a3c75a73475b3dc35ea29e2bb12ab1e84e8112f7df4c04704`;
- initial archive SHA-256:
  `f3fbc6b4e34650c31756088178fccf3c25eb46b6db6bc5ed5f9e6b7c848cd726`;
- post-recovery manifest SHA-256:
  `3ebf69a4f8e877088351149bd1d9afec39af43108fd0ff68440b013e5a36f7db`;
  and
- post-recovery archive SHA-256:
  `ddfd18321f711874a18c08ef0afa7d806b9a1e41b5ead2a1bc339a3a0e5819dd`.

The single-writer order was fixed at every activation: prove the local writer
absent or stop the shadow, pause the one exact Cloud Scheduler job, prove the
schedule paused, prove zero active Cloud Run executions, activate the local
production state/runtime, start only the exact local label, and require local
plus co-resident health. No failed gate could compensate by starting both
writers.

Initial activation checked all six configured sources with zero source errors,
reported five ready local checks, preserved local immutable control ownership,
left zero active scheduler wakeups, and passed all five co-resident service
checks.

## Timed rollback and final activation

The measured rollback stopped and proved the local label absent before
resuming Cloud Scheduler. One manual Cloud Run recovery completed
successfully, advanced remote state, and left all five co-resident services
healthy. The full rollback took 96 seconds; local and cloud writers never
overlapped.

Final cutover repeated the pause-and-drain sequence while local was absent,
created and validated the second generation-stable backup, refreshed the local
monitor tree from the recovered cloud state, and then started only the local
writer. The final production run again reported six sources, zero errors, five
ready checks, local ownership, zero active control wakeups, and 5/5
co-resident health.

The final reviewed runtime archive SHA-256 is
`e1f1dba5cd7ddb7bffc2909fb8e1bbc5616bbb5aa3ef8c774dba272be9028423`.
After its atomic runtime-only synchronization, RunAtLoad produced one bounded
`no_due_work` record: the completed daily journal prevented a second monitor
or notification attempt. Independent inspection matched all 152 installed
runtime files to the reviewed working tree and confirmed the production plist
SHA-256
`2ee64ab7a7ed8d95e60ec2567b290fd484fc54cb5922827a3a69cded6f907bb0`.

## Validation

The final repository and installed-state checks were:

- `python -m unittest discover -s automation/tests -v`: 283 tests passed;
- `python -m unittest tests.test_pipeline -v`: 24 tests passed;
- focused local-service suite: 19 tests passed;
- compilation of the core, scraper, postprocessing, and automation modules:
  passed;
- generated statistics consistency: passed;
- tracked-candidate secret-value scan: passed;
- `git diff --check`: passed;
- installed runtime tree comparison: 152 files matched; and
- independent external gate: local production label present, Cloud Scheduler
  paused, zero active Cloud Run executions, and co-resident health 5/5.

## Retained rollback and residual risk

The Mac LaunchDaemon is the sole production writer. The exact cloud schedule
is paused, and the cloud job, remote state, credentials, both local backups,
the prior production runtime, and the P4.LS shadow runtime/environment remain
available for scoped rollback. Rollback must always stop and prove the local
label absent before resuming cloud scheduling.

The review does not eliminate these residual risks:

- the local writer is now an operational dependency, so host, storage, role,
  credential, and launchd health still require monitoring;
- the source bucket did not provide versioning as the durable rollback copy,
  so the separately retained immutable local archives remain important;
- the paused cloud path can drift and must repeat its health and zero-overlap
  gates before any future recovery; and
- P4.O remains paused, so the local-first topology does not claim hosted
  orchestration portability.

This evidence proves the reviewed 2026-07-14 installation and transition, not
all future host or cloud configurations. Material runtime, plist, ownership,
credential, schedule, or state-transfer changes require fresh bounded review.

## Non-effects and conclusion

Phase 4 did not execute a scraper or validator, select a repository command,
publish or consume a live immutable job result, promote canonical data, invoke
Codex, deploy MustCite, deliver Phase 3 cases, or grant arbitrary shell, path,
flag, or environment authority. Those boundaries remain closed.

Phase 4 passes its local execution-plane gate and is `Implemented`. This
review makes P4.LS/P4.LC operational evidence durable, but it does not itself
authorize Phase 5 execution. P5.1 remains the next package and must first
define an approved fixed command registry.
