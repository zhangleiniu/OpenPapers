# Local control service package

This directory contains the completed P4.L3 package, P4.LS isolated shadow,
and P4.LC production boundary for the accepted local-first control plane. It
renders a credential-free system LaunchDaemon, provides bounded local
health/run records, and exposes mutually exclusive marker-gated shadow and
production modes.

On 2026-07-14 one authorized Mac completed the no-overlap P4.LC cutover. The
local LaunchDaemon is the production writer and the retained Cloud Scheduler
job is paused for rollback. Repository files alone are not proof of current
external health; verify launchd, bounded records, the cloud schedule, and
co-resident services before making an operational claim.

## Fixed storage and process boundary

`LocalServiceConfig` accepts explicit absolute repository, Python, internal,
and external-volume paths plus a dedicated role-user name. It derives these
paths rather than accepting arbitrary state or log files:

```text
<internal-root>/control/state.sqlite3
<internal-root>/service/health.v1.json
<internal-root>/service/runs.v1.json
```

The internal and external roots must be disjoint. The internal root and its
`control` child must already exist as private, non-symlinked directories owned
by the service process. OpenPapers does not create or mount the external
volume: the default probe requires the configured private execution directory
to be accessible and backed by a non-root mounted filesystem. Missing or
unsafe storage fails before the injected wakeup boundary or control SQLite is
opened.

The health snapshot is atomically replaced. Run history is an atomic JSON
document containing at most the configured limit (hard maximum 256) of stable
status/time/count records. Neither artifact retains configured paths, account
names, raw exceptions, credentials, or provider text. Corrupt or unsafe record
storage blocks work instead of being silently replaced.

## LaunchDaemon renderer

`render_launchdaemon` returns plist bytes and writes nothing. The rendered
system service has the fixed label `org.openpapers.local-control`, runs as the
explicit dedicated role user, wakes at load and at one hourly calendar
minute, and exits after one invocation. It has a restrictive umask,
low-priority/background hints, no shell, no environment dictionary, no
keepalive loop, no socket or public listener, and no launchd-managed log file.
Standard output and error go to `/dev/null`; bounded application records live
only under the internal root.

The ordinary rendered command has no concrete wakeup effect. Running
`python -m automation.local_service` without a test-injected effect or an
explicit concrete mode records and reports `effect_unconfigured`, returns
nonzero, and does not open the control database.

P4.LS adds `render_isolated_shadow_launchdaemon` and the fixed
`--isolated-shadow` flag. Before that mode can open state, an exact private
`.isolated-shadow.v1.json` marker must be initialized by the role account. Its
only effect invokes `run_scheduler_wakeup` against that isolated local-owned
SQLite database. It has no discovery, verification, notification, job,
command, scraper, result, cloud, Codex, or production adapter.

P4.LC adds `render_production_launchdaemon` and the fixed
`--production-control` flag. A distinct exact production marker binds a
private configuration file to the SHA-256 of an immutable monitor backup and
the restored GCS state generation. A separate private secret file contains
only the existing OpenReview and SMTP values; neither file appears in launchd
arguments, environment, bounded records, tests, documentation, or Git.

The production effect verifies the restored legacy monitor SQLite schema and
six registered source rows before opening mutable state. At or after 08:00
America/Chicago it durably claims one daily monitor run, checks the existing
three-venue/six-source registry, sends the existing change/error notification
through TLS SMTP, and then executes the schema-v6 local scheduler against a
separate control database. Hourly wakeups before the monitor slot still run
the local due scheduler. Exact daily replay does not repeat monitoring or
notification; an interrupted active claim remains ambiguous and blocks work.

## Focused verification

From the repository root:

```bash
.venv/bin/python -m unittest automation.tests.test_local_service -v
```

The tests use temporary private directories, fake clocks, fake volume probes,
and fake effects. They do not inspect a real role account or volume and do not
copy a plist or invoke the service manager.

## Installation, cutover evidence, and scoped rollback

The authorized P4.LS installation used a root-owned read-only runtime and
minimal Python environment, a dedicated non-login role, private isolated
state/records, and a private directory on the external filesystem. Duplicate
wakeup, missing-volume, ambiguous-state recovery, exact rollback/reinstall,
SSH disconnect, reboot, and co-resident-service gates passed. Host-specific
commands, paths beyond the fixed plist, and fingerprints remain in an ignored
local operations record rather than version control.

`build_rollback_scope` fixes the only removable service artifact as:

```text
label: system/org.openpapers.local-control
plist: /Library/LaunchDaemons/org.openpapers.local-control.plist
```

An authorized rollback may boot out only that exact label and remove only that
exact plist. It must preserve the internal root (including control state and
bounded records), repository/runtime, external execution data, and every
unrelated launchd label. P4.LS exercised that rollback and byte-identical
reinstall without touching a co-resident label. This package still exposes no
function that invokes the service manager or deletes a path.

P4.LC retained the shadow and production roots separately, restored and
integrity-checked the cloud monitor tree, copied the closed schema-v6 local
control database, and paused Cloud Scheduler only after a generation-bound
backup and zero-active-execution gate. The first and final local activations
each checked all six sources with zero errors and passed five local plus five
co-resident health checks.

The timed rollback stopped the local label before resuming the exact cloud
schedule, waited for a successful Cloud Run recovery, and completed in 96
seconds. Final cutover paused and drained cloud again before refreshing the
recovery generation and starting local. Rollback must preserve both local
state roots, backups, runtime/venv snapshots, cloud state, external data, and
unrelated labels. Never resume cloud until the local label is confirmed
stopped, and never start local until cloud is paused with zero active runs.
