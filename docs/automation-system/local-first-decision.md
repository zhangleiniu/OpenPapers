# Local-first automation decision

Status: accepted on 2026-07-14

## Context

P4.O attempted to preflight the Phase 4 Prefect topology on the maintainer's
current Prefect Cloud plan. Inspection showed that no planned Phase 4 pool,
queue, or deployment existed. The first create operation was rejected before
resource creation because the plan does not support the required hybrid
`process` work pool. Completing that design would require a recurring paid
orchestration tier whose cost is not justified for this hobby project.

The Mac mini is already an always-on host managed by system LaunchDaemons and
is reached through SSH. It also hosts unrelated production services and uses
an external data volume whose availability can lag a reboot. OpenPapers must
therefore remain headless, low-impact, fail closed when storage is unavailable,
and never manage or restart another project's services.

No Prefect Phase 4 resource, GCS result bucket, IAM grant, OpenPapers daemon,
fixture flow run, or live result object was created by the P4.O attempt. The
existing deterministic Cloud Run monitor remains the production baseline.

## Decision

The target control plane is local-first and single-host:

- one plain-Python scheduler reads durable local control state, derives due
  work from `next_check_at`, and exits after a bounded run;
- a system LaunchDaemon starts it at boot and on a coarse calendar interval;
- the daemon runs as a dedicated non-administrator role account and requires
  no GUI login, inbound port, Prefect profile, or orchestration subscription;
- SQLite on the Mac's internal storage is the target mutable control store;
- external storage is execution data, not scheduler state. OpenPapers observes
  its availability and fails closed; it does not mount the volume;
- existing typed jobs, stable identities, venue/year locks, disk gates,
  timeout/cancellation rules, replay suppression, and immutable result
  contracts remain reusable; and
- Vertex AI discovery, notification delivery, optional GCS backup/export, and
  later Codex calls remain bounded external effects, not scheduling or
  coordination dependencies.

The existing Cloud Run monitor stays active and remains the sole production
writer until an explicit later cutover package. Local development and shadow
runs must use isolated state. Cutover must first back up state, prove local
health and rollback, disable the cloud schedule, and then establish the Mac as
the only writer. Cloud and local schedulers must never concurrently mutate the
same control state.

## Why not the alternatives

- Paying for Prefect Cloud preserves the prototype transport but adds a fixed
  recurring cost disproportionate to the workload.
- Self-hosting Prefect replaces subscription cost with a database, server,
  upgrade, backup, and monitoring burden that this single-host workload does
  not need.
- A permanently running custom queue service adds failure modes without
  improving the durable `next_check_at` model. A bounded scheduled process is
  easier to inspect and recover.
- Moving everything immediately from Cloud Run to the Mac would create an
  unsafe, unmeasured writer cutover. Migration remains staged.

## Phase impact

- Phases 0-3 retain their current status and semantics.
- P4.1-P4.4 remain accepted prototypes. Their transport-specific Prefect
  topology and cloud-consumer ownership are historical; their identities,
  policy gates, local safety, and result validation are reusable.
- P4.O is paused, not complete. It records a failed feasibility gate rather
  than an operational worker.
- The P4.L package series replaces P4.O as the path to the Phase 4 gate.
- Phases 5-8 keep their user-visible goals but will execute through the local
  scheduler instead of a Prefect pull worker.
- Phase 9 may publish a derived public-safe snapshot to GCS, but GCS is not
  required for scheduler correctness.

## Migration and rollback invariants

1. Build and test local scheduling with fake clocks and temporary SQLite only.
2. Add a credential-free LaunchDaemon package without installing it.
3. Run an explicitly authorized, isolated shadow installation while the cloud
   baseline remains authoritative.
4. Compare outcomes and exercise reboot, SSH-disconnect, missing-volume,
   duplicate-wakeup, stale-claim, and recovery drills without touching
   canonical data.
5. Perform a separately authorized single-writer cutover with a state backup,
   cloud-schedule disablement, local activation, health checks, and a timed
   rollback procedure.

Before and after any host mutation or reboot, record bounded health for the
co-resident services. An OpenPapers rollback may stop or replace only the
OpenPapers daemon and its isolated files; it must not reload unrelated launchd
labels.

## Immediate implementation boundary

P4.L1 is the only next ready implementation package. It defines local
single-writer ownership and a clock-injected due-work planner/runner over
temporary state. It makes no network request, installs no daemon, changes no
production database, runs no scraper, and does not modify the current cloud
deployment. Later packages own composition, installation, drills, and cutover.
