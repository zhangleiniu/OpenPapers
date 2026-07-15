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
automation/production_discovery.py
automation/production_verification.py
automation/verification.py
automation/html_verification.py
automation/pdf_verification.py
automation/control_state.py
automation/lifecycle.py
automation/control_plane.py
automation/cases.py
automation/reminders.py
automation/notifications.py
automation/notification_integration.py
automation/job_queue.py
automation/mac_worker/
automation/local_service/
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
- Is it the current cloud baseline, target local control/execution plane, core
  scraper, or docs only?
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
compute schedules, or return actions; those behaviors remain separated in the
P2.5 modules.

The P2.5 lifecycle reduction and typed-routing checks are:

```bash
python -m unittest automation.tests.test_lifecycle -v
```

`automation/lifecycle.py` remains pure: it revalidates retained bundles,
reclassifies positive evidence, promotes monotonic facets/milestones, invokes
the existing reducer/scheduler, and returns immutable action intents without
performing them. `automation/control_plane.py` is only a lease/revision-aware
composition layer over P2.4. Tests replay compatible v1 artifacts and every
catalog venue/lifecycle shape through temporary repositories. They also prove
that untrusted, conflicting, continuous-conference, stale-readiness, and lost
lease paths cannot return or persist an executable effect. P2.S live review is
not part of the P2.5 tests; its separate authorization and reviewed result are
recorded in `phase2-live-review-2026-07-13.md`.

The P2.6 guarded automatic discovery effect checks are:

```bash
python -m unittest automation.tests.test_production_discovery -v
python -m unittest automation.tests.test_discovery -v
python -m unittest automation.tests.test_contracts -v
python -m unittest automation.tests.test_local_control_plane -v
```

`automation/production_discovery.py` builds a concrete
`automation.local_control_plane.DiscoveryEffect` around the existing
`DiscoveryService`, `GeminiSearchGroundingProvider.from_environment`,
`JsonBudgetLedger`, and `ArtifactStore`. It requires explicit private artifact,
budget-ledger, and health-ledger paths plus a validated, backwards-compatible
`automatic_discovery` policy block (existing policy artifacts without it
remain valid for every other reader). Before any production provider is
constructed or budget reserved, it durably checks and claims a separate
versioned, process-safe automatic-discovery health ledger distinct from the
attempt ledger: a same-venue/same-fingerprint cooldown and a distinct-venue
systemic circuit (opened only by a closed set of provider/transport/
output-shape failure categories, never venue-specific content categories)
both block before construction. Budget exhaustion finalizes as a guard skip,
never a cooldown. Tests use only fake providers and temporary private roots;
they cover a successful round trip, typed-failure cooldown and its expiry,
budget exhaustion as a guard skip, three distinct venues opening one systemic
circuit while venue-specific validation failures never do, circuit expiry,
cooldown/in-flight state surviving a new adapter process (including a
crash-safe in-flight blocker that never counts toward the systemic
threshold), corrupt-ledger closure before construction, concurrent-writer
safety, the required policy block, explicit-path construction, low-confidence
escalation returning only the primary result, a real
`run_local_control_wakeup` round trip, and the static
execution/service-import scope boundary. Nothing is installed or connected to
`automation/local_service/production.py`, and no test makes a live provider
call.

The P2.7 guarded automatic verification effect checks are:

```bash
python -m unittest automation.tests.test_production_verification -v
python -m unittest automation.tests.test_live_fetch -v
python -m unittest automation.tests.test_contracts -v
python -m unittest automation.tests.test_local_control_plane -v
```

`automation/production_verification.py` builds the concrete, uninstalled
`VerificationEffect` from explicit snapshot/health/review paths. Its strict
review loader requires current dated evidence for all catalog domains and the
grounding redirect, projects only the existing `CrawlPolicyGate` fields, and
fails before fetch authority on missing/stale dimensions. A separate locked,
atomic ledger durably guards each venue/year/source request. Tests use only
fake fetchers and temporary roots to cover strict HTML/PDF bundles, restart,
cooldown expiry, concurrent claims, redirects, budgets, 403/429/
`Retry-After`, CAPTCHA, partial evidence, unknown domains, corruption, and a
real local-wakeup round trip. The only network activity in P2.7 was the
bounded operator review recorded in
`p2-7-production-crawl-policy-review-2026-07-14.md`; no verifier test made a
live request. Nothing imports or changes `automation/local_service/`.

The P2.9 grounding-redirect resolution and COLT/PMLR profile checks are:

```bash
.venv/bin/python -m unittest automation.tests.test_grounding_resolution -v
.venv/bin/python -m unittest automation.tests.test_gemini_provider -v
.venv/bin/python -m unittest automation.tests.test_html_verification -v
.venv/bin/python -m unittest automation.tests.test_pdf_verification -v
.venv/bin/python -m unittest automation.tests.test_production_verification -v
```

These tests replay only the sanitized P2.8S redirect-only fixture and fake
HTML/PDF responses. The installed `google-genai` model exposes no resolved URL
beyond `GroundingChunkWeb.uri`, so `automation/grounding_resolution.py`
contains the closed reviewed mapping for `colt`/2025 only. Unknown or signed
wrapper shapes return no URL. The fake fetch log must contain only catalog
sources, PMLR listing identity/count must pass before link extraction, and
each stable PDF sample must independently pass the existing processing and
internal-copy gates. Do not run P2.9S or any live command as part of these
checks. The production crawl-policy artifact is unchanged and its
`vertexaisearch.cloud.google.com` entry remains `denied`.

P2.9S's separately authorized live run is complete and recorded in
`p2-9s-live-canary-review-2026-07-14.md`. The real response omitted the PMLR
domain label, so P2.9 correctly fetched only the official COLT page and
retained no action.

The P2.10 official-page-link derivation checks are:

```bash
.venv/bin/python -m unittest automation.tests.test_grounding_resolution -v
.venv/bin/python -m unittest automation.tests.test_gemini_provider -v
.venv/bin/python -m unittest automation.tests.test_html_verification -v
.venv/bin/python -m unittest automation.tests.test_production_verification -v
```

These tests replay only a new sanitized fixture,
`automation/tests/fixtures/phase2/p2-10-colt-official-page-only.json`,
reproducing the exact P2.9S source-label shape (one `learningtheory.org`
label, no `proceedings.mlr.press` label), plus fake HTML/PDF responses.
`automation/grounding_resolution.py`'s `is_known_colt_official_page` and
`automation/providers/gemini.py`'s `_add_known_official_page_pdf_candidate`
add a `pdf`-claim candidate citing the already-cited official page only when
no PMLR-sourced candidate exists. `automation/html_verification.py`'s
`extract_pmlr_volume_link` derives an exact unsigned PMLR volume-root link
from an already-fetched, already identity-verified page, returning `None` for
a missing, ambiguous, cross-host, signed, percent-encoded, or non-volume
candidate. `automation/production_verification.py` fetches the official page,
confirms its own venue/year identity, extracts that link, and only then
reuses P2.9's unchanged PMLR identity/count/link extraction and P2.3's PDF
signature sampling; every closed shape leaves the finding `review_required`
with zero PDF fetch attempts and no request to `proceedings.mlr.press` or the
grounding wrapper. Do not run P2.10S or any live command as part of these
checks. The production crawl-policy artifact, `automation/
production_wakeup.py`, `automation/production_wakeup_canary.py`, and
`automation/local_control_plane.py` are unchanged.

P2.10S is now the sole `Ready` package: a third separately authorized
fresh-root live proof of the unchanged P2.8S canary boundary against the same
`colt`/2025 venue/year, requiring separate live authorization.

The generalized live fetch adapter retains its transport-level DNS/SSRF and
operational crawl controls. The existence of that injected interface or the
P2.7 review artifact is not permission to make a live call; only an explicit
Ready live package plus separate user authorization may exercise it.

P2.S implements that adapter and its isolated manual composition. Focused
checks and the mandatory non-live refusal are:

```bash
python -m unittest automation.tests.test_live_fetch -v
python -m unittest automation.tests.test_verification_shadow -v
python -m unittest automation.tests.test_run_verification_shadow -v
python -m automation.run_verification_shadow \
  --discovery-root /nonexistent --output-root /tmp/openpapers-p2s-shadow-refusal
```

The last command must fail before constructing a transport because `--live`
is absent. An authorized manual review requires the retained discovery root
and a fresh or previously marked shadow root to be explicit:

```bash
python -m automation.run_verification_shadow --live \
  --discovery-root "$SCRAPER_DATA_ROOT/automation/discovery" \
  --output-root "$SCRAPER_DATA_ROOT/automation/verification-shadow/<review-id>" \
  --year 2026
```

The command uses `automation/config/p2s_shadow_policy.v1.json`, never the
default empty crawl policy as an implicit grant. It rejects IP-literal,
private, reserved, or mixed DNS targets; pins HTTPS to a reviewed public
address while retaining original-hostname TLS verification; follows redirects
only through the existing per-hop gate; and writes only snapshots, strict
artifacts, an isolated SQLite database, and inert summaries. A completed root
replays its first summary without a new network call. It is not a scheduler,
production state writer, dispatcher, or deployment surface. The first
15-venue record is `phase2-live-review-2026-07-13.md`.

The P3.1 persistent unresolved-case checks are:

```bash
python -m unittest automation.tests.test_cases -v
python -m unittest automation.tests.test_control_state -v
```

`automation/cases.py` is pure and accepts explicit typed observations or human
controls; it does not consume P2.5 intents or calculate reminders.
`ControlStateRepository` schema version 2 stores one current case per
venue/year/blocker plus immutable revisions and events under the existing
lease. Tests use temporary databases and fixed clocks to cover stable-key and
event-ID deduplication, meaningful-change timestamps, dormant-only automatic
reactivation, resolve/snooze/ignore/reactivate, unresolved-only listing,
version-1 migration, replay, corruption, lease loss, and transaction rollback.
No P3.1 test constructs or sends a notification.

The P3.2 clock-controlled reminder and grouped-digest checks are:

```bash
python -m unittest automation.tests.test_reminders -v
```

`automation/reminders.py` accepts validated case states, the existing policy,
and an injected aware clock. It returns defensive aged states plus stable due
slots and one immutable digest grouped weekly/monthly/dormant. Tests cover
exact windows, `last_meaningful_change_at` aging, active and expired snoozes,
closed/dormant cases, stable replay, grouping, and invalid clocks. The module
does not persist state, create a notification intent, classify delivery
retries, render/redact a message, or import a storage, orchestration, network,
email, or other transport dependency. P3.3 consumes this result only when a
caller supplies it explicitly; P3.4 now provides the separate repository-driven
shadow coordination boundary.

The P3.3 notification intent, redaction, and fake-delivery checks are:

```bash
python -m unittest automation.tests.test_notifications -v
python -m unittest automation.tests.test_control_state -v
python -m unittest automation.tests.test_contracts -v
```

`automation/notifications.py` accepts an explicitly supplied event or P3.2
digest, builds a strict redacted intent with stable source/evidence/run IDs,
and coordinates an injected transport only after `ControlStateRepository`
schema version 3 has committed an in-flight attempt. The repository uniquely
claims each event/reminder slot, suppresses delivered, permanent, and unresolved
in-flight replay, and permits only an explicit retry after a typed retryable
failure. Tests use fake transports, temporary databases, and fixed clocks;
they cover migration from valid version 1/2 databases, redaction, retry
classification, replay, source conflicts, corruption, lease loss, and
ambiguous post-acceptance failure. There is no concrete transport, external
request, recipient, Prefect integration, case/action/reminder consumer, or
deployment change. P3.4 composes this boundary without calling a transport;
P3.S completed the separately authorized synthetic delivery-canary action.

The P3.4 persistent shadow-integration checks are:

```bash
python -m unittest automation.tests.test_notification_integration -v
python -m unittest automation.tests.test_notifications -v
python -m unittest automation.tests.test_control_state -v
```

`automation/notification_integration.py` consumes typed P2.5 transition and
case actions, commits stable case observations separately, and registers
immediate output only for transition or meaningful case events. It also lists
unresolved repository cases, applies P3.2's injected-clock projection, filters
already claimed reminder slots, and registers one grouped digest for every
remaining due item. Registration reuses schema version 3's strict intent/source
tables but creates no attempt row: tests require every output to remain
`pending` with `attempt_count == 0`, including after reopen. Exact replay is a
no-op, a stable source cannot move to a different intent, and an output failure
cannot roll back a previously committed case event.

P3.4 has no command or scheduler and must not import or invoke the P3.3
transport protocol, Prefect, email/SMTP, HTTP/webhooks, a cloud notification
provider, recipients, credentials, or any external service. P3.S is the only
package that may add a separately authorized non-sensitive delivery/fatigue
canary; that authority is not inherited by P3.4 tests or callers.

The P3.S concrete transport and synthetic-canary checks are:

```bash
python -m unittest automation.tests.test_resend_notifications -v
python -m unittest automation.tests.test_run_notification_canary -v
python -m automation.run_notification_canary \
  --output-root /tmp/openpapers-p3s-refusal \
  --approved-recipient-sha256 \
  0000000000000000000000000000000000000000000000000000000000000000
```

The final command must refuse before constructing a transport because
`--live` is absent. An explicitly authorized canary sets `RESEND_KEY`,
`OPENPAPERS_CANARY_EMAIL_FROM`, and `OPENPAPERS_CANARY_EMAIL_TO` only in the
manual process environment, computes the normalized recipient SHA-256 without
retaining the address, and uses a new ignored output root:

```bash
python -m automation.run_notification_canary --live \
  --output-root "$SCRAPER_DATA_ROOT/automation/notification-canary/<review-id>" \
  --approved-recipient-sha256 "<approved normalized recipient SHA-256>"
```

The command builds exactly one three-item synthetic digest, refuses any second
request even after a typed retryable outcome, writes an in-flight claim before
transport I/O, and records only bounded results plus recipient/receipt
fingerprints in JSON. A marked
delivered root may be reopened to prove suppression, but it must not be copied
or pointed at a P3.4 root. Removing the three canary environment variables
disables delivery; there is no deployed resource or schema migration to roll
back. Provider acceptance is not independent mailbox confirmation. The first
authorized record is
`docs/automation-system/phase3-delivery-review-2026-07-13.md`.

The P4.1 immutable job, typed queue, and cloud-submission checks are:

```bash
python -m unittest automation.tests.test_job_queue -v
python -m unittest automation.tests.test_contracts -v
```

`automation/job_queue.py` emits only version 2 jobs at the queue boundary,
recomputes their full SHA-256 identity, and maps every existing job type to one
fixed queue in the inert `openpapers-mac` process-pool blueprint. Only an
explicitly supplied P2.5 existing-scraper action has a producer. The
asynchronous submission coordinator uses the job ID as the idempotency key;
the Prefect deployment adapter receives its client and deployment IDs by
injection. Tests use a fake client and sanitized local fixtures and assert
no flow-run creation for invalid jobs, queue drift, missing/misassigned
deployments, or wrong keys.

P4.1 has no command, live-client factory, pool/queue/deployment provisioning,
worker, Mac/`launchd` setup, scheduler connection, control-state write,
scraper/Codex execution, GCS result path, or production integration. Do not
use its local blueprint as evidence that Prefect resources exist.

The P4.2 fake-only Mac package and health checks are:

```bash
python -m unittest automation.tests.test_mac_worker -v
python -m unittest automation.tests.test_mac_worker_health -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/mac_worker/runtime.py` revalidates P4.1 envelopes and produces a
non-result `simulated` observation. The isolated Prefect flow accepts only
`queue_envelope` and disables retries/result persistence. Health tests use
temporary repository/data/auth-marker paths and a fake Prefect configuration
probe; no test reads auth contents, starts Codex, or contacts Prefect. The
plist is parsed locally and must contain the fixed pool/type arguments, a
restrictive umask, placeholders, and no credential or shell.

The runbook at `automation/mac_worker/README.md` records this as a frozen
prototype. Do not install its Prefect plist, create its work pool, or treat its
health command as evidence of a live local scheduler. New local service work
belongs to the separate completed `automation/local_service/` P4.L3 package;
the Prefect prototype remains unchanged.

The P4.3 local safety and replay checks are:

```bash
python -m unittest automation.tests.test_mac_worker_safety -v
python -m unittest automation.tests.test_mac_worker -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/mac_worker/safety.py` uses only a private Mac-local journal,
`fcntl` venue/year locks, injected disk usage, and injected fake execution
handles. It writes an exact claim before fake start, atomically promotes only
confirmed success, suppresses exact completed replay, and never ages out an
ambiguous claim; that claim blocks every job for its venue/year. Confirmed
stopped failure/cancellation/timeout may retry with the same job ID;
unconfirmed stop remains recovery-required. Tests use
temporary private directories and child processes and retain no command,
result, artifact, configured path, or raw exception.

Offline semantics are a fixed Prefect pull policy: no delivered envelope means
no local state, local queue, TTL, resubmission, or replacement job ID. This is
not evidence of a live worker or visible real queue.

The P4.4 immutable result and cloud-consumption checks are:

```bash
python -m unittest automation.tests.test_job_results -v
python -m unittest automation.tests.test_control_state -v
python -m unittest automation.tests.test_contracts -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/job_results.py` cross-validates strict manifest/result identities
against the full P4.1 v2 job. Its injected bucket adapter writes the manifest
before the result with `if_generation_match=0`, accepts only byte-identical
replay after a failed precondition, and pins reads to observed generations.
Control-state schema version 4 introduced one exact pair under the cloud lease;
`automation/job_result_consumer.py` only composes the read and record steps.
Tests use a fake bucket and temporary SQLite database and cover partial-write
recovery, conflict, generation drift, migration, restart replay, lease loss,
and corruption.

P4.4 constructs no GCS client, reads no credential, changes no external
resource, connects no worker, and applies no result to lifecycle state.

The P4.L1 local ownership and bounded due-work checks are:

```bash
python -m unittest automation.tests.test_local_scheduler -v
python -m unittest automation.tests.test_control_state -v
python -m unittest automation.tests.test_domain -v
python -m unittest automation.tests.test_scheduling -v
```

Control-state schema version 5 adds one immutable cloud/local owner plus
bounded wakeup and stable due-selection history. Existing version 1-4
databases remain cloud-owned and refuse local migration. The plain-Python
runner uses one injected aware clock and the existing lease; tests cover
due/not-due, missed and duplicate wakeups, hard bounds, contention, restart,
owner mismatch, and durable active-run ambiguity using fixtures and temporary
SQLite only. It has no action callback, command, environment expansion,
orchestration import, network client, CLI, or production path.

The P4.L2 fixture-only local composition checks are:

```bash
python -m unittest automation.tests.test_local_control_plane -v
python -m unittest automation.tests.test_local_scheduler -v
python -m unittest automation.tests.test_control_state -v
python -m unittest automation.tests.test_lifecycle -v
python -m unittest automation.tests.test_notification_integration -v
python -m unittest automation.tests.test_reminders -v
```

Schema version 6 adds a separate bounded wakeup-plan record so selections can
remain active while composed work runs. `automation/local_control_plane.py`
holds one local lease across injected fake discovery and verification, strict
retention/lifecycle reduction, case and pending-shadow integration, and one due
reminder projection. It completes only after each selected schedule advances or
clears. Exact completed replay invokes no fake; any failure after selection
leaves durable ambiguity. Tests use only sanitized fixtures, fake effects and
clocks, and temporary SQLite. They create no delivery attempt, job, command,
network call, daemon, host mutation, or production-state write.

Before opening any future durable control database with schema-v6 code, stop
overlapping writers and take a backup; rollback requires restoring that backup.
P4.O remains paused after its Prefect feasibility gate failed before resource
creation.

The P4.L3/P4.LS headless-service and isolated-shadow checks are:

```bash
python -m unittest automation.tests.test_local_service -v
```

`automation/local_service/` derives SQLite and bounded atomic health/run
records below one private internal root, which must be disjoint from the
external execution volume. Its typed local health gate runs before an injected
wakeup and its default mount probe only observes availability; tests replace
both with fakes and use temporary paths. Missing/unsafe storage, probe failure,
or corrupt records makes no effect call and does not open control SQLite.

The pure plist renderer fixes one low-impact hourly system LaunchDaemon with
no shell, environment, keepalive, socket, credential, or launchd-managed log.
Rollback data names only its exact label/plist and preserves state, records,
repository, external data, and unrelated labels. The standalone command has
no concrete effect and must return `effect_unconfigured`. Do not create or
access a role account/volume, write the plist to a host path, call the service
manager, or claim operational health in P4.L3 tests.

P4.LS adds a private exact marker, a separate shadow renderer, and an explicit
`--isolated-shadow` mode whose only effect calls
`automation.local_scheduler.run_scheduler_wakeup`. Tests prove missing,
conflicting, or unsafe markers fail before state; empty isolated state acquires
the local owner and exact replay retains one completed wakeup; and the mounted
directory probe accepts a private directory only when it is backed by a
non-root mount. The ordinary command remains `effect_unconfigured`.

The authorized 2026-07-14 host record additionally proves root-owned runtime
isolation, duplicate wakeup, missing-volume closure, ambiguous-state
preservation/recovery, exact rollback/reinstall, SSH disconnect, reboot, and
co-resident health. Host-specific evidence stays in an ignored local operations
record; automated tests do not require root, launchd, a real volume, or a live
service. Repository documents are not proof that the external host remains
healthy, so use read-only runtime checks when current status matters.

P4.LC completed the explicit no-overlap writer cutover. The local production
boundary validates a generation-bound legacy monitor backup, performs the
existing six-source daily monitor/notification work, and runs schema-v6 local
due scheduling separately. The cloud schedule is paused and may be resumed
only after the local label stops.

The P5.1 approved-command registry checks are:

```bash
python -m unittest automation.tests.test_command_registry -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/command_registry.py` revalidates strict v2 jobs and returns only a
job-bound structured specification for the fixed scraper or validator
repository entry point. Tests cover exact typed flag derivation, stable replay,
Codex rejection, forged/legacy input, and arbitrary shell/path/flag/argv/env or
expansion-shaped input. The module must not resolve an interpreter, repository
root, staging/data path, or environment and must not import/invoke the scraper,
validator, subprocess, local service, or a network/cloud dependency. P5.2 owns
isolated staging execution; P5.3 owns validator execution and manifests.

The P5.2 isolated staging and fake-supervision checks are:

```bash
python -m unittest automation.tests.test_staging_executor -v
python -m unittest automation.tests.test_command_registry -v
python -m unittest automation.tests.test_mac_worker_safety -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/staging_executor.py` accepts only a strict existing-scraper job,
binds P5.1's fixed `main.py` invocation to trusted runtime paths and one
private job-fingerprint root, and requires explicit disjoint canonical data.
Its exact child environment inherits nothing, disables dotenv, and binds data
and logs only to staging. Atomic checkpoints allow same-root resume after a
confirmed stopped failure, timeout, or cancellation; process success is
suppressed on replay, while running/ambiguous state blocks automatically.

Focused tests must inject fake launchers/handles/clocks and use a temporary
fake repository, fake executable, staging root, and canonical root. Do not
invoke the concrete subprocess adapter, `main.py`, a scraper, or the validator.
P5.2 has no CLI/runtime connection and produces no manifest/result or
validation claim. P5.3 now owns independent staged validation and manifests;
P5.4 now owns fixture-only composition with existing locks/disk gates and
result routing.

The P5.3 independent staged-validation and manifest checks are:

```bash
python -m unittest automation.tests.test_staging_validation -v
python -m unittest automation.tests.test_contracts -v
python -m unittest automation.tests.test_staging_executor -v
python -m unittest automation.tests.test_job_results -v
python -m unittest automation.tests.test_job_queue -v
```

`automation/staging_validation.py` reads only a strict existing-scraper job
whose P5.2 checkpoint is exactly `process_succeeded`. It hashes a bounded safe
regular-file staging tree into a candidate inventory/manifest below a separate
private artifact root, then requires a bound strict validation job before it
retains a versioned report and validation manifest. Tests use temporary roots
and cover announced/metadata/archival requirements, count, duplicate and
required-field issues, PDF existence/size/signature, exact replay, symlink and
path escape, candidate drift, corruption, identity/policy downgrade, and the
no-runtime/no-canonical scope boundary.

P5.3 does not invoke P5.2's subprocess adapter or the validator CLI, publish a
P4.4 job result, classify or route a failure, connect to P4.3/the scheduler/the
LaunchDaemon, generate statistics, or write canonical data. P5.4 owns those
fixture-only composition/result/routing concerns, and P5.S owns any authorized
actual scraper canary.

The P5.4 guarded execution-pipeline and readiness-routing checks are:

```bash
python -m unittest automation.tests.test_execution_pipeline -v
python -m unittest automation.tests.test_mac_worker_safety -v
python -m unittest automation.tests.test_staging_executor -v
python -m unittest automation.tests.test_staging_validation -v
python -m unittest automation.tests.test_job_results -v
```

`automation/execution_pipeline.py` accepts only a strict existing-scraper job
and coherent private/disjoint configuration. It holds P4.3's process-safe
venue/year lock, two-threshold disk gate, and exact claim across injected P5.2
execution, P5.3 validation, and P4.4 publication. Its closed observation
distinguishes ready/partial/failure/retry/cancellation/ambiguity/completed
replay and assigns transient, operational, or structural classes without
retaining exception text. Confirmed stopped process failure is same-root
resumable; ambiguity retains the claim. Manifest-first publication failure is
byte-identically replayable from retained checkpoint/report times.

Focused tests must use fake launchers, disk state, publishers, clocks, and
temporary private roots. P5.4 has no CLI, concrete cloud client, installed
service caller, canonical writer, statistics generator, promotion path, or
real scrape authorization. P5.S owns the first actual shadow/canary and host
recovery proof.

The P5.S manual existing-scraper shadow checks are:

```bash
.venv/bin/python -m unittest \
  automation.tests.test_execution_shadow \
  automation.tests.test_run_execution_shadow -v
.venv/bin/python -m automation.run_execution_shadow \
  --shadow-root /private/p5s-review \
  --canonical-data-root /reviewed/repository/data \
  --repository-root /reviewed/repository \
  --python-executable /reviewed/regular/venv/python \
  --venue colt --year 2025 --expected-count 181 \
  --timeout-seconds 3600
```

The second command must refuse before creating a root because `--live` is
absent. An authorized run requires a normalized regular (not symlinked)
interpreter that resolves the reviewed environment, a fresh or exactly marked
private repository-external root, the live local-label/cloud-zero-overlap/disk/
co-resident gates, and a retained canonical fingerprint. On macOS the fixed
sandbox profile denies every child write outside the exact shadow root,
including repository and canonical roots, while allowing isolated staging.

P5.S first used a one-second timeout and required confirmed process-group stop,
a cleared claim, and zero artifact/result files before resuming the exact same
job and root. The accepted COLT 2025 run then produced 181 papers and 181 valid
PDFs, passed a separate `postprocessing/validate_year.py` archival check, and
published only to the private create-only local store. Exact replay must return
`duplicate_completed` without changing the process log or any retained tree.
The durable review is
`phase5-existing-scraper-shadow-review-2026-07-14.md`.

This command is manual and uninstalled. Do not place it in a plist or scheduler,
point it at P2/P3 output, use a GCS client, update statistics, promote data,
deploy MustCite, enable Cloud Scheduler, or invoke Codex. An ambiguous claim or
unconfirmed stop requires retained-root manual recovery and must never be
cleared merely to force replay.

The P5.5 durable local action/job persistence and bounded dispatch checks are:

```bash
python -m unittest automation.tests.test_execution_retention -v
python -m unittest automation.tests.test_execution_dispatch -v
python -m unittest automation.tests.test_control_state -v
python -m unittest automation.tests.test_local_control_plane -v
```

Control-state schema version 7 adds an immutable current `execution_job` row
keyed by the recomputed job ID and an append-only numbered
`execution_attempt_history`, retained only under the local single-writer
lease. `automation/execution_retention.py` accepts only the exact
`ActionIntent` values a lease-protected local reduction just produced,
filters to `queue_existing_scraper`, and lets
`ControlStateRepository.retain_existing_scraper_action` recompute the job
with `automation.job_queue.build_scrape_job_from_action`; a caller can never
supply job bytes, and every read reconstructs the action and rebuilds the
job to detect stored drift. It has no import on
`automation.execution_pipeline`, `automation.mac_worker`, or
`automation.staging_executor`, so `automation/local_control_plane.py` calls
it directly inside `_compose_selection` without adding a process-adjacent
dependency; `test_local_control_plane.py` proves a real single-shot
`pdf_ready` fixture retains exactly one job and exact wakeup replay adds
nothing new.

`automation/execution_dispatch.py` is the separate bounded step that may
call a real P5.4 effect. `dispatch_one_existing_scraper` claims at most one
pending job under a short lease, releases it before calling an injected
`ExistingScraperExecutionEffect`, and reacquires a lease afterward to
reconcile the typed observation. A `retry`/`cancelled` observation returns
the job to `pending` with an incremented attempt number; a terminal
observation closes the job permanently. An effect exception, a
`recovery_required` observation, a job-identity mismatch, or a failure while
reconciling leaves the attempt `in_flight` and is never reclaimed by elapsed
time. Tests use only temporary local-owned SQLite, a real verified action
fixture, fixed clocks, and injected fake effects; no scraper, validator,
network request, installed service, or Prefect/GCS/Codex/promotion path
exists.

Scheduling tests use an injected timezone-aware clock. Keep venue catalogs free
of year-specific month/date assumptions; discovery records candidates, a
deterministic verifier promotes supported dates into conference-year
milestones, and only then may `automation/scheduling.py` compute
`next_check_at`.

Live tests must be opt-in and must respect a reviewed crawl policy. Shadow-only
policy does not grant a future production caller permission. Live observation
cannot be the only proof of correctness.

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
