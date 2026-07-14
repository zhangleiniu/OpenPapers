# Local control service package

This directory contains the completed P4.L3 packaging boundary and P4.LS
isolated host-shadow boundary for the accepted local-first control plane. It
renders a credential-free system LaunchDaemon, provides bounded local
health/run records, and exposes one marker-gated scheduler-only shadow mode.

The existing Cloud Run monitor remains the sole production scheduler and
writer. One authorized Mac installed and drilled the isolated shadow on
2026-07-14; repository files alone are not proof of its current external
health. P4.LC owns the no-overlap production cutover.

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
`python -m automation.local_service` without a test-injected effect or the
explicit shadow flag records and reports `effect_unconfigured`, returns
nonzero, and does not open the control database.

P4.LS adds `render_isolated_shadow_launchdaemon` and the fixed
`--isolated-shadow` flag. Before that mode can open state, an exact private
`.isolated-shadow.v1.json` marker must be initialized by the role account. Its
only effect invokes `run_scheduler_wakeup` against that isolated local-owned
SQLite database. It has no discovery, verification, notification, job,
command, scraper, result, cloud, Codex, or production adapter.

## Focused verification

From the repository root:

```bash
.venv/bin/python -m unittest automation.tests.test_local_service -v
```

The tests use temporary private directories, fake clocks, fake volume probes,
and fake effects. They do not inspect a real role account or volume and do not
copy a plist or invoke the service manager.

## Isolated installation evidence and scoped rollback

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

The shadow database must never be treated as migrated production state merely
because it is local-owned. P4.LC requires separate authorization, backup,
cloud-schedule disablement, no-overlap ownership activation, health checks, and
timed rollback.
