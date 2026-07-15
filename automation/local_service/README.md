# Local control service

This package renders and runs the marker-gated macOS LaunchDaemon used by the
current local-first automation deployment. It provides bounded health/run
records, an external-volume safety gate, a local SQLite due-selector effect,
and the production deterministic monitor effect.

An authorized no-overlap cutover completed on 2026-07-14. The Mac
LaunchDaemon became the sole production writer and the retained Cloud
Scheduler trigger was paused for rollback. Verify actual external state before
making a live-health claim.

## Process and storage boundary

`LocalServiceConfig` accepts explicit repository, Python, internal-root,
external-root, and dedicated-role values. It derives fixed state paths below
the internal root:

```text
control/state.sqlite3
service/health.v1.json
service/runs.v1.json
```

Internal and external roots must be disjoint private, non-symlinked paths.
OpenPapers does not create or mount the external volume. Missing or unsafe
storage fails before an injected effect or mutable control database opens.

Health is atomically replaced and run history is bounded. Records contain only
stable status/time/count information; they exclude configured paths, account
names, credentials, provider text, and raw exceptions. Corrupt or unsafe
record storage blocks work.

## LaunchDaemon

`render_launchdaemon` returns plist bytes and performs no installation. The
rendered service:

- uses the fixed label `org.openpapers.local-control`;
- runs as an explicit dedicated non-administrator role;
- wakes at load and at one hourly calendar minute, then exits;
- has no shell, environment dictionary, keepalive loop, socket, public
  listener, or launchd-managed log;
- uses restrictive file permissions and background/low-priority hints.

The ordinary unconfigured mode records `effect_unconfigured`, returns nonzero,
and does not open control state.

`--isolated-shadow` requires an exact private marker and invokes only
`run_scheduler_wakeup` against isolated local-owned SQLite. It has no network,
notification, scraper, agent, cloud, or production authority.

`--production-control` requires separately bound private marker,
configuration, and secret files. It validates the restored legacy monitor
state before mutable work. At or after 08:00 America/Chicago it durably claims
one daily monitor run, checks the registered sources, and sends change/error
notifications through TLS SMTP. Every hourly wake also runs the local due
selector against a separate control database. Exact replay does not repeat the
daily monitor or its emails; an interrupted active claim remains ambiguous and
blocks work.

The production command now validates the retained baseline v1 boundary plus an
agent-control v2 marker/config, schema-10 state, a pinned clean no-remote
`agent-source`, and the installed Codex executable. The installed v2 config has
`external_effects_enabled=false`, so date discovery, Codex agent execution,
retention, and Resend delivery are wired but inactive. Hourly replay therefore
preserves the baseline monitor and returns without a new external effect until
activation and each live canary are separately authorized.

Post-install operations use `automation.agent_credentials` for a fixed private
credential layout and `automation.agent_canary` for three independently gated
Gemini, Codex, and Resend checks. Disabled runtime/source updates must call the
marker-last `replace_disabled_agent_production_root` boundary while the service
is stopped. That boundary rejects enabled state on either side; it is not an
activation interface.

## Focused verification

```bash
.venv/bin/python -m unittest automation.tests.test_local_service -v
```

Tests use temporary private directories, fake clocks, fake volume probes, and
fake effects. They do not inspect a real role account, install a plist, or call
the service manager.

## Scoped rollback

`build_rollback_scope` fixes the only removable service artifact as:

```text
label: system/org.openpapers.local-control
plist: /Library/LaunchDaemons/org.openpapers.local-control.plist
```

Rollback may boot out only that label and remove only that plist. It preserves
control state, bounded records, repository/runtime files, external data, and
all unrelated labels. Never resume the retained cloud schedule until the local
label is confirmed stopped; never start local production until cloud is paused
and drained.

Host-specific installation, cutover, fingerprints, and rollback evidence are
kept in ignored local operations records rather than Git. The accepted
topology and public invariants are in
[`local-first-decision.md`](../../docs/automation-system/local-first-decision.md).
