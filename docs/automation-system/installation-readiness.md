# Agent automation installation readiness

This checklist separates evidence gathering from the later state-changing
installation. Repository code and fake tests do not prove that the dedicated
Mac role, private configuration, production SQLite, external volume, Codex
binary, ADC, or Resend recipient are ready.

## Current boundary

The installation gate completed with schema-10 state and private agent-control
v2 configuration. The unchanged fixed LaunchDaemon selects the installed
composition, but `external_effects_enabled=false` prevents every new external
effect. The deterministic baseline monitor remains active. Treat the checklist
below as the completed installation record and as prerequisites for any future
replacement; activation and live canaries remain separate operations.
The repository now has disabled-only marker-last refresh and dedicated-role
credential/canary tooling. Current host status evidence confirms private Codex
authentication and impersonated Google ADC; Resend remains unconfigured and no
new live canary has completed. These observations do not authorize a canary or
activation.

## Installed automation dependency gate

The fixed LaunchDaemon uses its own Python environment, not the repository
`.venv`. Before requesting root, stopping the service, refreshing the runtime,
or running any live canary, verify the installed interpreter can import every
declared automation runtime dependency:

```bash
PYTHON=$(/usr/libexec/PlistBuddy -c \
  'Print :ProgramArguments:0' \
  /Library/LaunchDaemons/org.openpapers.local-control.plist)
"$PYTHON" - <<'PY'
import importlib

for name in ("dotenv", "google.auth", "google.genai", "jsonschema"):
    importlib.import_module(name)
print("installed_automation_dependencies=verified")
PY
```

The host-local disabled install and refresh wrappers run this gate before
privilege escalation and again in the privileged preflight. A failure is an
installation defect, not permission to substitute the repository `.venv`,
inherit maintainer credentials, or retry a live canary. Install only the
tracked `automation/requirements.txt` into the fixed interpreter, keep
external effects disabled, and repeat this import-only gate before requesting
fresh live authority.

## Read-only audit gate

Run as the dedicated service role, substituting the private state path without
copying it into Git, prompts, tickets, or logs:

```bash
python -m automation.control_state_migration audit --state <control-state>
```

The command emits no path. Continue only when `status` is `ok`,
`quick_check_ok` and `migration_ready` are true, `owner_kind` is
`local_control_plane`, and all three active/in-flight counts are zero. A
WAL journal result is not migration-ready because immutable audit cannot prove
that uncheckpointed sidecar content is absent; stop/checkpoint it under a
separate operational procedure. A blocked result requires fixing role/path
access outside the repository; do not relax file permissions merely to make
the audit pass.

## Isolated rehearsal gate

Create a new empty directory owned by the dedicated role with mode `0700` on
storage that is not the production control directory. Then run:

```bash
python -m automation.control_state_migration rehearse \
  --state <control-state> --rehearsal-root <private-empty-directory>
```

The command uses SQLite backup, refuses an existing rehearsal database,
migrates only the copy, and emits no path. Require `source_unchanged: true`,
the code's current schema version, and preserved target-table counts. Retain
the private rehearsal copy only as long as needed for review; it is not a
production replacement or rollback backup.

## Inputs requiring maintainer approval

Before installation, review and approve:

- the exact tracked target cohort and its SHA-256 fingerprint;
- Gemini project, location, and model plus ADC availability for the dedicated
  role;
- the absolute Codex binary and its version;
- monthly run and systemic-failure limits;
- the separate monthly date-lookup ceiling;
- worktree age, retained-count, and per-wakeup deletion limits;
- the Resend sender and address-free recipient fingerprint;
- external `agent-runs` ownership/capacity and the fact that the old ICML 2026
  canary is unregistered and outside cleanup ownership.

Never place API keys, recipient addresses, ADC files, SMTP values, or Codex
authentication files in tracked configuration or command output.

## Separately authorized installation sequence

The following actions are intentionally not authorized by an audit or
rehearsal:

1. Stop and verify the current LaunchDaemon is inactive; keep the paused cloud
   rollback scheduler paused.
2. Create a fresh timestamped private SQLite backup and verify it before any
   migration.
3. Install versioned private agent configuration/secrets and a new marker
   fingerprint with byte-exact rollback copies.
4. Migrate the production database once, using the same code revision that
   passed rehearsal.
5. Replace/reload only the fixed OpenPapers LaunchDaemon and verify one
   no-network/no-due wakeup.
6. Separately authorize a date-provider canary, Codex canary, and Resend email
   canary. A canary permission does not authorize the next canary.

A later refresh must use a fresh candidate runtime and clean no-remote source,
retain byte-exact rollback copies, and replace v2 bindings marker-last through
`replace_disabled_agent_production_root`. Both installed and candidate
configuration must remain `external_effects_enabled=false`; refresh permission
cannot be reused for activation. The production database is already schema 10
and must not be migrated or downgraded during refresh.

If any step fails, stop the service. Restore the pre-migration database and
private config/marker/plist as one set before restarting; never attempt an
in-place schema downgrade. Do not resume the cloud rollback path until the
local service is confirmed stopped and the no-overlap procedure is reviewed.
