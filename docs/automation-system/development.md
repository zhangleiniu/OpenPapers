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

The target date-only path is currently an uninstalled Python interface,
`automation.event_dates.initialize_event_dates`. Its tests inject an
`EventDateProvider`; there is deliberately no production or live command yet:

```bash
python -m unittest automation.tests.test_event_dates -v
```

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
