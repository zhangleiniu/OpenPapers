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
| Existing baseline | Deterministic monitor, local LaunchDaemon/SQLite, email; paused Cloud Run rollback | Implemented |
| 0 | Contracts, policies, ownership, and safety boundaries | Implemented |
| 1 | LLM search discovery in shadow mode | Shadow (15-venue live review, 2026-07-13) |
| 2 | Evidence verification and lifecycle state | Shadow (P2.S 15-venue live review, 2026-07-13) |
| 3 | Cases and fatigue-resistant notifications | Shadow (P3.S one-delivery canary, 2026-07-13) |
| 4 | Local Mac scheduler, execution safety, and immutable results | Implemented (single-writer cutover and timed rollback, 2026-07-14) |
| 5 | Automatic execution of existing scrapers | Planned |
| 6 | Budgeted Codex diagnosis and repair proposals | Planned |
| 7 | Dataset promotion and MustCite deployment | Planned |
| 8 | Venue rollout and operational hardening | Planned |
| 9 | Read-only status export for an external dashboard consumer | Planned |

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

Accepted P3.1 persistent-case implementation:

- `automation/cases.py` derives one stable case per venue/year/blocker and
  validates the existing version 1 case contract with additional identity,
  timestamp, status, snooze, resolution, and non-empty-evidence semantics;
- ordinary repeated observations advance `last_checked_at` without resetting
  `last_meaningful_change_at`; new evidence or a changed summary is meaningful,
  and only new evidence automatically reactivates a dormant case;
- resolve, snooze, ignore, and reactivate are pure, validated controls. Human
  terminal states remain closed until explicit reactivation; and
- control-state schema version 2 atomically migrates valid version-1 local
  databases and retains one current case per key plus immutable revisions and
  observation/control events under the existing singleton lease. Exact event
  replay is a no-op, conflicting ID reuse fails, and default queries omit
  terminal cases.

P3.1 is not connected to P2.5 action intents or the deployed monitor. It adds
no notification construction or delivery behavior.

Accepted P3.2 reminder-policy and digest implementation:

- `automation/reminders.py` validates the existing case and policy contracts
  and uses an injected aware clock plus `last_meaningful_change_at` to derive
  non-mutating case aging and stable due slots;
- weeks 1-4, weeks 5-12, the exact dormant threshold, later dormant cadence,
  active/expired snoozes, closed cases, meaningful-change reset, and regressing
  clocks have deterministic tests;
- `open` cases age to projected `stalled`/`dormant` copies, while existing
  dormant state remains closed until P3.1's explicit/new-evidence reactivation
  semantics apply; and
- one immutable in-memory digest contains every currently due case once,
  retains evidence references, and groups cases in stable weekly, monthly, and
  dormant urgency order.

P3.2 is not connected to the case repository, P2.5 intents, or the deployed
monitor. It records no sent state, delivery attempt, immediate notification,
retry, redaction, email, or other transport.

Accepted P3.3 notification-delivery-boundary implementation:

- a strict version 1 notification-intent contract and pure builders produce
  stable immediate or grouped-digest messages from explicit sources, retain
  evidence/run references, bound message size, and redact common credential,
  authorization, cookie, token, and signed-URL forms before persistence;
- every upstream event or reminder-slot source may belong to only one immutable
  intent. Control-state schema version 3 migrates valid v1/v2 local databases
  and retains lease-protected intent/source rows plus numbered attempt history;
- an attempt is committed as in-flight before the injected transport is called.
  Delivered, permanent-failure, and unresolved in-flight replay makes no new
  call; retryable typed failures permit an explicit later attempt; and raw
  exception text is never stored; and
- fixed-clock temporary-database tests use only a fake transport and cover
  success replay, retries, permanent failure, source conflicts, redaction,
  corruption, lease loss, and ambiguous post-acceptance failure.

P3.3 does not consume P2.5 actions, case events, repository cases, or scheduled
reminders and does not add email, SMTP, HTTP, webhooks, Prefect, a cloud
provider, recipients, or live delivery.

Accepted P3.4 shadow-integration implementation:

- typed P2.5 transition actions register one immediate shadow intent, while
  create/update-case actions derive one stable observation per blocker and
  register immediate output only for a meaningful retained case event;
- case events commit before notification registration in a separate
  transaction. A registration failure cannot erase the case, and exact replay
  can register the missing output without another case revision;
- unresolved repository cases feed the P3.2 clock projection. Reminder slots
  already owned by an immutable intent are filtered, and every remaining due
  item is grouped into one digest intent; and
- registration-only persistence retains strict schema-v3 intents and unique
  sources as `pending` with zero attempts. Fixed-clock temporary-database tests
  prove replay/reopen, one-event/one-intent conflicts, partial-failure
  recovery, closed-case omission, grouped urgency, and claimed-slot filtering.

P3.4 calls no transport and adds no recipient, credential, external request,
Prefect/deployment wiring, production-state migration, or action execution.

Accepted P3.S delivery/fatigue canary:

- a separate Resend HTTPS adapter makes at most one non-redirecting request,
  uses the stable notification ID for provider idempotency, bounds response
  handling, and maps only secret-free failure categories into P3.3 state;
- the manual command refuses without `--live`, an isolated marked root, and a
  SHA-256 match for one approved recipient. It accepts no event, case,
  notification, database, or P3.4 input and constructs only a fixed synthetic
  weekly/monthly/dormant digest;
- the authorized review recorded one provider-accepted request and one durable
  delivered attempt. Reopen suppressed transport replay, the local rate-limit
  drill retained a retryable failure with no case rows, and removing canary
  recipient configuration refused before output or I/O; and
- the 1,334-character, 36-line grouped message kept synthetic evidence/run
  references and clear urgency headings. It is suitable for the sampled
  three-item volume; high-volume fatigue remains unproven.

The review is
[`phase3-delivery-review-2026-07-13.md`](./phase3-delivery-review-2026-07-13.md).
Phase 3 is `Shadow`, not `Implemented`: the canary is manual and isolated,
provider acceptance was not independently confirmed at the mailbox, P3.4
outputs remain pending, and no production flow, scheduler, state store, or
recipient is connected.

## Phase 4: Mac mini execution plane

Deliverables:

- local single-writer due-work scheduler over durable SQLite state;
- headless system LaunchDaemon installation and rollback runbook;
- worker health check and Codex login check;
- venue/year locks, disk checks, timeouts, cancellation, and idempotent job IDs;
- immutable local job-result/manifest publishing; and
- optional create-only GCS backup/export that is not required for scheduling.

Acceptance:

- worker resumes after Mac reboot and SSH disconnect;
- the Mac requires no public inbound command endpoint;
- duplicate delivery does not repeat a completed scrape;
- a shadow Mac cannot update cloud-owned state, and after cutover only the Mac
  can update local control state;
- local logs and health output are bounded and artifacts contain a stable
  result manifest;
- missed wakeups resume from durable `next_check_at` state; and
- cloud and local control writers are never active against the same state.

Accepted P4.1 queue/submission foundation:

- version 2 typed jobs derive and revalidate one full SHA-256 identity over
  their request and execution semantics, while version 1 remains compatible
  for retained validation;
- a strict envelope maps scrape, validation, and Codex job types to three
  fixed queues in the local `openpapers-mac` process work-pool blueprint and
  rejects arbitrary queue, command, field, secret, or identity drift;
- an explicitly supplied P2.5 existing-scraper action can produce only the
  closed archival scrape job. Validation and Codex producers remain later
  packages; and
- the cloud submission coordinator and Prefect deployment adapter validate
  before flow-run creation, confirm the deployment's configured pool/queue,
  and use the job ID as the flow-run idempotency key. Sanitized fixtures and a
  fake client prove exact replay and failure closure without changing Prefect,
  GCP, Mac, scheduler, scraper, or control state.

Accepted P4.2 Mac worker foundation:

- an optional Mac package revalidates P4.1 envelopes in a pure simulator, and
  its thin Prefect flow accepts only the envelope and returns a non-persisted,
  non-result `simulated` fixture observation;
- local health checks cover macOS, Python 3.12, repository/data paths, Prefect
  3.7+ and injected local configuration, plus a Codex auth marker using file
  metadata only. Reports retain no path, setting, credential, or exception
  text;
- an isolated Prefect dependency set and parseable credential-free `launchd`
  template/runbook documented the original user-agent installation,
  inspection, rollback, and recovery design without a public inbound endpoint;
  and
- fake typed jobs, temporary local fixtures, and static scope tests prove that
  no scraper, validator, Codex, arbitrary command, cloud state, GCS result, or
  external resource is used.

Accepted P4.3 local execution-safety semantics:

- a private versioned local journal writes an active claim before any injected
  fake work and atomically promotes only confirmed success. Exact completed
  replay does not call the starter, while an ambiguous claim blocks every job
  for that venue/year until recovery;
- a process-safe non-blocking venue/year lease serializes all typed jobs for
  one conference year, and both absolute and fractional disk thresholds must
  pass under that lease before a claim or start;
- injected fake handles prove bounded timeout and cancellation. Confirmed
  stopped failure/cancellation/timeout may retry under the same job ID;
  unconfirmed stop or post-start supervision failure remains claimed and
  requires recovery; and
- the fixed pull/offline policy leaves undelivered work in Prefect and creates
  no local buffer, expiry, resubmission, or replacement identity. Tests use
  temporary roots, fake disk/handles, and child processes only.

Accepted P4.4 immutable result protocol:

- strict job-manifest v1 and job-result v2 contracts derive their own
  fingerprints and bind job type, venue/year, and artifact/result semantics to
  one immutable P4.1 v2 job. Retained job-result v1 artifacts remain schema
  compatible but cannot cross the P4.4 boundary;
- an injected GCS bucket adapter writes the manifest before the result commit
  marker and uses create-only generation-match-zero preconditions for both.
  Exact canonical replay is accepted, conflicting bytes are never overwritten,
  and reads pin downloads to the observed object generation;
- control-state schema version 4 adds a cloud-only, lease-protected immutable
  consumption ledger. Exact job/manifest/result/name/generation replay is a
  durable no-op, while drift or stored corruption fails closed; and
- a thin local consumer composes exact-generation reads with that ledger but
  applies no lifecycle transition or action. Sanitized fixtures, a fake bucket,
  and temporary databases cover partial publication, restart replay, lease
  loss, migration, generation conflict, and corruption without live GCS.

Accepted P4.L1 local ownership and due-work foundation:

- control-state schema version 5 binds a database to exactly one immutable
  cloud or local owner. Legacy version 1-4 databases remain cloud-owned and a
  local role refuses them before migration; no ownership-transfer API exists;
- one plain-Python runner uses an injected aware clock and the existing
  singleton lease to record a bounded wakeup and select persisted
  `next_check_at <= now` conference-year state;
- exact completed wakeup replay is a no-op, unchanged due schedules are stable
  across later wakeups, and an interrupted active wakeup remains an explicit
  recovery blocker after lease expiry; and
- fake-clock and temporary-SQLite tests cover due/not-due, missed wakeups,
  duplicate selection, hard bounds, lease contention, restart, ownership
  mismatch, legacy state, and ambiguity without an external effect.

Accepted P4.L2 fixture-only local control composition:

- control-state schema version 6 retains bounded plan counts while a due
  wakeup remains active and marks it completed only after every composed
  selection succeeds. Partial domain commits therefore leave durable
  ambiguity instead of suppressing uncertain work as completed;
- one plain-Python coordinator holds the local lease across catalog-bounded
  injected fake discovery, separately injected strict verification, retained
  lifecycle reduction, case/pending-shadow integration, and one due reminder
  projection;
- strict venue/year/time/bundle bounds and a schedule-advance requirement fail
  closed, while exact completed replay makes no fake call; and
- fixture/fake-clock/temporary-SQLite tests prove state, case, reminder,
  pending-intent, inert-action, replay, ownership, and interruption behavior
  without delivery, execution, network, daemon, external volume, or production
  authority.

Accepted P4.L3 headless local service package:

- strict normalized configuration derives control SQLite and atomic bounded
  health/run records below a private internal root that must be disjoint from
  the external execution volume;
- typed macOS/Python/repository/internal-storage/volume health checks and a
  concrete local-only mount probe block before an injected wakeup or control
  database. Tests use a fake probe, fake clock/effect, and temporary paths;
- a pure renderer returns a fixed, credential-free, low-impact hourly system
  LaunchDaemon with no shell, environment, keepalive, socket, or unbounded
  launchd logs; and
- exact rollback data names only the OpenPapers label and plist while
  preserving state/logs, repository, external data, and unrelated services.
  No install, removal, service-manager call, account access, or host drill is
  performed.

Accepted P4.LS isolated host shadow and drills:

- an exact private marker gates the only concrete installed effect, which calls
  the bounded local scheduler against isolated local-owned SQLite and has no
  network, notification, job, command, result, or production-state capability;
- a root-owned read-only runtime, minimal isolated Python environment,
  dedicated non-login role, private internal records/state, and private
  external execution directory back one authorized system LaunchDaemon;
- exact duplicate wakeup, missing-volume closure without unmounting shared
  storage, ambiguous-wakeup preservation and archive/new-root recovery, scoped
  rollback/reinstall, real SSH disconnect, and reboot resumption all passed;
  and
- every mutation/reboot gate retained bounded application records and passed
  the private co-resident health check with all five expected labels. The cloud
  monitor remained authoritative throughout.

P4.LC completed the production boundary:

- a strict private marker binds the local configuration to a
  generation-stable monitor backup while secrets remain outside plist,
  records, tests, documentation, and Git;
- the local effect preserves the existing three-venue/six-source deterministic
  monitor and TLS SMTP change/error notification, plus the separate schema-v6
  local scheduler, without adding scraper or arbitrary-command authority;
- two backups, zero-active-run gates, initial/final 6/6 zero-error checks, and
  local/co-resident health passed without cloud/local writer overlap; and
- real rollback stopped local before cloud resume, recovered Cloud Run state,
  and completed in 96 seconds. Final activation paused/drained cloud again and
  refreshed the recovered generation before starting local.

The durable sanitized review is
[`phase4-local-cutover-review-2026-07-14.md`](./phase4-local-cutover-review-2026-07-14.md).
It records the host-shadow drills, backup fingerprints, no-overlap sequence,
timed rollback, final runtime synchronization, validation, and retained risks
without credentials, private state, or remote generations.

Phase 4 is `Implemented`. No Mac execution service yet selects or runs a
scraper/validator command; that is Phase 5. P4.O's Prefect
feasibility gate failed before resource creation because the acceptable cloud
plan does not support the required hybrid process pool. P4.O is therefore
`Paused`; paying for or self-hosting orchestration is not justified for this
workload.

The accepted [local-first decision](./local-first-decision.md) preserves the
typed identity, safety, and immutable-result semantics while replacing the
Prefect pull transport with a bounded local scheduler. P4.L1 implements its
isolated ownership/selection core and P4.L2 composes accepted domains with fake
effects only. P4.L3 packages the one-shot host boundary and leaves its ordinary
CLI effect unconfigured. P4.LS installed and drilled the separately marked
scheduler-only mode. P4.LC completed generation-bound state transfer,
capability-equivalent deterministic monitoring, no-overlap local ownership,
health checks, and timed rollback. P5.1 is next.

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

## Phase 9: external status export

The consumer of this phase — a browser or tablet dashboard showing upcoming
venues and pending cases — is a separate, independently maintained
application outside this repository. This repository's only responsibility is
making already-owned control-plane state cheaply and safely readable by that
consumer, without becoming its backend.

Deliverables:

- a versioned `dashboard-status` schema (`additionalProperties: false`)
  admitting only fields safe for broad or public read access: venue, year,
  lifecycle state, `next_check_at`, verified milestone dates, open-case count
  and urgency grouped by blocker, and recent job-result summaries
  (kind/venue/year/status/timestamp);
- explicit schema exclusions: evidence/crawl URLs, raw discovery or
  verification payloads, case free-text notes, credentials, and internal file
  paths;
- the existing single control-plane writer emits/overwrites one export object
  as the last step of an already-authorized commit (conference-state
  revision, case event, or job-result consumption). This is a derived,
  latest-only materialized view, not additional source-of-truth state, and
  creates no new writer role or second lease holder; and
- the export is written to a GCS location dedicated to this purpose and
  separate from `control_state`, snapshots, and discovery evidence, so its
  read permission can be granted (including public/unauthenticated GET with
  CORS enabled for direct browser fetch) without touching any bucket that
  holds control-plane internals or credentials.

Acceptance:

- schema validation rejects an unknown field or an excluded evidence-shaped,
  free-text, or credential-shaped value;
- fixture tests prove the export write is a side effect of an
  already-authorized commit, using no new lease, writer role, or live
  network/GCP call;
- a local command can produce a sample export from fixture control-state
  without a live GCP project, matching the `run_discovery`/
  `run_verification_shadow` manual-command convention; and
- IAM for the export location is reviewed separately from the control-plane
  bucket before any public/broad read grant.

Out of scope for this repository: the dashboard/kiosk frontend, any query API
beyond the static export object, authentication, and real-time/push delivery.

## Deferred decisions

Do not implement these merely because they appear attractive:

- Firestore/PostgreSQL migration without a documented trigger;
- calling two LLM providers for every check;
- a public endpoint that accepts arbitrary Mac mini commands;
- automatic Codex merge or production deployment;
- public rehosting of PDFs based only on a downloadable URL;
- a real-time or push-based dashboard, or a query API, inside this
  repository — Phase 9 defines only a narrow periodic pull-based export for
  an external consumer, not a dashboard service.
