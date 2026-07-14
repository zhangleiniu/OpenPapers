# Mac worker package and launchd runbook

This directory is the P4.2 Mac-side foundation. It is not an installed worker
and is not connected to Prefect Cloud, GCP, the deployed monitor, production
scheduling, or an executable scraper/Codex path. Its Prefect flow only validates
typed queue envelopes and returns a `simulated` fixture observation. Actual Mac
installation and operational drills belong to P4.O and require separate
operator authorization.

## Package boundary

- `runtime.py` revalidates a P4.1 queue envelope and performs no job action.
- `prefect_support.py` contains the only Prefect imports, a fake-only fixture
  flow, and a local-settings probe that makes no API request.
- `health.py` checks local prerequisites and never reads Codex authentication
  file contents or reports configured paths, API URLs, or credentials.
- `requirements.txt` is isolated from the core scraper and cloud-monitor
  dependency sets.
- `launchd/org.openpapers.prefect-worker.plist.example` is an inert template.
  Repository placeholders make it unsuitable for loading as-is.

The template refuses to create a missing work pool, disables runtime package
installation, and disables Prefect's optional HTTP health-check server. A
missing pool therefore fails visibly instead of mutating Prefect, and health
remains a local pull command rather than a public inbound endpoint.

The local health command starts no worker and makes no network request:

```bash
.venv/bin/python -m automation.mac_worker \
  --repository-root "$PWD" \
  --data-root "$SCRAPER_DATA_ROOT" \
  --codex-auth-path "$HOME/.codex/auth.json"
```

It reports only stable check names, pass/fail states, and bounded reason codes.
The runtime check requires macOS and the repository's supported Python 3.12.
The Codex marker check establishes that an owner-readable regular file exists
without group/other permissions; it does not prove that a token is current or
execute a Codex process. The Prefect check establishes only that the selected
local profile contains an API URL and key; it does not contact Prefect or prove
that the pool, queues, or deployments exist.

## Future P4.O installation procedure

Do not perform this section as part of P4.2. P4.O must separately authorize
the Mac/Prefect/GCS changes and record the resulting reboot, SSH-disconnect,
offline-worker, and recovery drills.

1. Confirm a dedicated non-administrator login account, a Python 3.12 virtual
   environment, repository/data/log directories owned by that account, and a
   backup of any existing worker plist. Install only the isolated package
   dependency set:

   ```bash
   .venv/bin/python -m pip install -r automation/mac_worker/requirements.txt
   ```

2. Select the intended Prefect profile and perform its interactive login as
   the worker account. Keep the API key in the Prefect profile or approved
   credential store; never add it to the plist, repository, `.env`, shell
   history, or log files. Create/verify the `openpapers-mac` process pool,
   typed queues, and fake-only deployments only under the P4.O authorization.
   The launch template will not create a missing pool for you.

3. Copy the example to a staging path. Replace every `/ABSOLUTE/PATH/TO/...`
   placeholder with an absolute, worker-owned path. Do not add
   `PREFECT_API_KEY`, Codex credentials, GCP credentials, or a command shell to
   `EnvironmentVariables` or `ProgramArguments`. Validate before loading:

   ```bash
   plutil -lint /path/to/staged/org.openpapers.prefect-worker.plist
   grep -n '/ABSOLUTE/PATH/TO' /path/to/staged/org.openpapers.prefect-worker.plist
   ```

   `plutil` must report `OK`, and `grep` must return no placeholder. Install
   the reviewed copy at
   `~/Library/LaunchAgents/org.openpapers.prefect-worker.plist` with mode 600.

4. Bootstrap the per-user agent from a logged-in GUI session. A user agent is
   intentional: it uses the selected user's Prefect profile and avoids a
   public inbound service.

   ```bash
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/org.openpapers.prefect-worker.plist"
   launchctl kickstart -k "gui/$(id -u)/org.openpapers.prefect-worker"
   ```

5. Inspect without copying settings or credentials into a ticket or log:

   ```bash
   launchctl print "gui/$(id -u)/org.openpapers.prefect-worker"
   .venv/bin/prefect work-pool inspect openpapers-mac
   tail -n 100 /absolute/path/to/logs/openpapers-prefect-worker.stderr.log
   ```

   Then run the local health command above. Separately verify Codex login only
   when P4.O explicitly authorizes executing the Codex status command. P4.2's
   file-metadata signal is intentionally not an authentication canary.

6. Exercise only fake deployments during P4.O. Confirm the worker survives a
   Mac reboot and SSH disconnect, and that an offline worker leaves fake work
   queued and visible. Do not route scraper, validator, Codex, production
   control-state, or result-publication jobs until their later packages and
   gates are complete.

## Stop, rollback, and recovery

To stop a future installed user agent:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/org.openpapers.prefect-worker.plist"
```

Preserve logs before replacing a failed configuration. Restore the backed-up
plist, re-run `plutil -lint`, and bootstrap it only after determining that the
old version is still compatible with the configured Prefect resources. If the
worker is offline, leave queued work in Prefect; do not recreate jobs with new
IDs or manually mark them complete. P4.3 will define duplicate/offline
semantics, and P4.4 will define immutable result recovery. Never repair an
offline worker by editing the cloud-owned SQLite database from the Mac.

P4.2 itself requires no rollback or runtime action: no plist was loaded, no
profile was changed, no worker was started, and no external resource exists
because of this package.
