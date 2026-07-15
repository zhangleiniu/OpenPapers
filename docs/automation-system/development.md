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

The target date-only path is installed behind the disabled production gate.
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
```

The fake-tested production composition is selected by the installed service
but returns before constructing adapters while `external_effects_enabled=false`.
Its private configuration contract pins the tracked target file hash,
Gemini project/location/model, an absolute Codex binary path, monthly/systemic
agent limits, a separate monthly date-lookup ceiling, worktree retention
bounds, and the SHA-256 fingerprint of the approved plain recipient. Resend API
key, sender, and recipient values are supplied
separately at runtime and must never be committed.

Post-install credential preparation and safe status checks use the dedicated
role and private internal root. These commands do not call a model or send
email:

```bash
python -m automation.agent_credentials --internal-root <private-root> prepare
python -m automation.agent_credentials --internal-root <private-root> status
```

`codex-login`, `google-adc-login`, and `configure-resend` are interactive
operator actions. Resend configuration additionally requires the fixed service
to be stopped and the exact `--confirm-service-stopped` flag. Never pipe or
paste their credential values into logs, prompts, or tracked files.

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
6. Update `README.md`'s "Current implementation"/"Not yet built" split and
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
