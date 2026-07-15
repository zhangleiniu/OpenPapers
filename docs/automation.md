# Automation and monitoring

This page describes the current deployed automation boundary. The target
agent-driven design and its implementation status are documented separately in
[`automation-system/`](./automation-system/README.md).

## What runs in production

One marker-gated system LaunchDaemon on the maintainer's Mac is the sole
production writer. It wakes hourly and exits after a bounded invocation:

1. It validates private production configuration, restored monitor state, and
   the external-volume safety gate.
2. Once daily at or after 08:00 America/Chicago, it runs the deterministic
   source monitor. Exact daily replay does not repeat the monitor or its
   notification effect.
3. It sends one TLS SMTP email for every source change or source error.
4. On every wakeup it runs the local SQLite due selector. The selector records
   bounded due work but currently dispatches no discovery call, scraper, or
   coding agent.

The retained Cloud Scheduler trigger is paused. Its Cloud Run/Prefect monitor
remains available only for rollback and must never run concurrently with the
local production service.

Repository documentation is not proof of live external health. Inspect the
actual LaunchDaemon, bounded records, cloud schedule, and co-resident-service
health before an operational change.

## Deterministic source monitor

`automation/conferences.json` is the versioned source registry. Runtime hashes,
counts, snapshots, and status are stored separately under
`$SCRAPER_DATA_ROOT/monitor/` for ordinary manual use; the installed local
service uses its private restored monitor tree.

```bash
python automation/monitor.py
python automation/monitor.py --venue icml --year 2026
python automation/monitor.py --no-write
```

Each JSON-line event reports source status, item count, content hash, change
status, bounded diagnostic detail, and the most recent immutable snapshot.
Supported detectors are:

- `openreview_api`: hashes sorted accepted-note IDs;
- `official_html`: hashes normalized text for a configured repeated item;
- `pmlr_volume`: detects a matching proceedings listing.

The monitor is retained as cheap operational coverage. It is not the target
readiness authority and its registry does not need to recognize every source a
future coding agent might use.

## Local scheduling boundary

`automation/local_scheduler.py` obtains the single-writer lease and selects
conference records whose persisted `next_check_at` is due. A frequent local
wakeup therefore does not mean every conference is searched or checked.

The installed service now validates schema-10 state, private agent-control v2
configuration, the explicit 2026 cohort, a pinned no-remote agent source, the
Codex executable, durable execution/report state, and bounded retention policy.
Its installed `external_effects_enabled=false` gate prevents automatic Gemini,
Codex, Resend, scraper, or retention calls. The authorized installation wake
returned `no_due_work` with zero target rows and unchanged baseline monitor
state. Each live adapter and final activation still requires separate operator
authorization. See the [`roadmap`](./automation-system/roadmap.md).

Dedicated-role credential-path injection, three adapter-specific canary
commands, and disabled-only marker-last refresh are implemented in the
repository. They are not live deployment evidence: no credential has been
provisioned through that path, no new canary has run, and the installed global
gate remains false until separately authorized activation.

Schema version 10 adds event-date and agent schedule/attempt tables plus the
new execution-artifact and agent-run-report records.
`automation/control_state.py` still also contains tables and interfaces for the
abandoned verification, case/reminder, notification, and typed-job design.
They are vestigial compatibility surface and are not wired into production.

## Discovery adapter

The repository includes a budgeted and cached Gemini Search Grounding adapter:

```bash
python -m automation.run_discovery --venue icml
python -m automation.run_discovery --live --venue icml --year 2026
```

The ordinary command uses fixtures/development behavior. `--live` requires
explicit authorization, Application Default Credentials, and makes a real
provider call. The original adapter still produces strict citation-backed
evidence. The new `GeminiEventDateProvider` has a separate loose date-only
prompt plus fake and isolated-live coverage; its installed caller is disabled
and the dedicated role does not yet have ADC.

This automation discovery use is separate from Gemini track classification in
some core scrapers, documented in
[`GOOGLE_CLOUD_SETUP.md`](./GOOGLE_CLOUD_SETUP.md).

## Email boundaries

The installed monitor uses TLS SMTP and reports only deterministic source
changes/errors. `automation/resend_notifications.py` is an unconnected
low-level HTTPS adapter.

The future agent-run report will select one production transport and send one
replay-safe email per run. The old case, reminder, canary, and fatigue-digest
notification design has been retired.

## Cloud rollback path

`automation/prefect_flows.py` and `automation/run_monitor_flow.py` implement
the retained Cloud Run monitor. When deliberately resumed for rollback, Cloud
Scheduler starts a Cloud Run Job that restores monitor state from GCS and
records flow/task state in Prefect Cloud.

The cloud path is not the target scheduler and does not contain agent work,
typed job dispatch, or local execution. Deployment assets and the exact
single-writer warning are in
[`automation/deployment/README.md`](../automation/deployment/README.md).

Rollback order is strict:

1. stop and verify the local OpenPapers LaunchDaemon;
2. resume only the exact retained cloud schedule;
3. verify a successful cloud recovery and monitor-state persistence.

Local activation uses the inverse no-overlap order: pause and drain cloud
before opening local production state. Never use both as writers.

## Historical material

The former P0-P6 design—deterministic citation resolution, HTML/PDF
verification, cases/digests, typed jobs, Mac Prefect worker, staging pipeline,
and Codex as a last-resort repair step—was abandoned. Its documents and live
reviews are under
[`automation-system/archive/`](./automation-system/archive/README.md).

Ignored `docs/local-p4*-operations.md` files may be present on the maintainer's
Mac. They are private cutover/audit records, not public development guidance;
the current `local-p4lc` record remains useful for scoped rollback evidence.
