# Automation work packages

This document is the durable thread-level execution map for the optional
automation system. The roadmap defines phase outcomes; this page divides those
outcomes into coherent units of work that should normally use separate Codex
threads and separate reviewable commits.

Use this page to select exactly one package. Do not treat a phase or the whole
automation system as one indefinitely running task.

## Sources of truth

The documents have different responsibilities:

- `architecture.md` defines stable design boundaries and safety invariants;
- `roadmap.md` defines phase deliverables, acceptance criteria, and status;
- this file defines thread-sized work packages, dependencies, and handoffs;
- `development.md` defines the common development and verification workflow;
- local `AGENTS.md` defines repository instructions; and
- local `.agent/PLANS.md` defines how a task-specific ExecPlan is written.

Task-specific ExecPlans under `.agent/plans/` are local living documents. They
do not replace this map and are not committed. Durable decisions discovered
during implementation belong in architecture, roadmap, or this file.

## Thread and package rules

Stay in one thread while investigating, implementing, testing, responding to
review, updating documentation, and committing one package. Start a new thread
when that package has a clean commit and the objective changes to another row
below.

Use a fork only for genuinely competing designs. Sequential work such as
P2.1R followed by P2.2 uses a new thread, not a fork. A venue-specific parser
failure found during rollout becomes its own bug thread and returns to the
rollout thread after the fix is committed.

Unless a row explicitly says otherwise, every package must:

1. begin from a clean tracked working tree and inspect recent commits;
2. read the required documents in `README.md` order;
3. create or update a local ExecPlan when required by `.agent/PLANS.md`;
4. remain inside its Included boundary and honor its Excluded boundary;
5. use fakes, fixtures, and temporary storage for automated tests;
6. update durable docs when behavior, contracts, policy, or status changes;
7. run the checks in `development.md` plus narrower package tests;
8. inspect the full diff and exclude local agent context; and
9. end with a reviewable commit and a factual handoff.

No package inherits permission for a live network request, cloud mutation,
email delivery, Mac installation, scraper execution, Codex execution,
promotion, deployment, or data deletion. Such effects require the package to
state them explicitly and the operator to authorize them.

## Status vocabulary

- `Complete`: accepted and committed; no required work remains.
- `Review fix required`: an implementation commit exists, but review findings
  must close before dependent work starts.
- `Ready`: prerequisites are complete and this is an approved next task.
- `Blocked`: do not start until the listed dependency is accepted.
- `Planned`: later work whose immediate predecessor is not yet complete.
- `Shadow`: implementation exists and is being observed without production
  action authority.

Only one package should normally be `Ready` on the main sequential path.
Independent packages may both become ready after their shared interface is
stable, but they should use separate branches/worktrees if developed in
parallel.

## Current packages

P2.1R through P2.5 have completed the local verification, persistence,
lifecycle, scheduling, and inert-routing slices. P2.S has completed the
explicitly authorized 15-venue live shadow review using isolated roots and no
production action. P2.6 has completed the fixture-only production-capable
discovery effect plus durable per-venue cooldown and distinct-venue systemic
circuit guardrails. P2.7 has completed the fixture-only production-capable
verification effect, separate durable source cooldown, and dated per-domain
production crawl-policy review. P2.8 has completed the fixture-only automatic
discovery→verification→P2.5→P5.5 retention composition. P2.8S has run the
explicitly authorized live canary for the exact P2.8 composition: one real
Gemini discovery call and zero live HTTP fetches, reconfirming the
already-documented COLT source-shape gap and retaining no action. Because the
package's own acceptance text treats a no-action outcome as a failed canary,
P2.8S is `Review fix required`, not `Complete`. P2.9 has completed the
fixture-only grounding-redirect fix without weakening verification or crawl
policy. P2.9S then ran the separately authorized second live canary. It
resolved and fetched only the official COLT page, because this provider
response contained no `proceedings.mlr.press` domain label; no PDF target or
action resulted. P2.9S is therefore also `Review fix required`. P2.10 is the
sole `Ready` package: it fixture-tests deterministic extraction of the exact
PMLR link already present in verified official COLT HTML. Phase 2 remains
`Shadow`. See "Phase 2 packages" below.
P3.1 has completed the persistent
case slice, P3.2 has completed reminder aging and grouped digest data, P3.3 has
completed the fake-only durable delivery boundary, and P3.4 has completed
pending shadow-output integration. P3.S has completed one separately
authorized synthetic delivery/fatigue canary, so Phase 3 is now `Shadow`.
P4.1 has completed the immutable typed-job, fixed-queue, and fake-tested cloud
submission boundary without creating external resources. P4.2 has completed
the fake-only Mac receiving package, secret-safe local health checks, and
uninstalled `launchd` runbook. P4.3 has completed local locks, disk gates,
supervision, duplicate suppression, and offline policy over injected fakes.
P4.4 has completed strict immutable manifests/results, create-only
GCS-compatible publication, exact-generation reads, and lease-protected
exactly-once logical cloud consumption using injected fakes and temporary
state. P4.O is `Paused`: its live feasibility gate failed before resource
creation because the acceptable Prefect Cloud plan cannot create the required
hybrid process pool. P4.L1 has completed the immutable local-owner and bounded
fake-clock due-work scheduler foundation using only temporary SQLite. The
accepted local-first redesign preserves reusable P4 contracts, and P4.L2 has
completed fixture-only discovery/verification/lifecycle/case/reminder and inert
action composition under the local lease. P4.L3 has completed the uninstalled
credential-free headless service package with private internal paths, bounded
records, a missing-volume gate, and scoped rollback. P4.LS has completed the
authorized marker-gated scheduler-only installation plus coexistence and host
drills without production authority. P4.LC has completed the generation-bound
backup, capability-equivalent local monitor, no-overlap writer transfer,
health gates, and 96-second timed rollback. Phase 4 is implemented; P5.1 is
complete at the pure registry boundary, P5.2 is complete at the isolated
fake-tested staging/process boundary, and P5.3 is complete at the independent
staged-validation/manifest boundary. P5.4 is complete at the fixture-only
guarded composition/result-routing boundary. P5.S has completed one real COLT
2025 timeout/resume/success/replay shadow with canonical write denial and
private immutable results. P5.5 has completed fake-only durable action/job
persistence (control-state schema version 7) and bounded dispatch/
reconciliation with no installed caller or live request. Phase 5 remains
`Shadow`; no automatic runtime connection or canonical promotion is
authorized. P2.8 is `Complete` (the uninstalled automatic deterministic
verifier/action-source composition), but P2.8S — its separately authorized
live-evidence half — is `Review fix required`: its first authorized run
retained no action, and P2.9S's second authorized run also retained no action
because its real response omitted the PMLR domain label required by P2.9's
closed mapping. P5.5S remains `Blocked` pending P2.10/P2.10S evidence that a
verified official-page PMLR link reaches a genuine authoritative
`pdf_status=ready` facet. The local
LaunchDaemon is authoritative and the retained Cloud Scheduler job is paused.

### P2.1R — harden verifier contract semantics

Status: `Complete`

Depends on: the initial P2.1 implementation

Objective: make verification requests and results semantically trustworthy,
not merely JSON-Schema-valid, before HTML or PDF verifiers depend on them.

Included:

- derive and revalidate exact verification kinds from discovery targets;
- bind `target_kind` and `verification_kind` to the referenced claim or
  candidate milestone;
- reject evidence-free `verified` results and inconsistent overall statuses;
- require finding, facet, and milestone evidence IDs to reference retained
  observations/snapshots;
- require fetched observations to carry an allowed policy decision and a
  non-null policy domain;
- represent and retain a sanitized, replayable redirect edge or hop;
- reject or redact credential-bearing/signed query data before URLs enter
  manifests or result artifacts; and
- add negative regression tests for every rejected construction.

Excluded:

- HTML parsing, paper-list counting, venue identity extraction, or proceedings
  verification;
- PDF fetching, signature checks, or sampling;
- live HTTP, SQLite state, transitions, action routing, jobs, or deployment.

Acceptance:

- the known evidence-free, kind-drift, dangling-evidence, missing-policy-domain,
  lost-redirect, and signed-query counterexamples fail closed;
- valid P2.1 fixtures remain replayable or have an explicit compatibility
  update;
- no live or deployed component is changed; and
- focused, automation, repository, compilation, statistics, dependency, and
  diff checks pass.

Completed boundary: version 2 request/result contracts add exact target-kind
and redirect representation while semantic validators retain compatible v1
replay and reject all listed counterexamples. The package changes no live or
deployed component. P2.2/P2.3 remain responsible for content verification.

Historical package prompt:

```text
Execute P2.1R from docs/automation-system/work-packages.md. Follow local
AGENTS.md and .agent/PLANS.md, keep P2.2/P2.3 out of scope, run all required
checks, update durable docs, and commit the completed package.
```

### P2.2 — deterministic HTML evidence verification

Status: `Complete`

Depends on: P2.1R

Completed boundary: `automation/html_verification.py` composes the accepted
one-request fetch and snapshot interfaces into a bounded redirect chain whose
every target is independently classified and policy-gated. A bounded parser
and explicit source profiles verify title/heading venue-year identity, exact
candidate dates, plausible distinct paper counts, required metadata, and
actual current proceedings entries. Strict v2 results remain cited,
evidence-backed, replayable, and unable to carry an action.

Sanitized fixtures reject the EMNLP 2026 future proceedings promise and the
ACL 2026 page used as NAACL evidence. The IJCAI fixture verifies a distinct
accepted list and complete metadata while producing no PDF finding or facet.
Redirect denials, loops, limits, malformed/non-HTML input, uncited evidence,
incomplete metadata, and conflicting counts fail closed. The package adds no
live transport, PDF verification, persistent state, reducer, action, job, or
deployment behavior.

### P2.3 — deterministic PDF evidence verification

Status: `Complete`

Depends on: P2.1R

Completed boundary: `automation/pdf_verification.py` deterministically selects
a bounded, order-independent sample of exact URLs cited by requested PDF
claims. Every initial and redirected URL is independently catalog-classified
and crawl-policy-gated separately for `pdf_fetch_for_processing` and
`store_internal_copy` before an injected fake fetcher can be called and its
evidence retained. Final responses require HTTP 200, a configurable minimum
actual size of 1024 bytes by default, consistent Content-Length when present,
and `%PDF-` at byte zero.

Strict v2 results record sampled and valid counts and emit `pdf_status=ready`
only when every selected sample passes; a supported subset may be `partial`,
but missing, unsafe, untrusted, bad-status, undersized, incomplete, or
signature-invalid samples cannot produce readiness. Sanitized fixtures and
fake responses cover policy closure, redirects, loops, limits, stable replay,
invalid content, provenance forgery, and scope boundaries. The package adds no
HTML identity logic, live transport, persistent state, reducer, action, job,
redistribution grant, or deployment behavior.

### P2.4 — persistent control state and replay

Status: `Complete`

Depends on: P2.2 and P2.3

Completed boundary: `automation/control_state.py` provides schema-versioned
SQLite storage restricted to the cloud control-plane owner. An empty database
migrates to version 1; future, malformed, and populated unversioned databases
fail closed. One expiring singleton lease excludes overlapping writers, and
every verification or conference-state mutation validates its opaque token in
the same immediate transaction.

Strict discovery/request/result bundles are retained atomically with canonical
fingerprints. Semantic replay preserves the first payload as a no-op, identity
conflicts fail, and ordered reads revalidate all three contracts. Conference
state uses optimistic current revisions plus immutable snapshot history, with
identical-write no-ops, stale-write rejection, and compound rollback. Tests use
temporary databases and deterministic clocks. The package adds no finding
reducer, milestone/facet promotion, scheduling integration, action router,
live network, GCS adapter, deployed migration, monitor-state change, job, or
deployment behavior.

### P2.5 — verified lifecycle reduction and typed routing

Status: `Complete`

Depends on: P2.4

Completed boundary: `automation/lifecycle.py` revalidates each retained bundle,
recomputes source trust, and promotes only fetched official/archival evidence
into monotonic facets, release/verified milestones, the existing
evidence-backed transition reducer, and an evidence-time-derived schedule.
Continuous venues cannot acquire conference-specific state, conflicts remain
review-blocking, and every consumed verification identity makes ordered replay
idempotent.

The pure router returns stable immutable recheck, transition-notice,
case/review, and existing-scraper intents as data. A scraper intent requires an
overall-verified authoritative PDF-ready facet, catalog scraper capability,
and no execution blocker. It does not create/submit a job, execute a scraper,
notify, persist an action, or advance state to `ingestion_queued`.
`automation/control_plane.py` only composes one retained record with the P2.4
lease and optimistic revision APIs. Sanitized temporary-repository fixtures
replay compatible v1 artifacts plus every catalog venue and annual/continuous
lifecycle shape deterministically. There is no live network, P2.S observation,
production state/GCS/Prefect integration, case service, notification, Mac
worker, Codex, promotion, or deployment behavior.

### P2.6 — guarded automatic discovery effect

Status: `Complete`

Depends on: Phase 2 gate (P2.5)

P2.6 is fixture/fake-only. It builds a production-capable implementation of
`automation.local_control_plane.DiscoveryEffect` around the existing
`DiscoveryService`, `GeminiSearchGroundingProvider`, `JsonBudgetLedger`,
`ArtifactStore`, and policy-derived `BudgetLimits`. The automatic adapter
requires explicit private artifact/budget/health-ledger paths and rejects a
missing budget or health ledger. It does not reuse the existing unmetered
manual `automation.run_discovery --live` entry point.

Included:

- require the existing process-safe daily/global/per-venue/secondary-call
  attempt reservations before provider I/O and retain concurrency enforcement;
- add a separate versioned, process-safe automatic-discovery health ledger
  containing only typed failure category/fingerprint, venue, timestamps,
  cooldown deadline, and circuit state—never provider text or credentials;
- enforce a durable same-venue/same-fingerprint cooldown across process restart
  and a global circuit when the configured systemic threshold is reached by
  distinct venues; circuit/cooldown checks occur before budget reservation or
  provider construction and do not silently discard due work;
- add backwards-compatible policy fields for automatic discovery cooldown and
  circuit duration while continuing to use the existing
  `systemic_failure.venue_failure_threshold`; and
- strict typed configuration for project/location/model and explicit private
  roots, with no caller-supplied provider, arbitrary environment, path from
  discovery content, or executable callback.

Excluded:

- any live Gemini/Vertex AI call in this package's own tests (fakes and
  temporary ledgers only);
- connecting this effect to `automation/local_service/production.py` or any
  installed caller;
- verification, crawl policy, P2.5 reduction, or P5.5 retention changes; and
- P6.1 Codex-trigger classification/cooldowns. P2.6 protects automatic LLM
  discovery calls only and cannot authorize or suppress Codex execution.

Acceptance:

- the concrete adapter satisfies `DiscoveryEffect` and round-trips through
  `run_local_control_wakeup` using fake providers and temporary private roots;
- budget exhaustion, provider failure, and low-confidence escalation are
  handled exactly as `automation/discovery.py` already defines, with no
  silent discard;
- exact reopen proves cooldown and circuit state survive a new adapter process;
  same-fingerprint replay makes no provider call or budget reservation, three
  distinct-venue systemic failures open one circuit at the configured
  threshold, and clock-controlled expiry is explicit;
- partial writes, corrupt/unsafe ledgers, conflicting replay, concurrent
  reservation, and secret-shaped retained data fail closed; and
- static scope tests prove no import of `automation.execution_pipeline`,
  `automation.mac_worker`, or `automation.local_service`.

Completed boundary: `automation/production_discovery.py` adds
`AutomaticDiscoveryGuardPolicy` (an optional, backwards-compatible
`automatic_discovery` policy block plus the existing systemic threshold),
`AutomaticDiscoveryHealthLedger` (a versioned, locked, atomically replaced
JSON ledger distinct from the attempt ledger), `AutomaticDiscoveryConfig`, and
`ProductionDiscoveryEffect`. The effect durably claims one in-flight venue
attempt before constructing `GeminiSearchGroundingProvider` through its
existing `from_environment` entry point (fed a synthetic, config-derived
environment mapping, never the process environment) and before any budget
reservation; it finalizes that claim as eligible, a guard skip (budget
exhaustion, which is deliberately never a health event), or a typed
cooldown/circuit failure. A closed, non-venue-specific category set (transport,
grounding, and output-shape problems) is systemic-eligible; venue-content
validation categories (domain/claim/milestone mismatches) are not, so
unrelated per-venue content problems cannot open the shared circuit.

Fixture/fake tests in `automation/tests/test_production_discovery.py` cover a
successful round trip that clears health state, a typed failure that opens a
per-venue cooldown and blocks the next construction, cooldown expiry, budget
exhaustion as a guard skip, three distinct venues with the same systemic
fingerprint opening one circuit while venue-specific validation failures never
do, circuit expiry independent of per-venue cooldown, cooldown/in-flight state
surviving a new adapter process (including a crash-safe in-flight blocker that
never counts toward the systemic threshold), a corrupt ledger failing closed
before any provider construction, concurrent-writer safety, the required
`automatic_discovery` policy block, explicit-path construction, low-confidence
escalation returning only the primary result (no secondary provider is
wired), a real `run_local_control_wakeup` round trip, and the static
execution/service import boundary. Nothing is installed or connected to
`automation/local_service/production.py`; Phase 2 remains `Shadow`.

### P2.7 — guarded automatic verification effect

Status: `Complete`

Depends on: P2.6

P2.7 is fixture/fake-only. It wraps the existing `CrawlPolicyGate`,
`automation/live_fetch.py`'s `LiveHttpFetcher` (generalized from its current
P2.S-only usage), `automation/html_verification.py`, and
`automation/pdf_verification.py` into one module conforming to
`automation.local_control_plane.VerificationEffect`
(`verify(discovery, *, observed_at: datetime) -> Sequence[VerificationBundle]`).
It reuses P2.6's accepted persistent health-ledger/configuration conventions
under a separate verification namespace, which is the sequencing dependency;
it does not share failure state or authority with discovery.

Included:

- author a reviewed, non-`shadow_only` production crawl-policy entry for each
  in-scope domain rather than a bare allowlist. Each entry records trust and
  permission dimensions, robots/source-terms review, identifiable User-Agent
  and maintainer contact policy, concurrency/delay/jitter/request budget,
  redirect handling, `Retry-After`/429/403/CAPTCHA stops, cache/resume policy,
  and separate fetch/internal-retention/redistribution decisions;
- bounded read-only operator research of public robots and source-terms pages
  is explicitly permitted for that policy review. It uses no credentials,
  bypass, bulk crawl, PDF download, or automated verifier execution; captured
  documentation contains conclusions and public citations, not session data;
- durable venue/source failure cooldown using P2.6's accepted ledger semantics,
  plus the existing per-domain request limits and transport stop behavior; and
- deterministic composition of the existing P2.2/P2.3 verifiers behind the
  `VerificationEffect` protocol, with no new parsing or trust logic beyond
  what those modules already implement.

Excluded:

- any live network request in this package's own tests (fakes and temporary
  snapshots only, matching P2.2/P2.3/P2.S's existing test conventions);
- discovery, P2.5 reduction, or P5.5 retention changes;
- connecting this effect to `automation/local_service/production.py` or any
  installed caller; and
- crawl-policy classification for domains outside the reviewed list (those
  remain `review_required` and closed).

Acceptance:

- the effect satisfies `VerificationEffect` and round-trips through
  `run_local_control_wakeup` in tests using fake discovery/fetch doubles;
- every produced verification bundle passes `validate_verification_result`
  and cites only crawl-policy-allowed, classified sources;
- every production crawl-policy entry has dated evidence for all required
  permission/rate/stop dimensions; a missing or stale dimension remains
  `review_required` rather than inheriting P2.S authority;
- restart, cooldown, redirect, domain-budget, 403/429/`Retry-After`, CAPTCHA,
  partial evidence, and unknown-domain cases are fixture-tested; and
- static scope tests prove no import of `automation.execution_pipeline`,
  `automation.mac_worker`, or `automation.local_service`.

Completed boundary: `automation/production_verification.py` adds a strict
loader for the separate non-shadow production crawl policy, a bounded
`ProductionVerificationEffect`, and a versioned, locked, atomically replaced
automatic-verification health ledger. The 2026-07-14 review covers every
catalog official/archival domain plus the grounding redirect domain and is
recorded in
`docs/automation-system/p2-7-production-crawl-policy-review-2026-07-14.md`.
Each entry has an exact catalog trust role, date, public robots and terms/
copyright evidence, separate permissions and retention/redistribution
decisions, identification, concurrency/delay/jitter/budget, manual redirect
handling, immutable cache/resume semantics, and conservative stop behavior.
`ecva.net` remains `review_required`; the grounding redirect domain is denied
by its published robots policy; no entry grants redistribution.

The effect selects a deterministic bounded target set, creates one strict
request/result bundle per selected target, and delegates only to the accepted
P2.2/P2.3 gates, redirect logic, generic profiles, sampling, and validators.
Every authorized request first obtains a durable venue/year/source in-flight
claim; typed transport, HTTP 403/429/5xx, `Retry-After`, and CAPTCHA failures
open a restart-safe cooldown and stop the remainder of that invocation.
Policy refusal and per-domain budget exhaustion make no request and return
conservative evidence. Fixture/fake tests cover strict HTML/PDF results,
restart/expiry/concurrency, redirects, budgets, stop signals, partial evidence,
unknown domains, corrupt/stale policy/ledger closure, and a real
`run_local_control_wakeup` round trip. Nothing is installed or connected to
`automation/local_service/production.py`; no test makes a live request.

### P2.8 — automatic verified-action composition

Status: `Complete`

Depends on: P2.7

P2.8 is the actual automatic deterministic verifier/action-source gate. It
composes the accepted P2.6 and P2.7 effects through
`run_local_control_wakeup`, P2.5 reduction, and P5.5 action retention behind
one fixed production-capable wakeup effect. It is fixture/fake-only and
uninstalled: tests inject fake providers/fetchers and temporary local-owned
state, but exercise the exact automatic scheduling, budget, policy,
verification, reduction, and retention control flow that a later service may
call.

Included:

- strict configuration deriving private control/discovery/snapshot/budget/
  health roots without accepting paths or policy from web/model content;
- one bounded automatic wakeup that advances or safely retains due work and
  persists only genuine P2.5 actions through P5.5; and
- exact replay, budget/circuit closure, verification denial, partial commit,
  active-wakeup ambiguity, and no-action outcomes under fake clocks/reopen.

Excluded:

- any live LLM/network call, production database, LaunchDaemon change, or
  installation;
- P5.5 dispatch/P5.4 execution, canonical write, statistics write, promotion,
  deployment, or Codex; and
- a generic callback/command interface or synthetic/manual job injection.

Acceptance:

- a fake authoritative PDF-ready bundle automatically persists exactly one
  P5.5 action/job without caller-supplied action or job data;
- denied, ambiguous, conflicting, unsupported, circuit-open, or budget-exhausted
  inputs persist no executable action and make no later effect call;
- exact wakeup replay makes zero provider/fetch calls and creates no duplicate
  action/job; interruption remains durable ambiguity rather than auto-retry;
- the module remains unimported by the installed service and contains no live
  adapter construction or execution dispatch; and
- completion satisfies the implementation half of P5.5S's automatic
  verifier/action-source prerequisite, but not its live-evidence half.

Completed boundary: `automation/production_wakeup.py` adds
`ProductionControlPlaneConfig` (explicit control-state path plus one private
`automation_root` from which every discovery artifact/budget-ledger/
health-ledger path and every verification snapshot-root/health-ledger path is
derived), `build_production_effects`, and `run_production_control_wakeup`.
The function always constructs exactly one `ProductionDiscoveryEffect` and one
`ProductionVerificationEffect` from that configuration and hands them,
unmodified, to the accepted `run_local_control_wakeup` boundary; a caller
configures storage and Gemini identity but can never substitute a different
effect, provider, fetcher, action, or job. One `clock()` read produces a
single frozen timestamp reused by both the discovery effect and the wakeup
itself.

Fixture/fake tests in `automation/tests/test_production_wakeup.py` drive the
real production discovery/verification pair (through the same private
provider-factory/fetcher test seams P2.6/P2.7 already expose) to a genuine
authoritative `pdf_status=ready` facet — reachable because
`automation/lifecycle.py` promotes `pdf_ready` directly from `unknown` once
`pdf_status=ready` is supported, without requiring paper-list/metadata/
proceedings facets to also be ready — which P5.5 retention persists as
exactly one execution job. Exact replay then makes zero provider/fetcher
calls and creates no duplicate job. Invalid PDF evidence, an open discovery
circuit, and budget exhaustion all persist no action; the latter two refuse
before any provider/fetcher call and leave the wakeup durably `active`. A
two-venue scenario proves partial commit: an earlier venue's retained job
survives even though a later venue's discovery raises and the whole wakeup is
left `active`. Static scope tests prove no import of
`automation.execution_dispatch`, `execution_pipeline`, `mac_worker`,
`staging_executor`, or `local_service`. Nothing is installed or connected to
`automation/local_service/production.py`; no test makes a live call.

### P2.8S — authorized live discovery and verification canary

Status: `Review fix required`

Depends on: P2.8

P2.8S is the only package in this sequence allowed to exercise real P2.6/P2.7
effects. A manual `--live` command runs the exact P2.8 composition against a
private marked root and a non-production local-owned database. It performs
metered Gemini calls plus reviewed crawl-policy-gated HTTP fetches, may retain
a genuine P5.5 action/job, and never dispatches that job.

The package requires separate live authorization. It preselects a supported,
bounded archival venue/year whose reviewed sources and scraper capability can
exercise the action path. Failure to produce an eligible action is retained as
evidence and fails the canary; verifier or policy rules are never weakened to
force success. Exact completed replay makes no second live call. Missing
`--live`, unsafe/non-isolated roots, budget/circuit closure, policy denial, or
identity conflict refuses before the corresponding effect. The durable review
must be sanitized and record costs, requests, decisions, replay, partial
failure, and scoped rollback without credentials or private host paths.

P2.8S writes no production database, changes no LaunchDaemon, runs no scraper,
and has no canonical, statistics, promotion, deployment, notification, or
Codex authority. Completion supplies only the live-evidence half of P5.5S's
prerequisite; installation and automatic scraper dispatch remain P5.5S.

Implementation and live-run record: `automation/production_wakeup_canary.py`
adds a private root/marker lifecycle (`prepare_canary_root`, modeled on the
existing P5.S/P4.L3 "fresh or exactly marked" pattern, and refusing outright
if a production-control or host-shadow marker is found inside the supplied
root), a one-time seed of the canonical all-`unknown` schema-v1
conference-state row the preselected `colt`/2025 venue/year needs to become
due, and `run_canary`, which calls the unmodified `automation.production_wakeup.
run_production_control_wakeup` with both private injection seams left empty
so it builds the real `ProductionDiscoveryEffect`/`ProductionVerificationEffect`
pair. `automation/run_production_wakeup_canary.py` is the explicit `--live`
CLI wrapper. Nothing in `automation/production_wakeup.py` changed; P2.8S only
supplies the root, the one preselected venue/year, and a bounded sanitized
JSON evidence summary around that existing boundary. This infrastructure is
sound and is reused, not rebuilt, by P2.9S below.

The one authorized live invocation (repeated once more to prove replay) made
one real Gemini Search Grounding discovery call (two billed Vertex AI
requests, matching the existing two-stage provider's `attempt_cost == 2`) and
zero live HTTP verification fetches: every one of the nine grounded citations
Vertex AI returned was an unresolved `vertexaisearch.cloud.google.com`
redirect wrapper rather than an already-resolved catalog URL, so the existing
deterministic verifier correctly left all nine targets `review_required`
with `reason_code: unsupported_source_shape` before any crawl-policy fetch
claim. No action or job was retained; the wakeup completed cleanly and a
second `--live` invocation against the same marked root replayed with zero
further provider or fetch calls, confirmed by the unchanged discovery budget
ledger and artifact count. This reconfirms, on the real production-capable
pair, the same COLT source-shape gap the earlier P2.S 15-venue review already
recorded, and identifies its precise mechanism (redirect-wrapped grounding
citations). The sanitized record is
[`p2-8s-live-canary-review-2026-07-14.md`](./p2-8s-live-canary-review-2026-07-14.md).

Review finding: the package text above is explicit — "failure to produce an
eligible action is retained as evidence and fails the canary." No eligible
action was produced, so this run does not satisfy P2.8S's own acceptance
criterion, and the package cannot be `Complete` merely because its
infrastructure and safety boundary worked correctly. The finding that must
close before dependent work (P5.5S) starts is the specific, now-identified
gap itself — grounding-redirect citations cannot yet be verified — not a
defect in the canary machinery. P2.9 below fixed the exact first-run shape
without weakening verification or crawl policy; P2.9S's separate run then
exposed a second shape where the provider omitted the PMLR domain label. The
P2.8S finding therefore remains open pending P2.10/P2.10S. None of these
packages may touch P5.5S, Phase 6, Phase 8, or `automation/local_service/`.

### P2.9 — deterministic verification of grounding-redirect citations

Status: `Complete`

Depends on: P2.8S's review finding

P2.9 closes the exact, real gap P2.8S's live run located: Vertex AI Search
Grounding returned every citation for a genuine, fully archived venue/year
(`colt`/2025) as an opaque `vertexaisearch.cloud.google.com/
grounding-api-redirect/...` wrapper URL rather than an already-resolved
`learningtheory.org`/`proceedings.mlr.press` URL, so the existing
deterministic verifier — which by design only recognizes already-resolved
catalog source shapes — could not classify any of the nine targets. The
earlier P2.S 15-venue live review independently recorded the identical COLT
finding on 2026-07-13, so this is a real, reproducible, not one-off gap.

Included:

- investigate whether the existing `google-genai`/Vertex AI Search Grounding
  API exposes an already-resolved source URL or hostname anywhere in its
  grounding metadata (a different response field, attribute, or request
  option) that `automation/providers/gemini.py`'s existing grounding-source
  normalization is not yet reading; prefer and require that field
  deterministically if it exists;
- if no such field exists, add a narrowly scoped, explicitly reviewed
  capability that lets the deterministic verifier derive an already-known
  catalog URL from the discovery-supplied `domain` label alone — never from
  redirect content, and only for domains the P2.7 production crawl-policy
  review already covers — and verify that derived URL; the grounding-redirect
  domain itself is never fetched, followed, or otherwise contacted;
- add a supported COLT/PMLR source-shape profile to
  `automation/html_verification.py`/`automation/pdf_verification.py` so a
  correctly resolved PMLR proceedings/paper listing for COLT can pass
  deterministic identity, count, and PDF-signature checks — the one concrete
  source-shape gap both the P2.S and P2.8S live reviews independently found
  for this venue; and
- add a sanitized fixture reproducing the exact grounding-redirect-only
  citation shape P2.8S observed (see
  `p2-8s-live-canary-review-2026-07-14.md`), so the fix is provable without
  another live call.

Excluded:

- any weakening of the `vertexaisearch.cloud.google.com` entry's `denied`
  verdict in the P2.7 production crawl policy, or any code path that fetches
  or follows it; that domain's robots policy is the reason it is denied, and
  this package does not reinterpret or bypass that reading;
- any change to `automation/production_wakeup.py`,
  `automation/production_wakeup_canary.py`,
  `automation/run_production_wakeup_canary.py`,
  `automation/local_control_plane.py`, P2.5 reduction, or P5.5 retention —
  P2.9 only extends what P2.2/P2.3/P2.6/P2.7 already deterministically
  classify and verify;
- P5.5S, Phase 6, Phase 8, or `automation/local_service/` in any form; and
- any live network call in this package's own tests (fixtures/fakes only,
  matching every prior P2.x package).

Acceptance:

- the sanitized fixture reproducing P2.8S's exact grounding-redirect-only
  COLT/2025 citation shape now deterministically reaches a verified,
  promotable result through the fix, without the fetcher ever being called
  with the grounding-redirect domain;
- the P2.7 production crawl policy's `vertexaisearch.cloud.google.com` entry
  is unchanged and still `denied`;
- every existing P2.1R/P2.2/P2.3/P2.6/P2.7/P2.8 fixture suite remains green
  with no weakened, removed, or skipped assertion; and
- static scope tests prove no import of `automation.execution_dispatch`,
  `execution_pipeline`, `mac_worker`, `staging_executor`, or `local_service`.

Completed boundary: local inspection of the installed `google-genai` response
models confirmed that `GroundingChunkWeb` exposes only `uri`, `title`, and
`domain`; there is no second resolved source URL to consume. The new pure
`automation/grounding_resolution.py` therefore resolves only the exact
reviewed `colt`/2025 `learningtheory.org` and `proceedings.mlr.press` domain
labels to repository-known URLs. It accepts only an unsigned HTTPS
`vertexaisearch.cloud.google.com/grounding-api-redirect/...` provider URI,
never requests it, and returns no URL for any other venue/year/domain/path
shape. `GroundingSource.provider_uri` preserves the original wrapper as
non-fetchable artifact provenance while the normalized discovery cites the
resolved catalog URL.

When that resolved PMLR volume supports a paper-list or proceedings claim, the
Gemini adapter adds one deterministic PDF verification candidate without
changing the discovery `pdf_status`. The P2.9 COLT/PMLR HTML profile requires
exact venue/year identity and 100--500 distinct paper entries. The production
verifier fetches the retained volume only through the existing metadata gate,
extracts only unsigned same-host/same-volume PDF links, applies P2.3's stable
bounded sampling, and independently gates every selected PDF for processing
and internal retention before checking its HTTP result, size, Content-Length,
and `%PDF-` signature. A sanitized redirect-only fixture produces a strict
promotable `pdf_status=ready` result with three fake PDF requests and zero
grounding-redirect requests. Unknown mappings, unsafe links, implausible
counts, and failed identity remain closed. The P2.7 policy file is unchanged;
its grounding entry remains `denied`, and no redistribution permission was
added. Nothing is installed or connected to `automation/local_service/`.

### P2.9S — second authorized live canary against the P2.9 fix

Status: `Review fix required`

Depends on: P2.9

P2.9S was intended to provide the concrete evidence closing P2.8S's review
finding. It reuses
`automation/production_wakeup_canary.py`'s existing root/marker/seed/replay
infrastructure unchanged — that machinery is not what needs fixing — but is
its own separately authorized live event with a fresh marked root, not a
rerun of P2.8S's own recorded root/evidence.

Included:

- one fresh, separately authorized `--live` invocation of the P2.9-fixed
  composition against the exact same preselected `colt`/2025 venue/year
  P2.8S used, so success is attributable to the P2.9 fix and not to picking
  an easier venue after the fact;
- a durable sanitized review recording whether a genuine authoritative
  `pdf_status=ready` facet and exactly one retained `queue_existing_scraper`
  action were reached, using the same "failure is retained as evidence, not
  forced" discipline P2.8S already established; and
- if and only if this run reaches a genuine retained action, updating
  P2.8S's status entry above to record that its review finding is closed.

Excluded:

- installing anything, dispatching the retained job, writing production or
  canonical state, or touching P5.5S, Phase 6, Phase 8, or
  `automation/local_service/` — identical exclusions to P2.8S; and
  weakening verifier or crawl-policy rules to force success. A second
  no-action outcome must be retained just as honestly as the first; P5.5S
  remains blocked pending a further iteration rather than a relaxed rule.

Acceptance: identical to P2.8S's acceptance criteria (missing `--live`,
unsafe/non-isolated roots, budget/circuit closure, policy denial, or identity
conflict all refuse before the corresponding effect; exact completed replay
makes no second live call; the durable review is sanitized), plus: the run
must exercise `colt`/2025, not a substituted venue/year.

Live outcome: the one separately authorized invocation and its exact replay
completed on 2026-07-14. The real response contained seven grounding sources:
one `learningtheory.org` label and six unrelated labels, with no
`proceedings.mlr.press` label. P2.9 therefore resolved and fetched only the
official COLT page and correctly did not invent a PMLR citation. Ten strict
verification bundles contained four verified, five review-required, and one
rejected result; there was no PDF verification target, `pdf_status` remained
`unknown`, and no action/job was retained. Six allowed HTML requests all went
to `learningtheory.org`; the denied Google wrapper was never fetched. Exact
replay made no new selection and left the two-attempt discovery budget and
single discovery artifact unchanged. The sanitized record is
[`p2-9s-live-canary-review-2026-07-14.md`](./p2-9s-live-canary-review-2026-07-14.md).

Review finding: this second no-action outcome fails the canary's required
action result just as explicitly as P2.8S's first one. A bounded audit of the
already-retained, identity-verified official COLT HTML found one ordinary
`proceedings.mlr.press` link. P2.10 below owns a fixture-only extension that
may derive a PMLR verification candidate only from that retained official-page
link after exact COLT/year identity succeeds. It may not infer the URL merely
from venue/year, depend on a provider domain label, or contact the denied
wrapper. P2.10S owns any later separately authorized live proof.

### P2.10 — derive archival verification from a verified official-page link

Status: `Ready`

Depends on: P2.9S's review finding

P2.10 closes the exact variability P2.9S observed without broadening source
authority. P2.9 correctly handles a provider-supplied reviewed PMLR domain
label; P2.10 handles the distinct real shape where grounding supplies only the
official COLT page and that fetched, identity-verified page itself contains an
exact PMLR volume link.

Included:

- a sanitized fixture reproducing the P2.9S source-label shape and the bounded
  relevant structure of the retained official COLT page;
- extraction of unsigned HTTPS links only after the existing COLT/year
  identity check passes, with an exact `proceedings.mlr.press` volume-root
  shape and independent P2.7 metadata/PDF permission gates;
- reuse of P2.9's PMLR identity/count/link extraction and P2.3 PDF signature
  sampling after that official-page corroboration; and
- fixture proof that failed official identity, missing/ambiguous/multiple,
  cross-host, signed, encoded, or non-volume links remain closed and no
  request ever targets the grounding wrapper.

Excluded: a live call; a venue/year-only hardcoded PMLR fetch; changing the
P2.7 crawl review; weakening existing identity/count/PDF checks; any canary,
installed service, dispatch, P5.5S, Phase 6, Phase 8, or
`automation/local_service/` change.

Acceptance: the sanitized P2.9S shape reaches a strict promotable
`pdf_status=ready` result with only official/PMLR fake requests; every existing
P2.9 and earlier suite remains green unchanged; and negative fixtures prove an
unverified page or unsafe link cannot create a new fetch target or action.

### P2.10S — live proof of official-page archival-link derivation

Status: `Blocked`

Depends on: P2.10

P2.10S is a third separately authorized fresh-root run of the unchanged
P2.8S canary boundary against the same fixed `colt`/2025 venue/year. It must
reach a genuine authoritative `pdf_status=ready` facet and exactly one retained
action before P2.8S/P2.9S findings or P5.5S's live action-source prerequisite
are considered closed. It inherits every P2.9S exclusion and cannot be started
without separate live authorization.

## Phase 2 packages — verification and lifecycle state

Phase gate: P2.5 permits explicitly supplied authoritative retained evidence to
affect local control state and inert action data. Live network observations
occur only in separately authorized manual shadow/canary packages and remain
isolated from production.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P2.1 | Complete | Phase 1 | Verifier contracts, source trust, crawl gate, one-request fetch boundary, immutable local snapshots, and P2.1R semantic hardening. |
| P2.2 | Complete | P2.1R | Deterministic redirect, venue/year identity, HTML list-count, metadata, and proceedings-index verification. Sanitized EMNLP, NAACL/ACL, and IJCAI regressions; no PDF verification, state write, action, or live run. |
| P2.3 | Complete | P2.1R | PDF permission, URL/status, size, `%PDF-` signature, and deterministic sampling. No HTML identity logic, state write, redistribution grant, or live run. |
| P2.4 | Complete | P2.2, P2.3 | Single-writer SQLite repository, schema/migration, evidence history, lease, idempotent consumption, and replay. Temporary databases in tests; no deployed migration. |
| P2.5 | Complete | P2.4 | Verified evidence to state reducer, milestone scheduling, and typed action routing. Actions are returned as data and never executed. Replay all catalog venue/lifecycle shapes with fixtures. |
| P2.S | Complete | P2.5 | Opt-in DNS/SSRF-safe live adapter and explicitly authorized 15-venue shadow review using reviewed crawl policy and isolated state/artifact roots. The record contains 28 targets, rejects the known readiness false positives, returns no queue intent, and performs no job, scraper, notification, or production-state write. |
| P2.6 | Complete | Phase 2 gate | Fixture-only production-capable `DiscoveryEffect` with required budget/artifact ledgers plus durable per-venue cooldown and distinct-venue systemic circuit state. No live LLM call, installed caller, or production wiring. |
| P2.7 | Complete | P2.6 | Fixture-only production-capable `VerificationEffect` plus fully reviewed per-domain production crawl policy and durable fetch-failure guardrails. Only bounded read-only robots/terms research was live; no live verifier request or production wiring. |
| P2.8 | Complete | P2.7 | Fixture-only automatic discovery→verification→P2.5→P5.5 retention composition with exact replay and failure closure. Uninstalled; no live call, dispatch, or production state. |
| P2.8S | Review fix required | P2.8 | Separately authorized isolated live canary for the exact P2.8 composition. One real run made a live Gemini call, correctly refused to weaken verification, and retained no action because every citation was an unresolved grounding-redirect URL. P2.9 fixed the fixture shape, but P2.9S found a second real source-label shape. |
| P2.9 | Complete | P2.8S | Fixture-only exact COLT/2025 grounding-domain resolution plus bounded PMLR identity/count/link extraction and existing P2.3 PDF sampling. The grounding wrapper remains denied and is never fetched. |
| P2.9S | Review fix required | P2.9 | The second authorized canary resolved/fetched only official COLT evidence because the real response omitted the PMLR domain label. It retained no action; exact replay was free and the wrapper remained unfetched. |
| P2.10 | Ready | P2.9S | Fixture-only derivation of an exact PMLR volume candidate from a retained, identity-verified official COLT page link, followed by the unchanged P2.9/P2.3 checks. |
| P2.10S | Blocked | P2.10 | Separately authorized third fresh-root live proof against the same `colt`/2025 venue/year; no install or dispatch. |

Phase 2 has passed its shadow gate with the reviewed record in
`phase2-live-review-2026-07-13.md`. It remains `Shadow`, not `Implemented`,
because live observation has no production action authority and source-shape
coverage remains conservative. P2.6, P2.7, and P2.8 are accepted. P2.8S's one
authorized live run did not reach a genuine authoritative `pdf_status=ready`
facet (see `p2-8s-live-canary-review-2026-07-14.md`), so per its own
acceptance text the package is `Review fix required`, not `Complete`. P2.9 is
now `Complete`: fixture/fake evidence closes the specific deterministic
grounding-redirect source-shape gap without touching crawl policy or weakening
any existing verifier check. P2.9S's separately authorized review likewise
did not retain an action because the real response contained no PMLR domain
label; it is `Review fix required`. P2.10 is the sole `Ready` package and
P2.10S remains blocked pending its fixture-first result. P5.5S's action-source
prerequisite is still not satisfied.

## Phase 3 packages — cases and notifications

### P3.1 — persistent unresolved-case state and controls

Status: `Complete`

Depends on: Phase 2 gate

Completed boundary: `automation/cases.py` derives one stable case per
venue/year/blocker, distinguishes repeated checks from meaningful changes,
retains evidence, reactivates dormant state only for new evidence, and applies
resolve, snooze, ignore, and reactivate as pure state controls. Closed human
states require explicit reactivation.

Control-state schema version 2 atomically migrates a valid version-1 database
and persists lease-protected case current rows, immutable state revisions, and
immutable observation/control events. Exact event replay is a no-op,
conflicting reuse is rejected, a relational uniqueness constraint reinforces
domain deduplication, terminal cases are absent from the default list, and
stored corruption or compound-write failure fails closed. Tests use fixed
clocks and temporary databases.

The package does not consume P2.5 action intents, compute weekly, monthly, or
dormant policy, group a digest, create or deliver a notification, call email
or another transport, synchronize GCS, or change the deployed monitor. P3.2
and later retain all reminder, notification, integration, and live-delivery
work.

### P3.2 — clock-controlled reminder policy and grouped digest

Status: `Complete`

Depends on: P3.1

Completed boundary: `automation/reminders.py` is a pure projection over the
existing validated case and policy contracts. An injected aware clock and
`last_meaningful_change_at` select stable weekly, monthly, and dormant cadence
slots, age defensive case copies to `stalled`/`dormant`, release expired
snoozes, preserve dormant reactivation semantics, and exclude closed or
actively snoozed cases. Exact default boundaries are days 7/14/21/28,
30/60, and 84 followed by the configured dormant interval.

`build_case_digest` returns one immutable in-memory result containing every
currently due case once, grouped in weekly/monthly/dormant urgency order with
stable evidence and slot references. Equal input and clock values replay to
equal output. Fixed-clock tests cover window boundaries, meaningful-change
versus last-check aging, snooze expiry, closed/dormant behavior, invalid input,
stable grouping, input immutability, and effect-free module imports.

The package does not persist aged state or delivery attempts, consume P2.5
case/action intents, create immediate or digest notification intents, classify
delivery retries, redact/render messages, call email or another transport,
synchronize GCS, use Prefect, or change the deployed monitor. P3.3 now owns the
isolated fake-only delivery boundary; P3.4 now owns pending integration and
P3.S has completed the separate authorized live canary.

### P3.3 — idempotent notification delivery boundary

Status: `Complete`

Depends on: P3.2

Completed boundary: the strict version 1 notification-intent contract and
`automation/notifications.py` build stable immediate messages from explicitly
supplied events and stable grouped messages from explicitly supplied P3.2
digests. Messages retain evidence/run IDs, have explicit item/text bounds, and
redact common credential assignments, authorization/cookie/token forms,
credential-bearing URLs, and signed query values before validation or
persistence.

Control-state schema version 3 atomically migrates valid version-1/version-2
local databases and retains immutable notification intents, uniquely claimed
event/reminder-slot sources, and numbered attempt history under the singleton
lease. The coordinator commits an in-flight claim before calling the injected
transport. Delivered, permanent-failure, and unresolved in-flight replay is
suppressed; typed retryable failures allow an explicit next attempt; untyped
transport bugs propagate and leave the claim closed for inspection; raw error
text is never retained.

Tests use only fake transports, fixed clocks, and temporary SQLite databases.
The package does not consume P2.5 action intents or case events, query case
state, coordinate reminder slots, use Prefect, provide email/SMTP/HTTP/webhook
or cloud adapters, configure recipients, synchronize GCS, call an external
service, or change the deployed monitor. P3.4 owns integration and shadow
output; P3.S completed only the separate synthetic delivery/fatigue review.

### P3.4 — persistent shadow notification integration

Status: `Complete`

Depends on: P3.3

Completed boundary: `automation/notification_integration.py` consumes only
typed P2.5 transition and create/update-case actions. A transition action maps
to one immediate source. A case action derives one stable observation per
blocker, commits it through the P3.1 repository, and registers immediate output
only when the retained event is meaningful. Case persistence and notification
registration are separate lease-protected transactions, so a forced output
failure leaves the case durable and exact replay fills the missing output
without another case revision.

The reminder coordinator lists unresolved repository cases, applies P3.2's
clock-controlled projection, filters reminder-slot sources already claimed by
an immutable intent, and groups every remaining due case into one digest.
`ControlStateRepository.register_notification_intent` persists strict pending
schema-v3 intent/source records without creating an attempt; exact replay is a
no-op and conflicting source meaning fails closed. Reopen tests prove each
transition, case event, and reminder slot belongs to at most one notification.

Tests use fixed clocks and temporary SQLite databases. Every P3.4 output has
status `pending`, attempt count zero, and empty attempt history. The package
does not call the P3.3 fake protocol or any real email/SMTP/HTTP/webhook,
Prefect, cloud provider, recipient, credential, scheduler, deployed monitor,
GCS synchronization, action executor, scraper, Mac worker, Codex, promotion,
or deployment path. P3.S remains a separate manual boundary and does not grant
P3.4 delivery authority.

### P3.S — authorized notification delivery and fatigue canary

Status: `Complete`

Depends on: P3.4

Completed boundary: `automation/resend_notifications.py` implements one
bounded Resend HTTPS request with no redirect and no second request after any
consumed attempt, a strict response limit, typed secret-free failure mapping,
and the stable notification ID as provider idempotency.
`automation/run_notification_canary.py` refuses
without `--live`, an explicit isolated output root, and the normalized SHA-256
fingerprint of one approved test recipient. The command accepts no event,
case, notification, or state input and builds only a fixed digest of three
non-sensitive synthetic cases at the weekly, monthly, and dormant boundaries.

The authorized run made one external request and retained one delivered
attempt after provider acceptance. A fake-only rate-limit drill retained a
retryable category without creating case state, exact reopen used zero
transport calls, and removing recipient configuration refused before root
creation or I/O. Retained JSON contains recipient and receipt fingerprints,
not addresses, credentials, or raw provider responses. The fatigue review
found the three-item grouped message clear at 1,334 characters and 36 lines;
larger-volume fatigue and independent mailbox confirmation remain unproven.
The durable record is
[`phase3-delivery-review-2026-07-13.md`](./phase3-delivery-review-2026-07-13.md).

P3.S does not import or deliver retained P3.4 output, change case/reminder
semantics or schemas, configure a production recipient, migrate production
state, wire Prefect/Cloud Run/Scheduler, or begin a job, scraper, Mac worker,
Codex, promotion, deployment, or P4 package.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P3.1 | Complete | Phase 2 gate | Persistent unresolved-case domain and repository with deduplication plus resolve, snooze, ignore, and reactivate controls. No reminder or notification generation and no transport. |
| P3.2 | Complete | P3.1 | Clock-controlled weekly, monthly, and dormant reminder policy plus grouped digest generation. No persisted delivery state, notification intent, or transport adapter. |
| P3.3 | Complete | P3.2 | Strict immediate/digest intents, unique source claims, persistent idempotent attempts, bounded retry classification, redaction, and fake-only transport tests. No real transport or integration. |
| P3.4 | Complete | P3.3 | Typed transition/case actions plus repository reminders produce uniquely claimed pending immediate/grouped shadow intents with zero delivery attempts. Case and output commits remain independently replayable. |
| P3.S | Complete | P3.4 | One approved-recipient delivery of a fixed non-sensitive synthetic weekly/monthly/dormant digest, with provider acceptance, replay, fatigue, failure, and rollback evidence. No P3.4 output or production integration. |

Case creation and message delivery remain separate effects. A transport failure
must not erase or duplicate the durable case.

## Phase 4 packages — Mac mini execution plane

### P4.1 — immutable typed queue and cloud submission boundary

Status: `Complete`

Depends on: Phase 3 gate

Completed boundary: version 2 jobs derive a full SHA-256 `job_id` and
`job_fingerprint` from the request/action identity, venue/year, job type,
requester, input artifacts, and closed payload. Version 1 jobs remain valid
compatibility artifacts but cannot cross the P4.1 queue boundary. The strict
queue envelope maps existing scrape, validation, and Codex job types to
separate fixed queues in the inert `openpapers-mac` process work-pool
blueprint; pool/queue drift, forged identity, secrets, arbitrary commands, and
unknown fields fail before submission.

`automation/job_queue.py` converts only an explicitly supplied P2.5
existing-scraper action into a closed archival scrape job. Its asynchronous
cloud coordinator calls an injected submitter with the job ID as idempotency
key. The Prefect adapter accepts an injected client and deployment mapping,
confirms the deployment is assigned to the dedicated pool and fixed queue,
passes one strict envelope, and returns a bounded receipt. Fixture/fake tests
construct no live Prefect client and change no external state.

P4.1 does not persist an action/job, connect P2.5 or Phase 3 to production,
create a Prefect pool/queue/deployment/flow run, install/configure the Mac or a
worker, execute a command/scraper/validator/Codex process, manage locks, disk,
timeouts, cancellation, or offline delivery, publish/consume results, touch
GCS, or begin P4.2 and later behavior.

### P4.2 — fake-only Mac worker package and launchd runbook

Status: `Complete`

Depends on: P4.1

Completed boundary: `automation/mac_worker/` provides a pure receiving
function that revalidates a strict P4.1 envelope and returns only a stable
`simulated` fixture observation. Its one thin Prefect flow accepts exactly the
`queue_envelope` parameter, disables result persistence/retries, and has no
executor or arbitrary callable/command input. Scrape, validation, and Codex
typed fixture jobs exercise their fixed queues without running any process or
claiming a job result.

The package also provides bounded local health signals for macOS, Python 3.12,
repository/data-root access, Prefect 3.7+ plus an injected
local-configuration probe, and a Codex login marker checked only through
owner/permission/file metadata. Reports retain no paths, setting values,
credential contents, or raw exceptions. The concrete Prefect probe reads local
profile settings without an API call. A Mac-only requirements file reuses the
existing Prefect range, and a parseable credential-free plist plus runbook
document future per-user `launchd` installation, inspection, rollback, and
recovery.

Tests use sanitized jobs, fake Prefect probes, temporary paths, and local plist
parsing. P4.2 does not install dependencies, log in, copy/load a plist, call
`launchctl`, start a worker, create/read/mutate external Prefect or GCP
resources, connect scheduling, execute a scraper/validator/Codex process,
persist jobs, manage P4.3 locks/disk/timeouts/cancellation/dedup/offline state,
publish or consume P4.4 results, or perform P4.O operational drills. It changes
no Phase 3 case/notification semantics.

### P4.3 — local execution safety, duplicate delivery, and offline semantics

Status: `Complete`

Depends on: P4.2

Completed boundary: `automation/mac_worker/safety.py` revalidates one P4.1
envelope before touching local state and holds a process-safe non-blocking
venue/year lock across its disk check, durable claim, and injected fake-handle
supervision. Private journal records contain only the stable version, job
identity/type, venue, and year. A claim is written before the fake starter and
atomically promoted to a local completed marker only after a typed confirmed
success. Exact completed replay skips the starter; an existing active claim is
never expired automatically, blocks every job for that venue/year, and returns
recovery-required.

Both minimum free bytes and free fraction must pass before a claim/start. The
injected handle receives a bounded runtime and cancellation signal. Confirmed
failure, timeout, or cancellation clears the active claim and permits an
explicit retry under the same immutable job ID. An invalid outcome,
post-start exception, cancellation failure, or unconfirmed stop retains the
claim and blocks replay. Stable observations contain no configured path, raw
exception, command, result, or artifact claim.

The fixed offline policy leaves Prefect as the sole pull-queue owner: when no
delivery reaches the Mac, no local claim/buffer/expiry/resubmission exists and
the job ID is preserved. Tests use sanitized jobs, fake handles/cancellation,
injected disk usage, temporary private roots, and child processes. P4.3 does
not install or contact a worker/Prefect/GCP resource, select or execute a
command/scraper/validator/Codex process, write cloud control state, publish or
consume a P4.4 result/manifest, or perform the P4.O operational drills. The
local completion marker is not a job result and cannot authorize a transition.

### P4.4 — immutable result publication and cloud consumption

Status: `Complete`

Depends on: P4.3

Completed boundary: strict version-1 job manifests and version-2 job results
derive their identities from all semantic fields and bind back to one P4.1
version-2 job. The manifest admits only closed typed artifact summaries and
safe relative object names. The result records terminal status, bounded
metrics, and the exact manifest ID; cross-artifact validation rejects job,
fingerprint, time, status, secret-shaped field, and manifest drift. Version-1
job results remain compatibility artifacts but cannot cross this boundary.

`automation/job_results.py` fixes `manifests/<job-id>.json` and
`job-results/<job-id>.json`, publishes the manifest before the result commit
marker, and supplies `if_generation_match=0` on both injected GCS uploads.
Failed preconditions are accepted only after an exact-generation read proves
byte-identical canonical content; conflicts are never overwritten. A
manifest-only partial write is safe to retry. Reads bind each download to the
generation just observed, so object replacement cannot produce a torn pair.

Control-state schema version 4 adds an append-only job-result consumption
ledger under the existing cloud singleton lease. It retains the strict job,
manifest, result, fixed object names, and positive generations, revalidates
them on every read, returns a no-op for exact replay after restart, and rejects
changed content or generation for an already-consumed job. The thin
`automation/job_result_consumer.py` coordinator composes only the injected
reader and repository; it applies no lifecycle transition or action.

Tests use sanitized fixtures, a fake GCS bucket, and temporary SQLite files.
P4.4 does not construct a GCS client, read credentials, create a bucket or IAM
binding, install or connect the Mac worker, add a command/handler, interpret a
result into conference state, change the deployed monitor, or perform P4.O
drills. P4.3's fake completion remains separate and no live result has been
published or consumed.

### P4.O — headless installation and operational drills

Status: `Paused` (Prefect Cloud plan constraint and unjustified recurring cost,
2026-07-14)

Depends on: P4.4

Outcome: read-only inspection found the planned pool, queues, and deployments
absent. The first apply was rejected before create by the service plan's hybrid
work-pool restriction. No Prefect Phase 4 resource, GCS result resource, IAM
binding, OpenPapers daemon, fixture run, reboot, or result object was created.
The uncommitted P4.O provisioning/canary code was withdrawn. Host-specific
evidence remains only in the ignored local operations record.

Do not resume P4.O unless a future decision explicitly accepts either the
recurring service cost or the operational burden of self-hosting. The adopted
replacement is documented in
[`local-first-decision.md`](./local-first-decision.md).

### P4.L1 — local ownership and due-work scheduler foundation

Status: `Complete`

Depends on: P4.4 and the accepted local-first decision

Objective: establish the deterministic, credential-free scheduling core that
can eventually replace the Prefect transport without changing production.

Included:

- define the target local mutable-state ownership contract and reject an
  active or ambiguous second writer;
- represent one bounded scheduler wakeup and derive due conference-year work
  from persisted `next_check_at` values using an injected timezone-aware clock;
- acquire one local singleton lease before selecting or recording work;
- make exact wakeup replay and duplicate due selection idempotent;
- record bounded local run outcomes needed for restart/recovery reasoning; and
- use plain Python, temporary SQLite databases, fixtures, and fake effects in
  tests.

Excluded:

- live websites, Vertex AI, Resend, Prefect, GCS, or any other network call;
- opening or migrating the deployed monitor's state;
- installing/loading a plist, calling `launchctl`, rebooting, or changing the
  dedicated runtime account;
- running a scraper, validator, Codex, notification transport, promotion, or
  MustCite operation;
- mounting or writing the external data volume; and
- disabling Cloud Scheduler or changing Cloud Run, Prefect, GCP, or production
  credentials.

Acceptance:

- fake-clock tests prove not-due, due, missed-wakeup, exact-replay,
  duplicate-wakeup, lease-contention, restart, and stale/ambiguous ownership
  behavior;
- the local runner has no import-time orchestration dependency and accepts no
  arbitrary command or environment expansion;
- all state is temporary/test-local and the current deployment remains
  unchanged; and
- durable docs distinguish this foundation from installation, shadow use, and
  cutover.

Completed boundary: `local_control_plane` is now an explicit target
control-state role, and schema version 5 persists one immutable owner per
database. Valid legacy version 1-4 databases remain cloud-owned and refuse a
local open before migration; only a new empty database explicitly created for
the local role can be local-owned, and no transfer API exists.

`automation/local_scheduler.py` observes one injected aware clock, acquires the
existing local singleton lease, records one bounded wakeup, and selects
persisted conference state with `next_check_at <= now`. Due identity is the
venue/year/exact schedule timestamp, so completed exact replay and later
wakeups over unchanged state cannot produce duplicate work. Interrupted active
wakeups remain durable ambiguity and block automatic restart rather than aging
out. The runner returns inert typed selection data, accepts no effect callback,
command, or environment expansion, and has no orchestration or network import.

Tests use only the existing conference-state fixture, fake clocks, and
temporary SQLite. No domain action is composed, no deployment or production
database is opened, and no website, Vertex AI, Resend, Prefect, GCS, daemon,
external volume, scraper, validator, Codex, promotion, or MustCite operation is
called. P4.L2 owns local control-domain composition.

### P4.L2 — fixture-only local control composition

Status: `Complete`

Depends on: P4.L1

Completed boundary: control-state schema version 6 adds one bounded immutable
plan record per scheduler wakeup. The repository retains selected due work and
counts while the v5 wakeup remains active, and copies those counts into a
completed wakeup only after the caller's composed domain work succeeds. The
P4.L1 runner preserves its effect-free select-and-complete behavior.

`automation/local_control_plane.py` holds one local singleton lease across a
catalog-bounded injected discovery effect, a separately injected strict
verification effect, verification retention, P2.5 lifecycle reduction, P3.4
case and pending immediate-output integration, and one repository reminder
projection with optional pending grouped output. Every artifact is revalidated,
venue/year and observation time are exact, verification output is bounded, and
each selected schedule must advance or clear before completion. Exact completed
replay invokes neither fake effect. An exception after selection leaves the
wakeup durably active and blocks automatic continuation.

Tests use sanitized fixtures, fake effects, fake clocks, and temporary local
SQLite only. Pending notification records have zero attempts; recheck, review,
and scrape actions remain inert typed outcome data. P4.L2 adds no live provider,
network/client call, notification delivery, job construction/submission,
command/executor, scraper, validator, Codex, result interpretation, daemon,
host/external-volume operation, production migration, ownership transfer,
promotion, MustCite, or deployment behavior.

### P4.L3 — headless local service package

Status: `Complete`

Depends on: P4.L2

Completed boundary: `automation/local_service/` defines strict normalized
absolute configuration and derives control SQLite plus atomic bounded health
and run records below one private internal root. That root must be disjoint
from the configured external execution volume, and both it and its control
child must be private non-symlinked directories. Typed
macOS/Python/repository/internal-storage/volume health checks run before the
injected wakeup boundary; the default volume probe only observes whether the
configured path is an available mount and never mounts it. Missing/unsafe
storage, probe failure, or corrupt record history makes no effect call and
does not open control SQLite.

The pure renderer returns a fixed `org.openpapers.local-control` system
LaunchDaemon for an explicit role user. It wakes at load and on one
hourly calendar minute, exits after one invocation, uses a restrictive umask
and low-impact hints, and contains no shell, environment dictionary,
keepalive, socket, inbound listener, credential, or launchd-managed log file.
Application health is atomically replaced; run history retains at most the
configured hard-bounded count of fixed-shape records and excludes paths,
account names, raw exceptions, and provider text.

Rollback is inert structured data naming only the exact OpenPapers label and
`/Library/LaunchDaemons/org.openpapers.local-control.plist`; it preserves the
internal root, control state, records, repository, external data, and every
unrelated label. Tests use fake clocks, fake effects, fake volume probes, and
temporary private directories. The standalone command deliberately has no
concrete effect and returns `effect_unconfigured` without opening state.

P4.L3 does not create/access an account or real external volume, render to a
host path, copy/install/load/start/stop/remove a plist, call the service
manager, run a reboot/SSH/coexistence/recovery drill, connect a live discovery,
verification, notification, job, command, scraper, result, cloud, Codex,
promotion, MustCite, or production-state effect, or perform ownership transfer
or cutover. P4.LS separately completed isolated host installation and drills;
P4.LC subsequently completed production transfer.

### P4.LS — isolated host shadow and drills

Status: `Complete`

Depends on: P4.L3

Completed boundary: an exact private marker gates the new
`--isolated-shadow` service mode before SQLite opens. Its only concrete effect
calls P4.L1's bounded scheduler against isolated local-owned state. It cannot
call discovery, verification, notification delivery, jobs, commands, scrapers,
results, Prefect, GCS, Codex, the deployed monitor database, or production
state. The ordinary CLI remains `effect_unconfigured`.

One authorized Mac installation uses a root-owned read-only source snapshot,
an isolated minimal dependency environment, the existing dedicated non-login
role, private bounded internal state/records, and a private execution directory
backed by the shared external volume. The concrete mount probe checks the exact
directory and its non-root mounted ancestor rather than granting role access to
the whole volume.

Operational evidence passed exact duplicate wakeup, real SSH disconnect,
reboot resumption without manual kickstart, intentional missing-path closure
without unmounting shared storage, ambiguous-wakeup preservation plus
archive/new-root recovery, bounded run/health visibility, and exact
OpenPapers-only rollback/reinstall. Pre/post gates passed the private
co-resident health check and all five expected service labels. The installed
database contains no conference state, the cloud baseline remained
authoritative, and no production authority was transferred.

### P4.LC — single-writer cutover

Status: `Complete`

Depends on: P4.LS

Completed boundary: a strict production marker binds private allowlisted
configuration to an immutable monitor backup fingerprint and exact remote
state generation. Private secret storage supplies only the existing
OpenReview and TLS SMTP values; the plist and bounded records remain
credential-free. The production effect validates and preserves the legacy
three-venue/six-source monitor state separately from schema-v6 local control,
durably suppresses duplicate daily monitoring/notification, fails closed on
ambiguous work, and runs the local due scheduler on hourly wakeups.

The authorized cutover created two generation-stable private backups, paused
the exact Cloud Scheduler job with zero active Cloud Run executions before
every local activation, and retained the cloud job/state plus shadow
runtime/venv for rollback. Initial and final local runs each checked six
sources with zero errors, reported five ready local checks, local immutable
ownership and zero active wakeups, and passed all five co-resident service
checks.

The timed rollback stopped local before resuming cloud, waited for a successful
Cloud Run recovery that advanced remote state, and completed in 96 seconds.
Final cutover paused/drained cloud again, refreshed local from that recovered
generation, and activated only the Mac writer. No writer overlap occurred.
P4.LC adds no live discovery/verifier, case-delivery, scraper, validator, job,
result, Codex, promotion, or MustCite deployment effect; those remain later
packages.

The committed sanitized acceptance record is
[`phase4-local-cutover-review-2026-07-14.md`](./phase4-local-cutover-review-2026-07-14.md).
It consolidates P4.LS/P4.LC host, backup, cutover, rollback, validation, and
residual-risk evidence while keeping private operations data outside Git.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P4.1 | Complete | Phase 3 gate | Immutable v2 job identity, fixed Prefect process-pool/typed-queue protocol, and injected fake-tested cloud submission boundary. No external resource or Mac change. |
| P4.2 | Complete | P4.1 | Fake-only Mac receiving flow, bounded local health checks, isolated dependency, and credential-free `launchd` runbook/template. Nothing installed or executed. |
| P4.3 | Complete | P4.2 | Mac-local venue/year locks, disk gates, injected-handle timeout/cancellation, completed-delivery suppression, ambiguous-claim recovery closure, and fixed Prefect pull/offline semantics. No command or result path. |
| P4.4 | Complete | P4.3 | Strict immutable manifest/result contracts, create-only GCS-compatible publishing, exact-generation reads, and lease-protected exactly-once logical consumption. Fake/local only; no external resource or execution. |
| P4.O | Paused | P4.4 | Prefect feasibility gate failed before resource creation; the required paid/self-hosted transport is not justified. |
| P4.L1 | Complete | P4.4 + local-first decision | Plain-Python immutable local ownership and clock-injected bounded due-work scheduler foundation using only fixtures and temporary SQLite. No external or production effect. |
| P4.L2 | Complete | P4.L1 | Compose accepted discovery, verification, lifecycle, case, reminder, pending-shadow, and inert-action boundaries under one local lease with fake effects and temporary SQLite only. |
| P4.L3 | Complete | P4.L2 | Credential-free headless LaunchDaemon renderer, fixed private internal paths, bounded health/run records, missing-volume closure, and exact rollback scope. Fake/temporary only; no installation or concrete effect. |
| P4.LS | Complete | P4.L3 | Marker-gated scheduler-only Mac shadow installed against isolated local state; duplicate/SSH/reboot/missing-volume/ambiguous-recovery/rollback/coexistence drills passed. No production authority. |
| P4.LC | Complete | P4.LS | Generation-bound backups, capability-equivalent local monitor, authorized no-overlap writer cutover, health gates, and 96-second timed rollback. Cloud rollback retained paused. |

Code implementation, Mac installation, cloud configuration, and operational
drills are distinct tasks even when performed by the same maintainer.

## Phase 5 packages — execute existing scrapers

### P5.1 — approved repository command registry

Status: `Complete`

Depends on: Phase 4 gate

Completed boundary: `automation/command_registry.py` revalidates one complete
version-2 immutable job and resolves only `scrape_existing` and
`validate_candidate` to the fixed `main.py` and
`postprocessing/validate_year.py` repository entry points. It returns an
immutable job-bound data specification with literal arguments derived only
from closed venue/year/enum/boolean/integer fields and an explicit
`isolated_staging_required` policy.

The registry accepts no command string, interpreter, repository/data/metadata
path, generic argv, caller flag, or environment mapping. Contract-valid Codex
jobs are explicitly outside Phase 5; their `allowed_paths` never become
execution authority. Recomputed-identity regression tests reject unknown
shell/command/path/flags/environment/argv fields, path-like or
expansion-shaped venue/level values, legacy jobs, and forged identities.
Static scope tests prove the module has no subprocess, shell, filesystem,
environment, scraper, validator, orchestration, or cloud dependency.

P5.1 does not import or invoke either repository entry point, bind a Python
executable or staging root, create a process/claim/checkpoint/manifest/result,
read or write canonical data, or connect to the local scheduler/LaunchDaemon.
It changes no service, cloud resource, credential, schema, persisted state, or
deployment. P5.2 owns isolated staging execution and supervision; P5.3 owns
independent validation and manifest generation.

### P5.2 — isolated staging execution and supervision

Status: `Complete`

Depends on: P5.1

Completed boundary: `automation/staging_executor.py` accepts only one strict
version-2 `scrape_existing` job and revalidates P5.1's fixed `main.py`
specification. Explicit normalized absolute configuration binds a trusted
repository/entry point and executable whose metadata is not group/other
writable to a private per-job root derived from the full immutable job
fingerprint. The staging root must be disjoint from both the repository and a
separately declared canonical data root. The exact child environment inherits
nothing, disables dotenv, and sets
only unbuffered Python plus staging-bound scraper data/log paths.

A strict atomic current checkpoint is created before execution and records a
closed process-only state and monotonic attempt. Confirmed nonzero exits,
timeouts, cancellations, and pre-start cancellation may resume through the
same data root; the P5.1 invocation retains the core scraper's default resume
behavior. Confirmed process success suppresses exact replay. A start or
supervision fault, corrupt/foreign state, or unconfirmed process-group stop
becomes durable ambiguity and never auto-expires. Checkpoint transitions and
compare-before-replace updates reject skipped-state and concurrent drift.

The module includes a no-shell standard-library subprocess adapter with a
private log and bounded TERM/KILL supervision, but it has no CLI or caller and
is not connected to P4.3, the scheduler, the installed LaunchDaemon, or
production state. Tests use only a temporary fake repository/executable,
separate temporary staging/canonical roots, fake clocks, and fake process
launchers/handles. No scraper, validator, network request, canonical-data
operation, manifest/result, service, or cloud resource is used or changed.
P5.3 now implements independent validation and manifest generation; P5.4 owns
runtime composition and failure classification.

### P5.3 — independent staged validation and manifests

Status: `Complete`

Depends on: P5.2

Completed boundary: `automation/staging_validation.py` accepts only a strict
version-2 existing-scraper job with its exact P5.2 `process_succeeded`
checkpoint. It inventories the private data tree without following symlinks
or accepting special, foreign-owned, group/world-writable, unbounded, or
changing files, and retains a deterministic candidate inventory plus strict
P4.4-compatible scrape-job manifest below a separate private artifact root.
Staging, artifact, and declared canonical roots must be normalized and
pairwise disjoint.

A separately supplied strict validation job must bind the candidate manifest,
venue/year, completeness level, expected count, and effective PDF policy. The
existing independent core validator checks count, required metadata,
duplicate IDs, and provisional archival records; P5.3 performs containment-
safe, inventory-bound PDF existence, minimum-size, and `%PDF-` checks. The
strict `validation-report` v1 contract records only closed policy, counts,
issues, identities, and fingerprints. The report and its validation-job
manifest are create-once and exact-replayable; unsafe paths, candidate drift,
corruption, or identity/policy downgrade fail before an authoritative report.

P5.3 has no CLI or runtime caller and does not start a scraper/validator
process, publish a P4.4 result, route readiness/failures, write canonical data,
generate statistics, make a network/cloud request, or change the installed
service. Tests use only temporary fixture staging/artifact/canonical roots.
P5.4 owns runtime composition, result construction, and failure/readiness
routing; P5.S owns any authorized real shadow execution.

### P5.4 — guarded execution composition and readiness routing

Status: `Complete`

Depends on: P5.3

Completed boundary: `automation/execution_pipeline.py` accepts only a strict
version-2 existing-scraper job and coherent, pairwise-disjoint P4.3/P5.2/P5.3
configuration. It holds the existing process-safe venue/year lock across the
existing two-threshold disk gate, Mac-local exact claim, injected P5.2 process
boundary, P5.3 candidate capture/validation, injected P4.4 create-only
publication, and completion promotion. It derives the validation job only
from the immutable scrape job and captured candidate manifest.

The closed route distinguishes ready, partial, failed, retry, cancelled,
recovery-required, and completed replay outcomes, with transient,
operational, or structural failure classes that never depend on exception
text. Confirmed stopped process failures remain same-root resumable without a
write-once result; ambiguity retains the blocking claim. Valid candidates and
terminal structural validation outcomes publish strict results. A
manifest-only publication failure clears the claim only after confirmed stop
and replays byte-identical retained timestamps/artifacts on retry. Partial or
invalid output remains staged and cannot touch canonical data.

P5.4 has no CLI, scheduler/LaunchDaemon caller, concrete cloud client,
credential, canonical writer, statistics generator, promotion path, or real
process authorization. Tests use a fake launcher, fake disk state, temporary
private roots, and a fake immutable publisher; no scraper, validator process,
network request, cloud resource, service, or canonical data is used or
changed. P5.S owns the first authorized real shadow/canary execution and its
host recovery evidence.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P5.1 | Complete | Phase 4 gate | Pure approved registry maps strict scrape/validation jobs to fixed repository entry points and literal arguments, rejecting Codex, shell, paths, caller flags, and environment expansion. No process or runtime connection. |
| P5.2 | Complete | P5.1 | Existing-scraper staging executor with private canonical-disjoint roots, strict checkpoints, same-root resume, process-success replay suppression, timeout/cancellation, and ambiguous-stop closure. Fake/temporary only; no actual run or runtime connection. |
| P5.3 | Complete | P5.2 | Strict candidate inventory/manifest plus bound independent validation report/manifest for count, metadata, duplicate IDs, PDF existence/size/signature, and completeness levels. Temporary fixture roots only; no runtime, result, or canonical write. |
| P5.4 | Complete | P5.3 | Fixture-only guarded job-to-staging-to-validation-to-immutable-result composition with readiness routing, replay recovery, and transient/operational/structural classification. No runtime connection or real scrape. |
| P5.S | Complete | P5.4 | Manual sandboxed COLT 2025 archival shadow: confirmed failure and timeout recovery, 181/181 independent validation, private create-only result, exact duplicate suppression, canonical invariance, coexistence, and scoped rollback. No installed or automatic caller. |
| P5.5 | Complete | P5.S | Persists only strict P2.5 `queue_existing_scraper` actions and their recomputed v2 jobs in local-owned control state (schema version 7), then claims and reconciles at most one job through an injected P5.4 effect after releasing the global control lease. Schema migration, exact replay, crash ambiguity, result reconciliation, and rollback use temporary state/fakes only. No installed caller, live request, scraper process, canonical write, promotion, or Phase 6 capability. |
| P5.5S | Blocked | P5.5, P2.8, P2.10S | Separately authorized installed automatic shadow using the accepted automatic verified-action composition and successful live canary evidence, on a bounded venue family not covered by COLT. It may write only isolated staging/artifact/result space and cannot promote, deploy, run statistics writes, or enter Codex repair. |

P5.S completed boundary: `automation/execution_shadow.py` and the explicit
`automation.run_execution_shadow --live` command bind P5.4 to a private marked
root, a fixed macOS child sandbox denying repository/canonical writes, and a
local create-only result store. The authorized COLT 2025 job retained a
confirmed stopped failure, a deliberate confirmed timeout with four staged
entries and no artifacts/results, same-root attempt-3 success with 181 papers
and 181 valid PDFs, independent zero-issue validation, and an exact
`duplicate_completed` replay that changed no retained tree or process log.
Live cloud/local/co-resident gates and the canonical fingerprint remained
unchanged. The record is
[`phase5-existing-scraper-shadow-review-2026-07-14.md`](./phase5-existing-scraper-shadow-review-2026-07-14.md).

This does not connect P2.5 actions, the local scheduler, or the installed
LaunchDaemon to P5.4. Phase 5 is `Shadow`; automatic dispatch and promotion
remain unimplemented, and Phase 6 remains planned rather than inherited by
this canary.

### P5.5 — durable local action and execution dispatch

Status: `Complete`

Depends on: P5.S

Completed boundary: control-state schema version 7 adds an additive
local-owner journal that retains only an exact P2.5 `queue_existing_scraper`
action together with the strict version-2 job recomputed by
`automation.job_queue.build_scrape_job_from_action` in one immutable current
row plus an append-only numbered attempt history.
`automation/execution_retention.py` is called from inside
`automation/local_control_plane.py`'s existing lease-protected reduction,
immediately after P2.5 reduction and P3.4 integration, so a caller cannot
submit arbitrary job JSON or turn discovery output directly into execution
authority. Exact action/job replay is a no-op; identity, evidence,
venue/year, payload, or stored-content drift fails closed, and it has no
import on `automation.execution_pipeline`, `automation.mac_worker`, or
`automation.staging_executor`.

`automation/execution_dispatch.py` is the separate bounded dispatch step. It
claims at most one retained job under the local control lease, releases that
global lease before any potentially long-running work, and calls only an
injected P5.4-compatible effect. The persistent dispatch claim and its typed
observation are then reconciled under a newly acquired lease. A `retry`- or
`cancelled`-permitted observation returns the job to `pending` with an
incremented attempt number under the same immutable job ID; a terminal
observation (`ready`, `partial`, `failed`, or duplicate-completed `skipped`)
closes the job permanently. An effect exception, a `recovery_required`
observation, a job-identity mismatch, or a failure while reconciling leaves
the attempt durably `in_flight`; elapsed time alone never reclaims it.
Terminal result identity is retained exactly once, but P5.5 does not
interpret the result as a lifecycle transition or promotion authority.

Tests use only temporary local-owned SQLite, a real single-shot `pdf_ready`
verification fixture that exercises genuine P2.5 reduction, fake clocks, and
injected fake execution effects. `test_local_control_plane.py` proves a real
verified action retains exactly one job and exact wakeup replay adds nothing
new. P5.5 does not change the installed LaunchDaemon, production database,
production marker/configuration, network policy, credentials, P5.S command,
canonical data, statistics, deployment, or Codex boundary. Because the
installed P4.LC production service runs a read-only source snapshot fixed at
install time, this change has no effect on the live production database
until a separately authorized reinstall/upgrade. Its living plan is
`.agent/plans/p5-5-durable-local-shadow-dispatch.md`.

### P5.5S — installed automatic execution shadow

Status: `Blocked`

Depends on: P5.5 plus P2.8 (the accepted automatic deterministic
verifier/action-source composition) and P2.8S (its authorized live evidence)

P5.5S is not ready merely because dispatch code exists. Phase 2 remains
`Shadow`, the installed production service does not run its deterministic
HTML/PDF verifier, and no production reduction currently persists a verified
scraper action. The host canary therefore waits until P2.8 proves the automatic
composition with fixtures and P2.8S proves that exact composition against real
retained, crawl-policy-allowed, venue/year-bound evidence. Neither a manual
synthetic action nor the live canary alone satisfies both prerequisites.

P2.8 is `Complete`, but P2.8S remains `Review fix required`: its first
authorized run (`p2-8s-live-canary-review-2026-07-14.md`) retained no action
because every citation was redirect-wrapped. P2.9 (`Complete`) fixed that
exact fixture shape. P2.9S's second authorized run
(`p2-9s-live-canary-review-2026-07-14.md`) also retained no action because the
new real response omitted the PMLR domain label: only the official COLT page
resolved, so no PDF target existed. P2.10 (`Ready`) and P2.10S (`Blocked`)
define the fixture-first official-page-link path and its separately authorized
live proof. P5.5S therefore remains `Blocked` until that chain genuinely
retains `queue_existing_scraper`, not merely until a package status flips.

When that prerequisite exists, P5.5S must receive separate authority for the
installed-service change and live requests. It starts with the same
single-writer, disk, identity, path-isolation, canonical-denial, coexistence,
and recovery gates used by P5.S; uses a supported, bounded venue family not
covered by the COLT canary; and records duplicate delivery, partial output,
timeout/cancellation, ambiguous-stop recovery, immutable result replay, and
scoped rollback. It cannot promote data, run `statistics --write`, deploy
MustCite, enable Cloud Scheduler, expose credentials, or enter Phase 6.

A structural failure in one venue opens a separate venue-specific bug thread.
The rollout thread resumes after that fix has its own tests and commit.

## Phase 6 packages — Codex diagnosis and repair

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P6.1 | Planned | Phase 5 gate | Stable failure fingerprints, transient/operational/structural classifier, cooldowns, budgets, concurrency, and systemic-incident circuit breaker. No Codex call. |
| P6.2 | Planned | P6.1 | Strict Codex task/result contracts, task-scoped file access, prompt redaction, timeout, and non-recursion rules. |
| P6.3 | Planned | P6.2 | Local `codex exec` adapter and isolated branch/worktree lifecycle. It cannot access the primary checkout, merge, deploy, or broaden scope. |
| P6.4 | Planned | P6.3 | Fixture-driven repair, patch/test/review-report artifacts, cleanup, cancellation, and adversarial secret/access tests. |
| P6.S | Planned | P6.4 | Explicitly authorized local structural-failure canary that stops at a reviewable branch/patch. No auto-merge or deployment. |

Use a fork only if comparing genuinely competing Codex adapter or isolation
designs. The selected design returns to one implementation thread.

## Phase 7 packages — promotion and MustCite deployment

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P7.1 | Planned | Phase 5 validation gate | Release-candidate and promotion contracts with provenance, rights, scraper version, job, validation report, and approval actor. Fetch permission does not grant redistribution. |
| P7.2 | Planned | P7.1 | Atomic canonical dataset versioning, promotion transaction, audit record, and rollback. No deployment. |
| P7.3 | Planned | P7.2 | MustCite deployment adapter and post-deploy health checks against an approved promoted version. |
| P7.4 | Planned | P7.3 | Manual approval workflow plus failed-promotion, failed-deployment, health-check, and rollback drills. Initial deployment remains manually approved. |

Promotion and deployment must remain separate tasks and separate recorded
actions. Validation success alone does not authorize either one.

## Phase 8 packages — venue rollout and hardening

Each venue family begins in shadow mode. A family may require multiple runs in
one thread, but a source-specific parser change is a separate task.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P8.1 | Planned | Phase 7 gate | ICML, AISTATS, and IJCAI rollout compared with the existing monitor. |
| P8.2 | Planned | P8.1 | ICLR, NeurIPS, and AAAI rollout across OpenReview and official proceedings. |
| P8.3 | Planned | P8.2 | ACL, EMNLP, and NAACL rollout with explicit venue-identity protection for ACL Anthology collaboration. |
| P8.4 | Planned | P8.3 | CVPR, ICCV, and ECCV rollout, including odd-year/event-identity handling. |
| P8.5 | Planned | P8.4 | COLT, UAI, and JMLR rollout across PMLR and continuous publication. |
| P8.6 | Planned | P8.5 | Cross-family cost, provider budget, notification volume, cooldown, circuit-breaker, and operational SLO review. |
| P8.7 | Planned | P8.6 | Failure/rollback drills, residual-risk review, operator runbook reconciliation, and final roadmap status update. |

Every family requires reviewed discovery/evidence accuracy, approved crawl
policy, accurate scraper capability, no executable false positive during
shadow, and a documented rollback before automatic action is considered.

## Phase 9 packages — external status export

The consumer of this package is a separate, independently maintained
application (for example a browser or tablet dashboard) that this repository
does not build, host, or select technology for. This package only makes
already-owned control-plane state readable by that consumer.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P9.1 | Planned | Phase 2.4 + Phase 3 production wiring; job-result fields also depend on P4.4 | Versioned, schema-validated `dashboard-status` export admitting only public-safe lifecycle, case-urgency, and job-summary fields (no evidence/crawl URLs, raw discovery/verification payloads, case free-text, credentials, or internal paths). The existing control-plane writer emits/overwrites it to a dedicated, separately-permissioned GCS location as the last step of an already-authorized commit. No new writer role, lease, query API, authentication, or push mechanism. Does not build, host, or select technology for the consumer dashboard application. |

## Minimal prompt for a new thread

After selecting a `Ready` package, the normal prompt is:

```text
Execute <PACKAGE_ID> from docs/automation-system/work-packages.md. Follow local
AGENTS.md and .agent/PLANS.md. Stay within that package, run all required
checks, update durable docs, and commit the completed work.
```

Add one sentence only when granting an effect not already authorized by the
package, such as a live network canary or an external deployment operation.
Do not paste architecture, acceptance criteria, credentials, or earlier chat
history into the prompt; the repository documents are the handoff.

## Maintenance rules

- Update this file when a package is split, merged, reordered, blocked, or
  accepted.
- Update roadmap status only when phase-level reality changes.
- Record stable design decisions in architecture, not in a package row.
- Keep failed approaches and detailed progress in the local task ExecPlan.
- Do not mark a dependent package ready while a required review finding is
  unresolved.
- After a package commit, record its commit ID only while it materially helps
  the next handoff; avoid turning this page into a permanent commit log.
- Ensure exactly one current package is obvious to a zero-context agent.
