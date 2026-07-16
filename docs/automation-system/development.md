# Automation development workflow

This page defines the common development and verification workflow for
`automation/` changes. Read [`README.md`](./README.md) and
[`architecture.md`](./architecture.md) first.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -r automation/requirements.txt
```

Install `automation/deployment/requirements.txt` only when working on the
paused Cloud Run rollback path.

## Commands

Run the deterministic baseline monitor:

```bash
python automation/monitor.py
python automation/monitor.py --venue icml --year 2026
python automation/monitor.py --no-write
```

Run the existing evidence-strict discovery adapter in unmetered manual
development mode. This path is separate from the new date-only provider.
`--live` requires a GCP project and Application Default Credentials and makes
a real Gemini call:

```bash
python -m automation.run_discovery --venue icml
python -m automation.run_discovery --live --venue icml --year 2026
```

The target date-only path is installed behind the enabled production gate;
development and test commands still inject fakes and make no automatic call.
`automation.event_dates.initialize_event_dates` tests inject an
`EventDateProvider`; there is deliberately no standalone automatic live command:

```bash
python -m unittest automation.tests.test_event_dates -v
python -m unittest automation.tests.test_due_policy -v
python -m unittest automation.tests.test_codex_agent -v
python -m unittest automation.tests.test_agent_worktree_retention -v
python -m unittest automation.tests.test_agent_run_notifications -v
python -m unittest automation.tests.test_agent_production -v
python -m unittest automation.tests.test_control_state_migration -v
python -m unittest automation.tests.test_agent_status -v
python -m unittest automation.tests.test_source_change_hints -v
```

The Codex standing prompt receives the due policy's accepted retry window. For
`not_ready`, it asks for an evidence-based UTC `suggested_retry_at`, with timely
checks during active/partial publication and an announced revision/proceedings
date used when appropriate. It may still return null; the controller then uses
its configured fallback. Tests must not infer a retry from explanation text.

Monitor-change hint tests use sanitized event mappings and temporary journal /
schema-10 databases. A changed available event is de-identified to venue/year,
advances at most one existing future schedule after the wake's ordinary work,
and creates no attempt. Replay, missing schedules, unconfigured targets, and a
newer agent run are deterministic; the next wake still owns every due-policy
gate.

The fake-tested production composition is selected by the installed service.
It returns before constructing adapters whenever
`external_effects_enabled=false`; the current production installation is
enabled, so ordinary development must continue using injected fake adapters.
Its private configuration contract pins the tracked target file hash,
Gemini project/location/model, an absolute Codex binary path, monthly/systemic
agent limits, a separate monthly date-lookup ceiling, worktree retention
bounds, and a bounded sorted list of SHA-256 fingerprints for approved plain
recipients. Legacy schema-2 single-recipient configuration remains readable;
interactive configuration upgrades it to schema 3. Resend API key, sender, and
recipient values are supplied
separately at runtime and must never be committed.

The tracked target file is a bounded annual cohort policy with an explicit
allowlist equal to all 14 annual catalog venues. It does not enroll continuous
JMLR because terminal `success` would miss later papers in the same year. Its
America/Chicago rollover adds the next year in October and moves the active
window in January while preserving all older durable state. Tests that
exercise the boundary inject an explicit date; ordinary runtime loading uses
the local calendar. The enabled installed runtime remains pinned to the
previous three-venue explicit-2026 file until a separately authorized
production upgrade.

Post-install credential preparation and safe status checks use the dedicated
role and private internal root. These commands do not call a model or send
email:

```bash
python -m automation.agent_credentials --internal-root <private-root> prepare
python -m automation.agent_credentials --internal-root <private-root> status
```

`codex-login`, `google-adc-login`, and `configure-resend` are interactive
operator actions. Resend configuration additionally requires the fixed service
to be stopped, the exact `--confirm-service-stopped` flag, and a non-secret
`--recipient-count` from 1 through 10. Recipient addresses are prompted
individually and must be unique. Never pipe or paste their credential values
into logs, prompts, or tracked files.

Live adapter checks are intentionally three subcommands with three unrelated
flags:

```bash
python -m automation.agent_canary --internal-root <private-root> \
  --repository-root <runtime> --external-root <execution-root> \
  gemini --venue icml --year 2026 --authorize-gemini-live
python -m automation.agent_canary --internal-root <private-root> \
  --repository-root <runtime> --external-root <execution-root> \
  codex --venue icml --year 2026 --authorization-id <id> \
  --authorize-codex-live
python -m automation.agent_canary --internal-root <private-root> \
  --repository-root <runtime> --external-root <execution-root> \
  resend --authorization-id <id> --authorize-resend-live
```

These examples document interfaces; they are not standing live authorization.
One canary flag never authorizes either other adapter or automatic activation.

External-effects control is a separate four-command boundary. The cloud proof
is produced by an ignored host wrapper after querying the exact retained GCP
resources; it contains only schema version, paused state, active execution
count, and a UTC observation time. `audit` probes the fixed LaunchDaemon and is
read-only:

```bash
python -m automation.agent_activation audit \
  --internal-root <private-root> --repository-root <runtime> \
  --execution-root <execution-root> --state <control-state> \
  --cloud-proof <fresh-private-cloud-proof>
```

`rehearse-disabled`, `activate`, and `rollback` additionally require a stopped
service, a fresh private backup destination, and different exact authorization
flags. These interfaces are documented for review; no flag shown in
documentation is standing authority to run it. In particular, disabled
rehearsal/refresh permission cannot be reused with
`--authorize-external-effects-activation`.

The migration helper has two explicit modes. `audit` is read-only and never
prints the supplied path. `rehearse` creates and migrates a new SQLite backup
inside an already-created private directory; it refuses an existing
destination:

```bash
python -m automation.control_state_migration audit --state <control-state>
python -m automation.control_state_migration rehearse \
  --state <control-state> --rehearsal-root <private-empty-directory>
```

Run those commands as the dedicated service role for production evidence.
They do not authorize installation or production migration. See
[`installation-readiness.md`](./installation-readiness.md).

Enabled production status is also read-only. Exact canary paths and expected
Git identities stay in a private schema-1 baseline; the tracked command emits
only booleans for branch, HEAD, status-digest, and remote-count matches. An
ignored privileged host wrapper may inspect both differently owned canaries,
then must install the generated proof as a mode-0600 file owned by the service
role. The proof is valid for 15 minutes, like the separately generated cloud
proof:

```bash
python -m automation.agent_status canary-proof \
  --baseline <private-canary-baseline>
python -m automation.agent_status report \
  --internal-root <private-root> --repository-root <runtime> \
  --execution-root <execution-root> --state <control-state> \
  --cloud-proof <fresh-private-cloud-proof> \
  --canary-proof <fresh-private-canary-proof>
```

The private baseline has exact fields `schema_version` and `canaries`. It has
exactly two entries named `codex_installed` and `icml_2026`; each entry contains
`path`, `head`, `branch`, `status_sha256`, and `remote_count`. Baseline creation
is an explicit operator act because it blesses the current Git state. Neither
command writes SQLite, calls a provider, sends email, or changes service/cloud
state. The module is installed in enabled production. Generating its required
fresh private proofs remains an operator action. The disabled-only refresh
command rejects enabled production and must not be used for future upgrades.

The venue dashboard is a narrower scheduling view and does not need cloud or
canary proofs. Run it as the account that can read the schema-10 database from
the installed runtime directory:

```bash
<installed-python> -m automation.agent_dashboard \
  --state <control-state> --bind 127.0.0.1 --port 8765
```

It refuses any bind other than `127.0.0.1`, rereads state immutably on each
page request, and has no mutation endpoint. Production manages that backend as
a LaunchDaemon. A separate `_openpapers` Caddy LaunchDaemon exposes only the
fixed NIU private interface with local-CA HTTPS and Basic Auth. From the NIU
network or VPN, open `https://archer.cs.niu.edu:8443/`.

The page shows all catalog venues even when not enrolled, and distinguishes
deterministic monitor registration from coding-agent schedule state. The
username is `openpapers`. Copy
`/Users/Shared/OpenPapers-dashboard-local-ca.crt` from the server and explicitly
trust it on the client; never move the Caddy private key or password verifier.
Do not weaken SQLite permissions or expose the application listener directly.

## Checks

Minimum checks for an automation-only code change:

```bash
python -m unittest discover -s automation/tests -v
python -m compileall -q automation
python postprocessing/generate_statistics.py --check
git diff --check
```

Run the broader suite when shared scraper, config, validator, or utility
code changes:

```bash
python -m unittest discover -v
python -m compileall -q main.py config.py utils.py scrapers postprocessing automation
```

Use saved sanitized fixtures for tests. A live network call, live agent
invocation, or Mac installation requires the package to state that
explicitly and the operator to authorize it separately — no package inherits
that permission by default.

## Change workflow

1. Read the required docs in `README.md`'s order.
2. Identify which roadmap phase (in `roadmap.md`) the change belongs to and
   its acceptance criteria. Do not select work from `archive/work-packages.md`.
3. Create a local ExecPlan under `.agent/plans/` when `.agent/PLANS.md`
   requires one (multi-component features, schema/storage migrations, a new
   executable capability, or material security/operational risk — a new
   frequency-gate or agent-execution module qualifies; a small fixture or
   doc fix does not).
4. Implement the narrowest change that satisfies the phase's acceptance
   criteria. Keep strict types at scheduling, isolation, budget, persistence,
   and notification boundaries; do not constrain the coding agent with a
   deterministic list of allowed source or scraper actions.
5. Run the checks above.
6. Update `README.md`'s "Current implementation"/"Not yet active or built" split and
   `roadmap.md`'s status table in the same change if phase status, persisted
   schemas, or safety policy changed.
7. Inspect the full diff and exclude local agent context before committing.

## Documentation hygiene

- Non-archive pages describe only current behavior or the active target.
- Historical P0-P6 packages, canary reviews, and deleted component designs
  stay under `docs/automation-system/archive/`.
- `docs/local-p4*-operations.md` files are ignored, host-specific operational
  records. Preserve them locally for audit/rollback, but never link them as
  required public documentation or copy their private details into Git.
- `docs/automation.md` describes the deployed monitor and rollback boundary;
  this directory describes the target control plane.
- A frequent LaunchDaemon wakeup is not a conference check. Documentation
  must distinguish local due selection from network, discovery, and agent
  effects.
