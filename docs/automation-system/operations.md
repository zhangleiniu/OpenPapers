# Operations runbook

Task-oriented procedures for the installed production automation. Commands
run on the production Mac; `<python>`, `<internal-root>`, and
`<runtime>` come from the LaunchDaemon plist, which is root-only because it
carries credentials:

```bash
PLIST=/Library/LaunchDaemons/org.openpapers.local-control.plist
PYTHON=$(sudo /usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$PLIST")
RUNTIME=$(sudo /usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$PLIST")
INTERNAL=$(sudo /usr/libexec/PlistBuddy -c 'Print :ProgramArguments:8' "$PLIST")
```

Never copy credential values, recipient addresses, or private file contents
into logs, tickets, prompts, or commits.

## Services

| LaunchDaemon | Kind | Restart |
|---|---|---|
| `org.openpapers.local-control` | Bounded run-and-exit, hourly at :17, `RunAtLoad` | `sudo launchctl kickstart system/…` — **never `-k`**: the job may be mid-wake and `-k` kills it mid-transaction, leaving a durable ambiguity that fail-closes every later wake |
| `org.openpapers.agent-dashboard` | Persistent loopback web backend | `sudo launchctl kickstart -k system/…` (`-k` is fine here); required after every runtime upgrade — it does not reload swapped code by itself |
| `org.openpapers.agent-dashboard-proxy` | Persistent Caddy HTTPS proxy | same as dashboard |

A plist edit needs `bootout` + `bootstrap`, not kickstart. `bootstrap` on
`local-control` immediately triggers a wake (`RunAtLoad`) — do not follow it
with `kickstart -k`.

## Failure visibility

Each failed wake records a bounded, secret-free `failure_category`
(exception class, plus the static message for control-plane errors) in the
private `service/runs.v1.json`. After 3 consecutive failed wakes the service
emails the monitor recipients through the existing TLS SMTP path, then about
daily while still broken. If wakes fail but no email arrives, the SMTP
configuration itself may be what broke.

Inspect recent wakes (read-only):

```bash
sudo -u _openpapers "$PYTHON" - <<'PY'
import json, os
root = os.environ["INTERNAL"]
for r in json.load(open(f"{root}/service/runs.v1.json"))["records"][-5:]:
    print(json.dumps(r))
PY
```

(export `INTERNAL` first, or use `automation.agent_status report` for the
full bounded summary.)

## Operator commands

`automation/agent_operations.py` is the audited exit for states the control
plane deliberately fail-closes. Every subcommand is a dry run unless
`--apply` is passed; run as the service role from the installed runtime:

```bash
cd "$RUNTIME"
sudo -u _openpapers "$PYTHON" -m automation.agent_operations <subcommand> …
```

| Situation | Subcommand |
|---|---|
| A wake was killed mid-date-lookup; every wake now fails with "event-date attempt is active or ambiguously interrupted" | `recover-event-date --state $INTERNAL/control/state.sqlite3 [--retry-minutes N] --apply` |
| A venue/year's canonical scrape already exists on disk (manual scrape predating enrollment); stop the automation from re-scraping it | `mark-completed --state … --venue V --year Y [--event-date YYYY-MM-DD] --apply` (`--event-date` required when the target never got a successful date lookup) |
| `automation/conferences.json` changed in a deployed runtime; wakes fail with "registry fingerprint changed" or "source count changed" | `update-monitor-config --internal-root $INTERNAL --apply` (rewrites the private config and regenerates **both** integrity markers, in order) |
| The marker chain is inconsistent after an interrupted/partial config change | `repair-markers --internal-root $INTERNAL --apply` |

Background: the private monitor config is bound by
`.production-control.v1.json`, and `.agent-production-control.v2.json`
chains over both of those plus the agent config/secrets. Editing any file in
that chain by hand invalidates everything above it — always use the
commands, never hand-edit.

The monitor's persisted state must also hold exactly one row per registered
source before a wake will run it; after adding sources, prime once:

```bash
sudo -u _openpapers "$PYTHON" "$RUNTIME/automation/monitor.py" \
  --registry "$RUNTIME/automation/conferences.json" \
  --state "$INTERNAL/monitor/state.sqlite3"
```

## Diagnosing an orphaned event-date target

`event_date_schedule` has no deletion path by design — cohort/`extra_targets`
config changes (a biennial-cadence fix, a venue moving in or out of
`extra_targets`) never retroactively remove a row that no longer matches the
current config; they simply stop reprocessing it. Both
`initialize_event_dates` and `_chain_successor` only ever look at rows whose
`(venue_id, year)` is in the *current* `load_agent_targets()` output, so an
orphaned row sits `'pending'` forever, invisible to normal operation — except
on the dashboard, where its stale `next_check_at` can win
`_current_target_priority`'s tie-break over a venue's real target and hijack
its Status/Next-attempt columns. Sweep for these (read-only; the state file
is `staff`-group-readable, see the handoff's permission note) with:

```bash
"$PYTHON" - <<'PY'
from datetime import date
from automation.agent_production import load_agent_targets, load_continuous_venue_ids
import sqlite3
conn = sqlite3.connect("file:$STATE?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
valid = {(t.venue_id, t.year) for t in load_agent_targets(today=date.today())}
continuous = load_continuous_venue_ids()
for r in conn.execute("SELECT venue_id, year, status, next_check_at FROM event_date_schedule"):
    if (r["venue_id"], r["year"]) not in valid and r["venue_id"] not in continuous:
        print(dict(r))
PY
```

A row this flags is not automatically wrong — a freshly `_chain_successor`'d
or manually-backfilled future year is *expected* to be outside the current
cohort window until the calendar rollover (`rollover_month`) reaches it; only
a row for a year that will **never** become valid again (a cadence mismatch,
a venue no longer in `extra_targets`) is a true orphan. For a true orphan,
the only allowed edit is `next_check_at` (`status` stays `'pending'`; the
CHECK constraint forbids setting `estimated_event_date` without also
supplying real provider metadata, so never fabricate a confirmed date) —
align it with the venue's real next edition so it stops winning priority
ties, run as the service role in one transaction:

```bash
sudo -u _openpapers sqlite3 "$STATE" <<'SQL'
BEGIN IMMEDIATE;
UPDATE event_date_schedule
SET next_check_at = '<real-next-edition-iso-timestamp>',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE venue_id = '<venue>' AND year = <year> AND status = 'pending';
COMMIT;
SQL
```

This is the one legitimate hand-edit exception to "never hand-edit state
outside the commands" above — it is scoped narrowly to `next_check_at` on an
already-orphaned `'pending'` row, never to the marker/config chain.

To give an operator-completed venue with no successor a real 2027-style
target instead of a blank "Next attempt" forever, reuse the exact fallback
mechanism `event_dates.py` already applies when Gemini fails
(`_calendar_fallback_date` + `ensure_scheduled_agent_target`) rather than
hand-writing a row:

```bash
cd "$RUNTIME"
sudo -u _openpapers "$PYTHON" - "$STATE" <<'PY'
import sys
from datetime import datetime, timezone
from pathlib import Path
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.event_dates import _calendar_fallback_date, _check_time

state_path = Path(sys.argv[1])
now = datetime.now(timezone.utc)
venue_id, year, interval = "<venue>", <successor_year>, <interval_years>
with ControlStateRepository(state_path, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: now) as repository:
    lease = repository.acquire_lease("manual-successor-backfill")
    try:
        repository.register_event_date_target(venue_id, year, registered_at=now, lease=lease)
        fallback = _calendar_fallback_date(repository, venue_id, year, interval)
        assert fallback is not None, "no prior confirmed date in the database to shift forward"
        repository.ensure_scheduled_agent_target(
            venue_id, year, next_check_at=_check_time(fallback, now),
            registered_at=now, lease=lease,
        )
    finally:
        repository.release_lease(lease)
PY
```

If `_calendar_fallback_date` returns `None` (no database-recorded prior
estimate — e.g. the venue's most recent edition predates automation
entirely), source the "prior" date from the curated
`config/venue_editions.v2.json` file instead and compute the same
`prior_date.replace(year=prior_date.year + interval)` shift by hand before
calling `ensure_scheduled_agent_target`. Either way this is a temporary
bridge: the new row sits outside the current cohort window until the next
calendar rollover, at which point a normal wake attempts a real Gemini
lookup and overwrites the fallback — no code changes, only the same
repository methods production already calls.

## Updating the monitor registry (end to end)

1. Change `automation/conferences.json` in the repository, verify each new
   source with `python automation/monitor.py --venue V --year Y --no-write`,
   commit, and deploy the runtime (below).
2. Prime the monitor state (command above) so the row count matches.
3. `update-monitor-config --internal-root $INTERNAL --apply`.

A wake that fires between steps fails closed and recovers by itself once
all three are done; below the alert threshold it sends no email.

## Runtime upgrades

The host-local wrapper `data/automation/agent-upgrade/upgrade-enabled.zsh`
(untracked, operator-owned) performs the stopped-service, marker-last,
exact-rollback replacement. It delegates reusable fail-closed checks to
`automation.upgrade_safety`; do not copy those checks back into ad hoc shell
or inline Python.

Prepare a candidate from one clean, committed source snapshot. Run all tests
and compilation before generating `manifest.json`, keep
`PYTHONDONTWRITEBYTECODE=1` set during every later candidate probe, and never
modify the candidate after its manifest is written. The safety gate requires
an exact file count and digest, rejects symlinks and generated bytecode, and
binds the manifest to the expected full commit:

```bash
python -m automation.upgrade_safety candidate \
  --runtime <candidate-runtime> --manifest <manifest> \
  --expected-commit <40-character-commit>
```

The upgrade sequence is fixed:

1. Complete all read-only candidate, dependency, co-resident-service, cloud,
   credential-shape, source, and database audits before stopping a service.
2. Stop local-control and the dashboard; record the reached phase.
3. Stage the runtime as `root:wheel` with traversable directories and readable
   regular files, then run `upgrade_safety staged-runtime`. This catches a
   runtime that hashes correctly but `_openpapers` cannot import.
4. Create and verify the complete SQLite/file/config rollback package. Do not
   mark `backup_ready` merely because its directory exists.
5. Swap runtime/source, replace private bindings marker-last, and migrate.
6. Run one bounded wake in the foreground. Prove it with
   `upgrade_safety fresh-record --started-at …`; identify freshness by the
   record's UTC timestamp, never by history length because the history is
   capped. Both `completed` and `no_due_work` are healthy result codes.
7. Validate the database, configuration, cloud proof, co-resident services,
   and retained canaries; then bootstrap both services and verify they load.

Rollback is phase-driven. Before `backup_ready`, quarantine partial candidates
and restart the original services without trying to restore files that may not
exist. At and after `backup_ready`, restore only the resources that the reached
phase could have changed. `upgrade_safety rollback-plan` validates this phase /
backup combination; the wrapper retains prior runtimes, sources, and backups
for human inspection.

Operational details:

- Run it **without** `sudo` — it validates as the operator and re-executes
  itself as root; a `sudo` prefix makes it exit silently at its first check.
- `current-candidate-path` must contain an **absolute** path.
- Confirm the wrapper restarts the dashboard backend; after manual recovery,
  use `kickstart -k` for that persistent service or it keeps serving the
  previous code.
- Never print an entire LaunchDaemon description or plist: it can contain
  credentials. Read only named non-secret fields with `PlistBuddy`, and
  redirect `launchctl print` to `/dev/null` when checking loaded state.

## Credentials

- OpenReview (monitor + scrapers): `EnvironmentVariables` in the
  local-control plist — survives runtime swaps; the plist is 0600 for this
  reason. A runtime-relative `.env` copy does not survive upgrades.
- Codex device auth, Google ADC, Resend key/recipients: the dedicated
  role's private credential root, managed only through
  `automation.agent_credentials` / the marker-last replacement tooling.

## Certificate renewal

The dashboard's NIU-issued DigiCert leaf expires 2026-12-03 and does not
auto-renew. Start renewal with NIU DoIT by early November 2026: new CSR
without overwriting the live key, validate the returned chain, then a
stopped-proxy atomic swap.
