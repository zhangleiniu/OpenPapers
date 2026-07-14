# Local control service package

This directory contains the completed P4.L3 packaging boundary for the
accepted local-first control plane. It renders a credential-free system
LaunchDaemon and provides bounded local health/run records, but nothing here
is installed, loaded, started, or connected to a live effect.

The existing Cloud Run monitor remains the sole production scheduler and
writer. P4.LS owns any later isolated host installation and drills; P4.LC owns
the no-overlap production cutover.

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
volume: the default probe requires the configured root to be an available
local mount point. Missing or unsafe storage fails before the injected wakeup
boundary or control SQLite is opened.

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

The rendered command currently has no concrete wakeup effect. Running
`python -m automation.local_service` without a test-injected effect records
and reports `effect_unconfigured`, returns nonzero, and does not open the
control database. P4.L3 deliberately adds no provider, notification, job,
command, scraper, cloud, or production adapter. Do not install this inert
definition as evidence of a working scheduler.

## Focused verification

From the repository root:

```bash
.venv/bin/python -m unittest automation.tests.test_local_service -v
```

The tests use temporary private directories, fake clocks, fake volume probes,
and fake effects. They do not inspect a real role account or volume and do not
copy a plist or invoke the service manager.

## Future installation and scoped rollback

P4.LS must separately authorize and record creation of the dedicated account
and internal paths, plist installation, service-manager operations, isolated
state, co-resident-service health gates, and reboot/SSH/missing-volume and
recovery drills. It must not give this service production authority.

`build_rollback_scope` fixes the only removable service artifact as:

```text
label: system/org.openpapers.local-control
plist: /Library/LaunchDaemons/org.openpapers.local-control.plist
```

A future authorized rollback may boot out only that exact label and remove
only that exact plist. It must preserve the internal root (including control
state and bounded records), repository, external execution data, and every
unrelated launchd label. Actual installation or rollback commands are P4.LS
operator actions; this package exposes no function that executes them or
deletes a path.
