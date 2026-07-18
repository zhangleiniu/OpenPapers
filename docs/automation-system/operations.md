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
exact-rollback replacement. Points that have bitten before:

- Run it **without** `sudo` — it validates as the operator and re-executes
  itself as root; a `sudo` prefix makes it exit silently at its first check.
- `current-candidate-path` must contain an **absolute** path.
- A healthy first wake may legitimately report `no_due_work`.
- Restart the dashboard backend afterwards (`kickstart -k`), or it keeps
  serving the previous code.

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
