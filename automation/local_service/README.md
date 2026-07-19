# Local control service

This package renders and runs the marker-gated macOS LaunchDaemon used by the
current local-first automation deployment. It provides bounded health/run
records, an external-volume safety gate, and the production deterministic
monitor plus agent-control effects.

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

`--production-control` requires separately bound private marker,
configuration, and secret files. It validates the restored legacy monitor
state before mutable work. At or after 08:00 America/Chicago it durably claims
one daily monitor run, checks the registered sources, and sends change/error
notifications through TLS SMTP. The agent composition separately applies its
event-date and run due policies against control state. Exact replay does not repeat the
daily monitor or its emails; an interrupted active claim remains ambiguous and
blocks work.

The production command now validates the retained baseline v1 boundary plus an
agent-control v2 marker/config, the exact current control schema, a pinned clean no-remote
`agent-source`, and the installed Codex executable. The installed v2 config has
`external_effects_enabled=true`; date discovery, Codex agent execution,
retention, and Resend delivery are active only behind their persisted due,
budget, cooldown, concurrency, and replay gates. The first enabled wake made
one event-date attempt and no Codex or Resend attempt.

The validated source may be `<external>/agent-source` while managed worktrees
use its sibling `<external>/agent-runs`. The composition rejects the production
execution root when it equals or sits inside the source, and rejects any source
that equals or sits inside the managed runs root.

Post-install operations use `automation.agent_credentials` for a fixed private
credential layout and `automation.agent_canary` for three independently gated
Gemini, Codex, and Resend checks. Disabled runtime/source updates must call the
`replace_disabled_agent_production_root` boundary while the service is
stopped. That boundary rejects enabled state on either side; it is not an
activation interface.

`automation.agent_activation` is the separate activation boundary. Its
read-only audit combines the exact v1/v2 files with exact-schema idle state,
credential/recipient/source/disk checks, and the fixed LaunchDaemon probe.
`rehearse-disabled` backs up, replays, and restores the disabled binding.
`activate` changes only the gate bit after its own exact authorization;
`rollback` restores the retained disabled files. Repository implementation
and disabled rehearsal do not authorize activation; the current enabled
state came from a separate explicit production activation and retains its
exact disabled rollback backup.

**Removed 2026-07-19**: the integrity-marker chain that used to bind
config+secrets+baseline together (writes were "marker-last": interruption
between file replaces used to fail validation closed) and the
`automation.agent_status` two-worktree canary drift proof (Codex/ICML HEAD/
branch/status/remote-count comparison) — both defended against tampering,
which has no realistic actor on this single-maintainer, physically-
controlled host. Config/secrets files are now validated independently
against their own schema only; `agent_status` no longer requires or reads
a canary proof. See `docs/automation.md`'s security-posture note.

`automation.agent_dashboard` is a narrower loopback-only scheduling view. It
uses the immutable safe target summary, lists every catalog venue, and exposes
no service, cloud, credential, or mutation interface. Its persistent backend
has a separate restart/deployment lifecycle from the bounded local-control
service; use `docs/automation-system/current-handoff.md` plus a live read-only
probe rather than inferring its installed revision from repository code.

`automation.source_change_hints` is the scheduling-only bridge from a validated
baseline monitor change to an existing configured agent schedule. The baseline
stores a de-identified pending hint in its existing wakeup journal; enabled
composition applies it only after this wake's ordinary agent work, so a later
wake must still pass all due gates. It never calls an adapter or creates a
target. The repository implementation is not evidence that the installed
runtime contains the bridge.

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
