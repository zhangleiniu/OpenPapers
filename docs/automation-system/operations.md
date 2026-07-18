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
