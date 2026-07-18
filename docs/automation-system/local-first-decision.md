# Local-first automation decision

Status: accepted on 2026-07-14; still current

## Context

The earlier design expected Prefect Cloud to coordinate a process worker on
the maintainer's Mac. Its operator preflight found that the required hybrid
process work pool was unavailable on the acceptable plan. Paying for a higher
tier or self-hosting an orchestrator was disproportionate for a single-host
hobby workload.

The Mac is already an always-on SSH-managed host. It also runs unrelated
services and uses an external data volume whose availability may lag a reboot,
so OpenPapers must remain headless, low-impact, and fail closed without
managing another project's services or mounts.

## Decision

The production control plane is local-first and single-writer:

- a bounded plain-Python process wakes from a system LaunchDaemon;
- SQLite on internal storage owns scheduling state under an expiring lease;
- the scheduler reads `next_check_at`, processes only due records, and exits;
- external storage is scraper execution data, not scheduler coordination
  state, and its absence closes execution safely;
- no inbound service, Prefect work pool, GCS job queue, or multi-writer
  protocol is part of the target design;
- discovery, coding-agent invocation, and notification are bounded external
  effects called by the local control plane;
- the retained Cloud Run monitor is a paused rollback path, not a second
  scheduler.

An hourly LaunchDaemon wakeup is only a cheap local SQLite poll. It does not
authorize hourly web searches or conference checks. A venue/year with a
future `next_check_at` remains asleep until that time.

## Production state

An authorized no-overlap cutover completed on 2026-07-14:

- the local LaunchDaemon became the sole production writer;
- the existing deterministic monitor and TLS SMTP change/error notifications
  moved to that local effect;
- the Cloud Scheduler trigger was paused after local activation;
- a timed rollback proved local could be stopped before cloud was resumed;
- the cloud job, monitor state, and credentials were retained for rollback.

The original cutover enabled only the daily baseline monitor and local due
selection. Later separately authorized phases enabled date discovery, Codex,
and run-report delivery without changing this local-first topology. The dated
installed boundary now lives only in [`current-handoff.md`](./current-handoff.md);
actual external state must still be checked before an operational change.

Host-specific cutover and rollback evidence lives in the maintainer's ignored
`docs/local-p4lc-operations.md`. Earlier ignored `local-p4o` and `local-p4ls`
records are historical audits. None is a tracked development specification.

## Current migration and rollback invariants

1. The Mac and cloud monitor must never be active writers at the same time.
2. Rollback stops and verifies the local OpenPapers label before resuming the
   exact cloud schedule.
3. Activation pauses and drains cloud before opening local production state.
4. OpenPapers rollback may change only its own label, plist, runtime, and
   isolated files; it must not reload or modify co-resident services.
5. Internal SQLite state and external scraper data remain separate.
6. New date/agent functionality is fake-tested against isolated state before
   any separately authorized live invocation or service upgrade.

## Superseded parts of the earlier decision record

The local-first topology remains accepted, but the old plan to reuse typed
jobs, deterministic verification, a Prefect worker prototype, staged scraper
execution, and GCS-compatible results has been abandoned. Those details and
the original phased history are preserved under
[`archive/`](./archive/README.md); they are not constraints on the current
agent-driven roadmap.
