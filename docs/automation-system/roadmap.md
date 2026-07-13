# Automation roadmap

This roadmap describes planned work, not a claim that the target system is
already implemented. Update the status table and the relevant acceptance
criteria in the same change that completes a phase.

Thread-sized task boundaries, dependencies, and the current next package live
in [`work-packages.md`](./work-packages.md). Keep this roadmap focused on
phase-level outcomes and status.

## Status

| Phase | Scope | Status |
|---|---|---|
| Existing baseline | Deterministic monitor, Cloud Run/Prefect/GCS, email | Implemented |
| 0 | Contracts, policies, ownership, and safety boundaries | Implemented |
| 1 | LLM search discovery in shadow mode | Shadow (15-venue live review, 2026-07-13) |
| 2 | Evidence verification and lifecycle state | Shadow (P2.S 15-venue live review, 2026-07-13) |
| 3 | Cases and fatigue-resistant notifications | Planned |
| 4 | Mac mini Prefect worker and immutable results | Planned |
| 5 | Automatic execution of existing scrapers | Planned |
| 6 | Budgeted Codex diagnosis and repair proposals | Planned |
| 7 | Dataset promotion and MustCite deployment | Planned |
| 8 | Venue rollout and operational hardening | Planned |

Valid phase statuses are `Planned`, `In progress`, `Shadow`, `Implemented`,
and `Paused`, with a short reason/date when paused.

## Phase 0: contracts and policy foundation

Deliverables:

- versioned schemas for discovery, conference/case state, typed jobs, immutable
  job results, and Codex results;
- venue catalog containing stable identity, aliases, official domains,
  lifecycle kind, and existing scraper capability;
- evidence-backed conference-year milestones and policy-derived
  `next_check_at` scheduling;
- transition table and blocker codes;
- single-writer SQLite/GCS ownership and job-result protocol;
- configurable reminder-decay, provider budget, Codex budget, and systemic
  failure policies;
- crawl/publication policy model;
- architecture tests for schema validation and invalid transitions.

Acceptance:

- planned and implemented behavior are clearly distinguished;
- schemas reject unknown/missing execution-critical fields;
- no LLM result can directly create a scrape command;
- duplicate evidence/job results are idempotent;
- storage ownership and secret boundaries are documented and tested where
  executable;
- the existing production monitor remains operational.

Implemented in the Phase 0 foundation:

- `automation/schemas/v1/` and `automation/contracts.py` provide strict,
  versioned artifact validation and reject missing/unknown executable fields;
- `automation/config/venue_catalog.v1.json` covers all 15 current core
  scrapers without changing the deployed monitor registry or hardcoding
  year-specific check months;
- `automation/config/policies.v1.json` defines conservative reminder, provider,
  Codex, systemic-failure, dynamic scheduling, crawl, and publication defaults;
- `automation/scheduling.py` derives `next_check_at` from verified milestones,
  post-event backoff, low-frequency unknown-date fallback, and a maximum
  silence guard;
- `automation/domain.py` provides the transition/action/blocker/permission
  vocabulary, deterministic actor gate, evidence replay, write-once result,
  single-writer ownership, and secret-boundary checks; and
- sanitized fixture tests prove invalid transition and schema rejection,
  discovery/action separation, idempotent evidence/result replay, Mac/cloud
  ownership, JMLR continuous-publication behavior, and current monitor
  compatibility.

Phase 0 does not provide durable control state, a discovery provider, evidence
verification, routing, execution, or deployment. Those remain assigned to the
later phases below.

## Phase 1: LLM discovery

Deliverables:

- provider-neutral discovery interface;
- initial Gemini search-grounded implementation;
- structured prompt and output-schema versions;
- caching, discovery of candidate milestone dates, daily/per-venue budgets,
  and second-provider escalation interface;
- retained discovery/evidence artifacts;
- fixture-based tests plus a live, opt-in canary command.

Run in shadow mode across all registered venues. Discovery may report what it
would do but cannot queue a scrape.

Acceptance:

- every actionable claim has a source URL;
- venue/year mismatches and unsupported claims are rejected;
- daily and per-venue call limits work under retries;
- repeated unchanged checks use cache where appropriate;
- a manually reviewed sample across all venue families records agreement,
  false positives, missed sources, and ambiguous cases;
- the deterministic baseline continues in parallel.

Implemented so far:

- `automation/discovery.py` defines the provider-neutral interface and
  validates that every claim and candidate milestone matches the exact
  venue/year and cites a URL returned in provider grounding metadata;
- the version 1 discovery schema has a backwards-compatible typed
  `candidate_milestones` extension; candidates remain unverified evidence;
- immutable evidence artifacts, request/evidence fingerprints, an expiring
  cache, attempt-before-I/O daily/per-venue budgets, bounded concurrency and
  retries, safe error fingerprints, and a second-provider policy/interface are
  implemented with deterministic clocks and fakes;
- `automation/providers/gemini.py` uses Vertex AI Gemini Search Grounding plus
  a no-tool schema structuring pass over grounded excerpts, and
  `automation/run_discovery.py`
  refuses remote access unless `--live` is explicit. The manual development
  CLI is unmetered and accepts any catalog venue, while the reusable service
  retains tested ledger enforcement for future automatic callers; and
- fixture tests cover unsupported citations, source-class claims,
  venue/year/date mismatch, cache replay, retry accounting, second-provider
  escalation, and the command's non-live boundary.

Contract-valid live observations and manual review now cover all 15 catalog
venues and every rollout family. The review confirmed useful date and
paper-list discovery, while recording readiness false positives and NAACL/ACL
identity contamination. Prompt v14 uses grounded excerpt-to-source mappings,
deterministic catalog source classification, typed facet claims, conservative
status downgrades, cross-year annual milestones, continuous-publication
handling, and deterministic ended-date derivation. The review matrix is in
`phase1-live-review-2026-07-13.md`. Phase 1 is now `Shadow`, not `Implemented`:
repeat observations and operational integration remain future work, and Phase
2.2/2.3 must fetch cited resources and verify list, metadata, PDF,
proceedings, and venue identity before any state transition.

## Phase 2: verification and state transitions

Deliverables:

- URL/source trust classification;
- page, list-count, metadata, and PDF validators;
- crawl-policy enforcement before network actions;
- idempotent state reducer with evidence history;
- typed action router;
- source snapshots suitable for later parser repair.

Acceptance:

- unverified or conflicting claims cannot queue execution;
- PDF-ready claims include successful fetch/signature sampling where allowed;
- state history explains why every transition occurred;
- a new domain defaults to review rather than unrestricted crawling;
- JMLR follows a continuous-publication policy;
- replaying artifacts produces the same state.

Accepted P2.1 verifier-foundation implementation:

- version 2 verification request/result contracts bind selected targets and
  exact derived kinds to one discovery/evidence identity and reject unknown or
  executable fields; semantic compatibility validation preserves consistent
  version 1 fixture replay;
- catalog source trust is classified independently from crawl permission;
- the crawl-policy gate returns review/deny/missing-permission decisions before
  an injected fetch can run, enforces a per-run domain request budget, and
  carries the approved crawl constraints into a one-request,
  no-auto-redirect fetch interface;
- a local `SnapshotStore` implementation retains fake response bytes,
  allowlisted metadata, and sanitized redirect edges immutably by
  content/evidence fingerprint; and
- fixture tests prove exact/subdomain trust, suffix-confusion rejection,
  closed policy behavior, fake fetch call ordering, byte bounds, secret-safe
  snapshots, replay, cloud ownership of verification results, target/kind
  binding, retained evidence references, overall-status consistency, policy
  provenance, redirect retention, and signed-query rejection.

Accepted P2.2 HTML evidence implementation:

- every redirect target is independently classified and crawl-policy-gated
  before an injected fake fetcher may request it; loops, hop limits, and closed
  targets retain prior immutable evidence and fail closed;
- a bounded standard-library parser with explicit source profiles verifies
  token-bounded venue/year identity, exact candidate dates, plausible distinct
  paper counts, title/author/abstract completeness, and current proceedings
  index entries;
- strict v2 results associate evidence only with exact cited target URLs,
  preserve replay-stable observations, reject PDF targets, and report
  authoritative source disagreement as conflicting; and
- sanitized fixtures reproduce the EMNLP future publication promise,
  NAACL/ACL identity contamination, and IJCAI list-without-PDF cases without a
  live request.

Accepted P2.3 PDF evidence implementation:

- exact PDF claim citations are sampled deterministically and within a hard
  bound, independent of provider URL ordering;
- every initial or redirected URL is separately catalog-classified and must
  have separate `pdf_fetch_for_processing` and `store_internal_copy`
  permissions before the injected fake fetcher is called and evidence is
  retained;
- final responses require HTTP 200, a configurable minimum actual size aligned
  with canonical validation, consistent Content-Length when present, and a
  `%PDF-` signature; and
- strict v2 findings distinguish ready, partial, invalid, incomplete, unsafe,
  and untrusted samples while retaining replay-stable evidence and never
  granting redistribution permission.

Accepted P2.4 persistent control-state implementation:

- a versioned standard-library SQLite repository is restricted to the cloud
  control-plane owner and rejects future, malformed, or populated unversioned
  databases rather than touching the deployed monitor state;
- an expiring singleton lease prevents overlapping control writers and is
  revalidated in the same transaction as every history or state mutation;
- strict discovery/request/result bundles are retained atomically, use their
  validated semantic identity for idempotent consumption, and are
  fingerprint- and semantics-checked again during ordered replay; and
- conference-state current rows use optimistic revisions with an immutable
  snapshot history, atomic rollback, identical-write no-ops, and stale-write
  rejection.

Accepted P2.5 lifecycle reduction and typed-routing implementation:

- positive facets and milestones are promoted only from retained fetched
  official/archival evidence whose catalog trust is independently recomputed;
- monotonic facets, observed/verified milestones, the Phase 0 reducer, and
  evidence-time scheduling produce a strict state whose transitions trace to
  immutable verification evidence;
- stable typed recheck, transition, case/review, and existing-scraper intents
  are returned as data and never persisted, submitted, or executed;
- ambiguous, conflicting, crawl-denied, untrusted, continuous-conference, and
  unsupported-scraper conditions suppress executable intents; and
- a thin P2.4 coordinator persists optimistic revisions, while temporary
  fixture repositories prove deterministic replay for all 15 catalog venues,
  annual/continuous shapes, and compatible v1 artifacts.

P2.S is accepted at the shadow boundary:

- a standard-library live adapter rejects non-global/mixed DNS answers, pins
  the connection to the reviewed public IP while verifying the original TLS
  hostname, performs one bounded no-auto-redirect GET, and applies conservative
  delay/status/CAPTCHA stops;
- a separate reviewed shadow crawl policy grants no redistribution permission,
  and the opt-in command requires `--live` plus explicit isolated roots;
- the 15-venue sample retained 28 strict targets and isolated local state. It
  verified two exact future milestones, rejected 22 targets, left four for
  review, rejected eight exact PDF citations for invalid signatures, returned
  no queue intent, and performed no job, scraper, notification, or production
  state write; and
- the replayable review is recorded in
  `phase2-live-review-2026-07-13.md`. Conservative live source-shape gaps remain
  for later venue-family rollout.

Phase 2 is `Shadow`, not `Implemented`. P2.1 through P2.5 and the P2.S manual
runtime are not deployed or scheduled, and there is no action dispatcher.

## Phase 3: cases and notifications

Deliverables:

- persistent unresolved cases with deduplication;
- immediate transition/failure notifications;
- weekly, monthly, and dormant digest generation;
- snooze/ignore/reactivate/resolve controls;
- notification delivery retries separated from case creation.

Acceptance:

- one event creates at most one immediate notification;
- weeks 1-4, weeks 5-12, and dormant policies are covered by clock-controlled
  tests;
- `last_meaningful_change_at` drives aging;
- resolved cases stop appearing;
- one digest contains all due cases, grouped by urgency;
- email includes evidence and run references without leaking credentials.

## Phase 4: Mac mini execution plane

Deliverables:

- dedicated Prefect work pool and typed queues;
- macOS installation and `launchd` runbook;
- worker health check and Codex login check;
- venue/year locks, disk checks, timeouts, cancellation, and idempotent job IDs;
- immutable GCS job-result/manifest publishing;
- cloud result-consumer flow.

Acceptance:

- worker resumes after Mac reboot and SSH disconnect;
- the Mac requires no public inbound command endpoint;
- duplicate delivery does not repeat a completed scrape;
- the Mac cannot update cloud-owned SQLite state;
- logs appear in Prefect and artifacts contain a stable result manifest;
- offline workers leave work queued and visible.

## Phase 5: execute existing scrapers

Deliverables:

- approved command templates rather than arbitrary shell input;
- announced, metadata, and archival readiness routing;
- staging data directory and promotion candidate manifest;
- independent validation and repository completion checks;
- retry classification that separates transient, operational, and structural
  failures.

Acceptance:

- only verified, crawl-policy-allowed sources can queue a scrape;
- paper counts, required metadata, duplicate IDs, PDFs, minimum size, and PDF
  signatures are checked as applicable;
- invalid or partial output cannot overwrite canonical data;
- statistics and generated README coverage are updated only in a promotion
  candidate change;
- success, partial success, and failure are distinguishable and resumable.

## Phase 6: Codex diagnosis and repair

Deliverables:

- error classifier and stable failure fingerprints;
- per-venue/global budgets, cooldowns, concurrency, runtime limits, and a
  systemic-incident circuit breaker;
- local `codex exec --json` adapter with schema-constrained output;
- isolated branch/worktree lifecycle;
- patch, fixture, test, and review report artifacts.

Acceptance:

- transient HTTP, credentials, disk, or provider outages do not trigger Codex;
- the same failure fingerprint respects its cooldown;
- three or more likely-related venue failures open one systemic incident;
- Codex cannot access the primary checkout or unrelated secrets;
- Codex does not recursively trigger itself;
- code changes stop at a reviewable patch/branch and never auto-merge/deploy.

## Phase 7: promotion and MustCite deployment

Deliverables:

- explicit `validated -> release candidate -> promoted -> deployed` workflow;
- provenance and rights metadata in release manifests;
- rollback-capable canonical data versioning;
- MustCite deployment adapter and post-deploy health check;
- separate policy for metadata publication and PDF redistribution.

Acceptance:

- public availability of a PDF is not treated as redistribution permission;
- every promoted dataset can be traced to evidence, scraper version, job,
  validation report, and approval actor;
- deployment failure does not corrupt the validated dataset;
- rollback and health-check procedures have been exercised;
- initial promotion/deployment remains manually approved.

## Phase 8: rollout

Roll out in venue families, each beginning in shadow mode:

1. ICML, AISTATS, IJCAI: compare with the existing monitor;
2. ICLR, NeurIPS, AAAI: OpenReview and official proceedings;
3. ACL, EMNLP, NAACL: ACL Anthology;
4. CVPR, ICCV, ECCV: visual-conference sources;
5. COLT, UAI, JMLR: PMLR and continuous publication.

Acceptance for each family:

- reviewed discovery/evidence accuracy is recorded;
- no executable false positive occurred during shadow mode;
- domain crawl policy is approved;
- scraper capabilities and expected readiness are accurate;
- operational cost and notification volume remain within configured budgets;
- failure and rollback drills have passed before enabling automatic action.

## Deferred decisions

Do not implement these merely because they appear attractive:

- Firestore/PostgreSQL migration without a documented trigger;
- calling two LLM providers for every check;
- a public endpoint that accepts arbitrary Mac mini commands;
- automatic Codex merge or production deployment;
- public rehosting of PDFs based only on a downloadable URL;
- a real-time dashboard before cases and operations show it is necessary.
