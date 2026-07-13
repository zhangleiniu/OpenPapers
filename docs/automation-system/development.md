# Automation development runbook

This page tells a zero-context agent how to begin, make a scoped change, verify
it, and leave a useful handoff.

## Start every task

From the repository root:

```bash
git status --short
git log -5 --oneline
python -m unittest discover -s automation/tests -v
```

Then inspect, as applicable:

```text
AGENTS.md
docs/automation-system/README.md
docs/automation-system/architecture.md
docs/automation-system/roadmap.md
docs/automation-system/work-packages.md
docs/automation.md
automation/deployment/README.md
automation/conferences.json
automation/monitor.py
automation/prefect_flows.py
automation/contracts.py
automation/configuration.py
automation/domain.py
automation/scheduling.py
automation/discovery.py
automation/providers/gemini.py
automation/run_discovery.py
automation/verification.py
automation/html_verification.py
automation/config/venue_catalog.v1.json
automation/config/policies.v1.json
```

Do not infer live GCP, Prefect, email, Mac, or Codex health solely from files.
Use read-only runtime checks when the task requires current deployment facts.

Select exactly one `Ready` package from `work-packages.md`. The package is the
default thread and commit boundary. Investigation, implementation, tests,
review fixes, documentation, and commit stay in that thread; the next package
starts a new thread. Do not begin a blocked package or silently absorb work
from an adjacent package.

## Establish scope before editing

Record these answers in the task plan or handoff:

- Which roadmap phase and acceptance criterion does the change implement?
- Is it cloud control plane, Mac execution plane, core scraper, or docs only?
- Does it add a network request, credential, persistent state transition,
  executable action, notification, or code-writing agent capability?
- What is the safe failure behavior?
- How will the behavior be tested without depending on a live changing page?

If work crosses phases, split it so safety prerequisites land before the
capability that depends on them.

## Implementation rules

### Keep automation optional

- Automation-only dependencies belong under `automation/deployment/` or a
  future automation-specific dependency group.
- Imports used by `main.py` and core scrapers must not require Prefect, GCP
  orchestration, email, or Codex.
- Prefer plain Python domain functions wrapped by thin Prefect tasks/flows.

Install the Phase 0/1 dependencies separately when developing automation:

```bash
python -m pip install -r automation/requirements.txt
```

Do not move it into the root requirements merely to make automation tests
available. The current monitor deployment has its own dependency file and does
not import the Phase 0 modules.

### Separate state, effects, and orchestration

- State models and transitions must be testable without Prefect.
- Network providers and storage use interfaces that can be replaced by
  fixtures/fakes.
- Prefect flows coordinate typed functions; they should not contain venue
  parsing or large policy branches.
- Action routing returns typed action data, not a shell command string.

### Preserve reproducibility

- Store raw/sanitized source snapshots on first observation and meaningful
  change.
- Version prompts and JSON schemas.
- Store the model/provider identifier with discovery output.
- Structural parser fixes include a minimal source fixture and regression test.
- Never place authentication headers, cookies, tokens, or personal secrets in
  a fixture.

### Evolve state safely

- Version registries and persisted schemas.
- Add migrations or backwards-compatible reads for persisted changes.
- Test replay and duplicate delivery.
- Cloud state remains single-writer while SQLite/GCS is used.
- Job results are immutable and addressed by stable job ID.

The implemented schemas are compatibility contracts. Additive optional fields
may extend a pre-deployment contract when old artifacts remain valid; semantic
or required-field changes need a new schema/config version and an explicit
compatibility reader. Keep the target venue catalog separate from
`automation/conferences.json` until a later phase deliberately migrates the
deployed monitor.

### Escalate permissions deliberately

- New discovery is shadow-only before executable routing.
- New domains require crawl-policy classification.
- Existing scraper execution precedes Codex escalation.
- Code-changing Codex jobs use isolated worktrees and stop for review.
- Dataset promotion and MustCite deployment are separate actions.

## Testing

Minimum automation checks:

```bash
python -m unittest discover -s automation/tests -v
python -m compileall -q automation
python postprocessing/generate_statistics.py --check
git diff --check
```

Run the full repository test suite when shared scraper, validator, config, or
utility code changes. For a conference-year data change, follow every item in
the repository-level `AGENTS.md` rather than treating the automation tests as
sufficient.

Tests should include, where applicable:

- schema success and rejection cases;
- state transition and idempotency cases;
- retry and clock-controlled reminder cases;
- budget, cooldown, concurrency, and circuit-breaker cases;
- source/crawl policy allow and deny cases;
- fixture-based parsing and verification;
- redaction and secret-boundary cases;
- duplicate job delivery and immutable-result cases;
- failure paths, not just the happy path.

Phase 0 contract/domain tests are split into:

```bash
python -m unittest automation.tests.test_contracts -v
python -m unittest automation.tests.test_domain -v
python -m unittest automation.tests.test_scheduling -v
```

Phase 1 discovery tests and the explicit remote-call gate are split into:

```bash
python -m unittest automation.tests.test_discovery -v
python -m unittest automation.tests.test_gemini_provider -v
python -m unittest automation.tests.test_run_discovery -v
python -m automation.run_discovery --venue icml
```

The final command must refuse to construct a live provider because `--live` is
absent. A real development canary is operator-visible and intentionally
unmetered:

```bash
python -m automation.run_discovery --live --venue icml --year 2026
```

It requires a project setting and Application Default Credentials, retains
artifacts outside tracked source, does not read or write the canonical budget
ledger, and does not update state or call a scraper. This exception applies
only to the explicit manual development CLI; future scheduled or automatic
discovery must use the configured budgets and circuit breakers.

The Phase 2.1/P2.1R verifier-foundation checks are:

```bash
python -m unittest automation.tests.test_verification -v
```

They use only fake fetchers and temporary fixture storage. New builders emit
version 2 verification contracts; `validate_request_against_discovery` and
`validate_verification_result` also provide semantic compatibility reads for
consistent version 1 artifacts. P2.1's `EvidenceFetcher` contract performs one
request with automatic redirects disabled and retains a sanitized redirect
edge without requesting its target. A P2.2 adapter must independently classify
and policy-gate the next URL before another request; the implemented P2.2
coordinator does so without changing the P2.1 foundation module.

The P2.2 deterministic HTML checks are:

```bash
python -m unittest automation.tests.test_html_verification -v
```

`automation/html_verification.py` keeps HTML/list/metadata/proceedings checks
outside the foundation module. Tests use only explicit source profiles,
sanitized fixtures, fake no-redirect responses, and temporary snapshots. Every
redirect hop is classified and policy-gated; fixture results cover exact
venue/year and date identity, distinct counts, metadata completeness, current
proceedings indexes, replay, conflicts, and the P2.2 scope boundary.

The P2.3 deterministic PDF checks are:

```bash
python -m unittest automation.tests.test_pdf_verification -v
```

`automation/pdf_verification.py` remains independent of the HTML module. It
uses stable bounded sampling, the injected one-request fetcher, explicit
`pdf_fetch_for_processing` and `store_internal_copy` permissions, sanitized
fixtures, and temporary snapshot roots. Tests cover per-hop redirects and
policy closure, exact cited sample selection, final HTTP status, minimum/actual
size, Content-Length, `%PDF-` signature, replay, forged provenance, and P2.3
scope boundaries. Persistent history remains outside the PDF verifier.

The P2.4 persistent control-state checks are:

```bash
python -m unittest automation.tests.test_control_state -v
```

`automation/control_state.py` is a standard-library SQLite repository distinct
from the deployed monitor's `StateStore`. Tests use temporary databases and
fixed clocks to cover empty-database migration, rejection of future and
unrecognized schemas, cloud-only ownership, lease overlap/expiry/renewal,
atomic verification-bundle retention, semantic no-op replay, ordered validated
reopen, optimistic conference-state revisions, rollback, stale writes, and
stored corruption. P2.4 does not reduce findings, promote facets or milestones,
compute schedules, or return actions; keep those behaviors in P2.5.

A live fetch adapter must add transport-level DNS/SSRF protections and
operational crawl controls before use; the existence of the injected interface
is not permission to make live calls.

Scheduling tests use an injected timezone-aware clock. Keep venue catalogs free
of year-specific month/date assumptions; discovery records candidates, a
deterministic verifier promotes supported dates into conference-year
milestones, and only then may `automation/scheduling.py` compute
`next_check_at`.

Live tests must be opt-in and must respect the same crawl policy as production.
They cannot be the only proof of correctness.

## Documentation updates

Update documentation in the same change when:

- a roadmap phase/status or acceptance criterion changes;
- a state, blocker code, schema, action, ownership boundary, or policy changes;
- a new runtime service, credential, deployment step, or operator action is
  introduced;
- current production behavior diverges from `docs/automation.md` or the
  deployment guide.

Keep stable design in this directory and concrete current deployment commands
in `automation/deployment/`. Do not put secret values, copied `.env` content,
or local authentication files in either place.

## Commit and handoff checklist

Before committing:

1. inspect `git status --short` and the full diff;
2. confirm unrelated user changes are excluded;
3. run the applicable checks above;
4. verify Markdown links and paths;
5. state which roadmap acceptance criteria are complete;
6. state what remains planned or shadow-only;
7. list runtime migrations or operator actions, if any;
8. include rollback implications for state/deployment changes.

A good handoff is concise and factual:

```text
Implemented:
Roadmap phase/criterion:
Still not implemented:
Tests run:
Runtime action required:
Risks/follow-up:
```

Do not mark a phase `Implemented` when only interfaces, mocks, schemas, or
documentation exist. Use `Shadow` when it observes live systems but cannot yet
take production action.
