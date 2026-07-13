# Automation architecture

This document defines the target boundaries and safety invariants. Most of the
components described here are planned; consult [roadmap.md](./roadmap.md) and
the executable code before assuming a component exists.

## Implemented foundation and Phase 1/2.S/P3.1/P3.2 boundaries

Phase 0 is implemented as a side-effect-free foundation and is not yet wired
into the deployed monitor:

- `automation/schemas/v1/` contains Draft 2020-12 JSON Schemas with strict
  required fields and `additionalProperties: false` at executable boundaries;
- `automation/contracts.py` validates versioned artifacts and creates canonical
  fingerprints;
- `automation/config/venue_catalog.v1.json` separates the target venue catalog
  from the current three-entry `automation/conferences.json` monitor registry;
- `automation/config/policies.v1.json` holds conservative reminder, provider,
  Codex, systemic-failure, dynamic scheduling, crawl, and publication defaults;
- `automation/domain.py` implements the vocabulary and pure transition,
  idempotency, storage-ownership, and secret-shaped-field checks; and
- `automation/scheduling.py` derives `next_check_at` from verified
  conference-year milestones without network or orchestration effects.

Phase 1 now adds an optional, shadow-only discovery path:

- `automation/discovery.py` defines provider-neutral discovery, strict
  normalization, immutable evidence retention, cache, budgets, retry,
  concurrency, and second-provider escalation;
- `automation/providers/gemini.py` is the first Search Grounding adapter and
  imports `google-genai` only for the optional live path. It searches once,
  then uses a no-tool structured call over the untrusted grounded report and
  an allowlisted source table. The model emits short source IDs and
  deterministic adapter code resolves them to exact grounding URIs; and
- `automation/run_discovery.py` requires `--live` and permits an explicitly
  selected venue from the validated catalog. This manual development surface
  is unmetered and does not read or write the automatic-caller budget ledger.

This path is not deployed or scheduled. Contract-valid live artifacts and a
manual review now cover all 15 catalog venues, but they remain unverified
discovery evidence until replayed by a deterministic verifier. No scheduled or
deployed content verifier, persistent reducer, action router, Mac worker, or
Codex adapter consumes discovery output. P2.4's local persistence repository
can retain an explicitly supplied, already validated artifact bundle, and
P2.5 can reduce such retained fixture/local records; no live or scheduled
caller supplies one.

Phase 2.1 plus its P2.1R hardening add verifier contracts and effect boundaries
without claiming content verification:

- version 2 `verification-request.json` binds each selected claim/milestone ID
  and derived verification kind to one exact discovery ID and evidence
  fingerprint; semantic readers continue to validate consistent version 1
  artifacts against their discovery source;
- version 2 `verification-result.json` carries typed source observations,
  sanitized redirect targets, findings, verified facets/milestones, and
  evidence identities, but cannot carry an action, command, job, or transition;
- cross-artifact semantic validation recomputes request/result identities and
  rejects kind drift, dangling evidence, evidence-free positive results,
  inconsistent overall status, missing fetch-policy provenance, and unsafe
  retained URLs;
- `automation/verification.py` classifies official/archival catalog domains
  without granting fetch permission, and `CrawlPolicyGate` requires a separate
  approved domain permission before calling an injected fetcher;
- `EvidenceFetcher` receives one no-auto-redirect request plus the reviewed
  crawl constraints, derives and retains a sanitized edge without requesting
  its target, and leaves each next hop for Phase 2.2 to authorize; and
- `SnapshotStore` defines immutable evidence retention, with a local
  content-addressed implementation that stores allowlisted headers, a replayable
  redirect edge, and reuses identical fake/fixture observations.

P2.1 contains no live HTTP adapter and does not parse HTML or PDFs, persist
conference state, apply transitions, compute actions, or change the deployed
monitor. P2.1R closed the initial schema/semantic drift, redirect-loss, and
unsafe URL retention findings.

P2.2 consumes that interface in `automation/html_verification.py`:

- `fetch_html_evidence` composes only the injected one-request fetcher and
  snapshot store. It classifies and policy-gates every exact redirect URL,
  bounds loops and hops, and retains partial evidence when a later target is
  closed by policy;
- `ElementSelector` and `HtmlVerificationProfile` describe reviewed source
  shapes without embedding a general selector engine or venue scraper;
- the bounded parser requires venue aliases/display name and event year in one
  title/heading region, matches exact candidate dates, counts distinct titles
  and title/author/abstract completeness, and requires actual proceedings
  index entries rather than future-looking prose; and
- `verify_html_evidence` accepts only cited P2.2 targets, rejects PDF targets,
  records conflicts conservatively, and delegates final v2 artifact semantics
  to the hardened P2.1R result builder.

P2.2 uses only fakes, sanitized fixtures, explicit profiles, and temporary
snapshot roots. It adds no live transport, default crawl permission, PDF
inspection, lifecycle-state writer, reducer, action, or deployment path.

P2.3 consumes the same accepted foundation independently in
`automation/pdf_verification.py`:

- `build_pdf_sample_plan` ranks each PDF claim's exact cited URLs by an
  immutable request/target/URL hash and selects at most three by default, with
  a hard bound of ten, so input ordering and ambient randomness cannot alter a
  replay;
- `fetch_pdf_evidence` independently classifies and policy-gates every exact
  redirect hop for `pdf_fetch_for_processing` and separately requires
  `store_internal_copy` before retaining sanitized observations and immutable
  fixture/fake snapshots. It stops before a denied target, loop, or redirect
  limit;
- final responses must have HTTP 200, at least 1024 actual bytes by default,
  a matching numeric Content-Length when present, and `%PDF-` at byte zero;
  file extensions and Content-Type labels are not treated as proof; and
- `verify_pdf_evidence` accepts only selected URLs from requested PDF claims,
  recomputes catalog classification, records sampled/valid counts, and emits
  `pdf_status=ready` only when every selected sample passes. A supported subset
  may be `partial`, but missing, unsafe, untrusted, or invalid evidence cannot
  produce readiness.

P2.3 uses no live transport or HTML identity inference and grants no
`redistribute_pdf` permission. P2.2/P2.3 findings remain data only.

P2.4 adds `automation/control_state.py` as a separate standard-library SQLite
repository for the cloud control plane:

- schema version 1 is created only from an empty database; newer, malformed,
  and populated unversioned databases fail closed so the deployed monitor
  database cannot be mistaken for control state;
- one expiring singleton lease is acquired, renewed, and released with an
  opaque token, and every mutable history/state write rechecks that token in
  the same `BEGIN IMMEDIATE` transaction;
- a discovery, verification request, and verification result are retained as
  one canonical bundle after existing cross-artifact validation. Equivalent
  semantic replay is a no-op, conflicting identity is rejected, and ordered
  reads revalidate stored fingerprints and semantics;
- conference state is stored as an optimistic current revision plus immutable
  revision history. Identical writes are no-ops and stale revisions fail; and
- repository construction enforces the existing cloud-only `control_state`
  ownership rule.

P2.4 uses temporary databases in tests and has no deployed migration, GCS
adapter, reducer, scheduler, router, or action. It remains a persistence-only
module.

P2.5 adds `automation/lifecycle.py` and `automation/control_plane.py` without
changing that storage boundary:

- a strict verification bundle is revalidated and each positive facet or
  milestone must map to a retained HTTP-200 snapshot from an official or
  archival URL whose trust is recomputed from the catalog;
- facet promotion is monotonic, release observations and verified candidate
  milestones retain provenance, and the highest newly justified lifecycle
  state is applied through `automation.domain.apply_transition`;
- every consumed verification ID is retained in conference state, schedules
  use the first retained `verified_at`, and replaying an already consumed
  record is a no-op;
- continuous publications reject conference facets/milestones into human
  review, and conflict/review/crawl blockers suppress executable intents;
- the router returns immutable, stable recheck, transition-notice, case,
  human-review, or existing-scraper intents as data. Only a newly supported,
  overall-verified PDF-ready result can return the scraper intent; and
- the thin coordinator reads the current optimistic revision and stores the
  pure reduction under the caller's existing P2.4 lease.

P2.5 fixture replay covers every catalog venue plus annual and continuous
lifecycle shapes, including semantically compatible v1 artifacts. It has no
live transport, action store/dispatcher, case or notification service, job
submission, scraper invocation, GCS adapter, Prefect wiring, or deployed
migration. Returning a queue intent does not claim `ingestion_queued` and does
not perform an effect.

P2.S adds the live transport only at a separate manual shadow boundary:

- `automation/live_fetch.py` rejects IP-literal targets and DNS answers that
  are empty, malformed, non-global, or mixed public/private; connects directly
  to one reviewed public IP; and verifies TLS with the original hostname;
- the adapter performs one bounded GET, retains only allowlisted response
  headers, follows no redirect automatically, serializes this manual sample,
  and honors policy delay, 403/429, Retry-After, and CAPTCHA stop semantics;
- `automation/config/p2s_shadow_policy.v1.json` is separately reviewed and
  shadow-only. It grants metadata fetch and bounded PDF processing/internal
  retention, but no redistribution permission, and leaves `ecva.net` in
  review;
- `automation/verification_shadow.py` selects catalog-bounded citations from
  retained grounding metadata, then independently reclassifies and gates every
  actual redirect hop. It writes only content-addressed snapshots, strict
  verification bundles, isolated SQLite state, and inert action previews below
  an explicit shadow root; and
- `automation/run_verification_shadow.py` refuses remote access without
  `--live`, requires explicit non-overlapping roots, and is not imported by the
  monitor or any scheduled/deployed path.

The reviewed 2026 sample covered all 15 catalog venues and 28 targets. Two
exact future milestones verified, seven discovery `partial`/`ready` PDF
signals failed exact-URL signature checks, the EMNLP proceedings false positive
did not promote, JMLR stayed on its continuous policy, and no queue intent was
returned. The live review also found conservative source-shape gaps; Phase 2
is therefore `Shadow`, not `Implemented`. There remains no scheduled/deployed
verifier, action store/dispatcher, notification service, job submission,
scraper execution, GCS integration, or production-state migration.

P3.1 adds a local case domain and extends the same control repository; P3.2
adds a separate pure reminder projection:

- `automation/cases.py` derives the stable case identity from
  venue/year/blocker, preserves one case per key, separates ordinary checks
  from meaningful evidence/summary changes, and applies resolve, snooze,
  ignore, and reactivate controls without storage or transport effects;
- new evidence reactivates a `dormant` case, but it does not silently override
  `resolved`, `ignored`, or `wont_fix`; those require an explicit reactivate
  control; and
- control schema version 2 migrates a valid version-1 database atomically and
  stores lease-protected case current rows, immutable revisions, and immutable
  event records. A stable event ID is a no-op on exact replay and a conflict
  when reused with different meaning; and
- `automation/reminders.py` validates case/policy inputs, uses
  `last_meaningful_change_at` for clock-controlled aging, returns defensive
  `stalled`/`dormant` state projections and stable cadence slots, and groups
  all due cases into deterministic weekly, monthly, and dormant digest data.

P3.1 accepts explicitly supplied observations and controls only. P3.2 accepts
explicit case states, policy, and an aware clock only; it does not persist its
aged copies or record a delivery. Neither package consumes P2.5 action intents,
creates a notification intent, delivers email or another transport, or wires
into the deployed monitor.

## Design principles

1. **Discovery is not proof.** An LLM can find candidate facts and URLs, but a
   deterministic verifier must support an executable state transition.
2. **Business state is separate from orchestration state.** Prefect records
   runs, retries, and logs; OpenPapers owns conference, case, and job state.
3. **One mutable owner per state domain.** Avoid cross-host SQLite writes.
4. **Heavy work runs near the data.** PDF scraping, validation, and Codex run
   on the Mac mini rather than the lightweight Cloud Run monitor.
5. **Escalate capabilities gradually.** Existing scraper, then validator, then
   Codex diagnosis, then an isolated proposed patch.
6. **External content is untrusted.** A page cannot grant permissions, expose
   secrets, or instruct an agent to broaden its task.
7. **Optional means isolated.** Core scraper dependencies and workflows must
   not acquire automation-only requirements.

## Components and ownership

| Component | Runs on | Responsibility | Must not do |
|---|---|---|---|
| Scheduler | GCP/Prefect | Start due discovery and reminder flows | Decide conference facts |
| Discovery provider | Cloud control plane | Return structured claims, citations, and uncertainty | Trigger scraping directly |
| Evidence verifier | Cloud control plane | Fetch/corroborate sources and classify readiness | Execute arbitrary page instructions |
| State transition service | Cloud control plane | Apply valid idempotent transitions | Accept unverified LLM claims |
| Action router | Cloud control plane | Select notify, recheck, queue, or review | Submit commands outside approved job types |
| Notification service | Cloud control plane | Immediate transitions and periodic digests | Send duplicate stateless alerts |
| Prefect worker | Mac mini | Pull approved typed jobs | Expose a public command endpoint |
| Scrape executor | Mac mini | Run existing repository commands | Modify scraper code |
| Validator | Mac mini | Enforce metadata/PDF contracts | Promote invalid data |
| Codex adapter | Mac mini | Diagnose and propose tested repairs | Modify the primary checkout or auto-merge |
| Promotion/deployment | Mac mini or CI | Publish an approved validated candidate | Infer redistribution rights from HTTP access |

## Discovery contract

A provider must return a schema-constrained result containing at least:

```text
venue, year, checked_at
conference_status
paper_list_status
metadata_status
pdf_status
proceedings_status
claims[]
  - claim
  - evidence_urls[]
  - source_type
  - observed/published date when available
confidence
uncertainties[]
```

The implemented version 1 form is
`automation/schemas/v1/discovery-result.json`. It additionally records a
stable discovery ID, provider/model and prompt version, claim IDs, an evidence
fingerprint, strict source types, and an additive typed list of candidate
milestones. Unknown fields such as `action` or `command` are rejected.

Candidate dates found by the provider remain discovery evidence. They do not
enter conference state or alter a schedule until deterministic verification
confirms the venue/year and an official or recognized archival source.

The provider-neutral interface allows another search provider later. The
first implementation uses Gemini Search Grounding because GCP is already part
of the project. A second provider remains an unconfigured exception interface
for low confidence or conflicting evidence, not a default call for every
venue.

Discovery results and cited responses must be retained as evidence artifacts.
The implemented pre-call request fingerprint includes provider, model,
prompt/schema version, and venue/year. Immutable artifact paths additionally
include the post-call evidence fingerprint. Only normalized results and
allowlisted grounding source/query metadata are retained; raw SDK transport
objects are not.

## Evidence verification

Before an action can be queued, deterministic code checks applicable facts:

- venue and year match the cited content;
- URLs are reachable and redirects are recorded;
- the source is official, recognized archival infrastructure, or independently
  corroborated;
- a claimed list contains a plausible number of distinct paper entries;
- metadata fields required for the claimed readiness level exist;
- claimed PDF URLs are reachable and sampled files have a `%PDF-` signature;
- evidence is not merely a search snippet repeating an unsupported claim;
- crawl policy permits the proposed request type.

Ambiguous or conflicting evidence creates/rechecks a case. It does not trigger
an execution job.

P2.2 implements redirect, venue/year identity, candidate-date, list-count,
metadata, and proceedings-index verification, including fixture regressions
for the known EMNLP future-index, NAACL/ACL identity, and IJCAI no-PDF false
positives. It does not prove that all 15 live venue shapes are configured or
healthy. P2.3 implements deterministic PDF permission, exact cited-URL,
status, size, signature, and bounded-sampling verification with fake responses
and sanitized fixtures. P2.4 persistently retains already validated bundles
and state revisions behind a lease. P2.5 connects authoritative retained
findings to transitions, scheduling, and typed action data without executing
those actions. P2.S supplies the bounded live shadow observation recorded in
`phase2-live-review-2026-07-13.md`; its source-profile gaps remain inputs to
the later venue-family rollout, not permission to weaken the verifier.

## Conference-year state

The lifecycle vocabulary is:

```text
unknown
scheduled
conference_ended
paper_list_released
metadata_ready
pdf_partial
pdf_ready
ingestion_queued
ingesting
validated
published
```

Readiness is not always linear. Formal proceedings may appear after a
provisional accepted list has already been ingested. Store evidence-backed
facets as well as a summarized lifecycle state so a source upgrade can be
represented without losing history.

Blocker reason codes include:

```text
no_public_list
no_pdf
unknown_download_source
unsupported_scraper
scraper_failed
validation_failed
agent_pending
codex_patch_pending
human_review_required
crawl_policy_denied
```

Every transition records its previous state, new state, evidence IDs, reason,
actor, and timestamp. Replaying the same evidence must be idempotent.

JMLR is continuously published and requires a lifecycle policy different from
annual conferences. Do not fabricate a conference-ended transition for it.

`automation/domain.py` implements this transition table as pure Python.
Only a deterministic verifier, job-result consumer, or human actor can request
a transition; an LLM discovery actor is not valid. Equivalent evidence replay
is a no-op, conflicting reuse is rejected, and the continuous-publication
guard prevents JMLR from entering `conference_ended`. Persistent state and the
deterministic evidence verifiers now exist locally. P2.5 applies authoritative
retained findings through this reducer and stores the next optimistic revision;
there is still no live or deployed caller.

The version 1 state also stores nullable evidence-backed milestones for
conference start/end, acceptance notification, expected paper-list and
proceedings release, and observed release. Static venue-level check months are
deliberately not part of the catalog: these dates vary by year and must be
discovered and verified. `automation/scheduling.py` selects `next_check_at`
from expected release dates first, otherwise wakes shortly before another
verified milestone, applies configured post-conference backoff when proceedings
remain absent, and uses a low-frequency fallback when dates are unknown. A
maximum-silence guard permits occasional revalidation of far-future dates.

## State and result storage

The initial design keeps SQLite and GCS. It avoids distributed writes as
follows:

```text
control/state.sqlite3       cloud control plane is the only writer
snapshots/...               content-addressed immutable source evidence
discoveries/...             immutable structured LLM responses
job-results/<job-id>.json   Mac mini writes once; cloud consumes
manifests/<job-id>.json     immutable scrape/validation manifest
```

Requirements:

- prevent overlapping control-plane runs with a lease;
- use GCS object-generation preconditions for mutable state upload;
- use stable job IDs and reject an existing result object;
- never synchronize the control SQLite tree from the Mac mini;
- have the cloud state reducer consume job results exactly once;
- retain enough immutable input to reproduce a transition or diagnosis.

Phase 0 expresses the ownership and write-once rules in
`automation/domain.py`: the cloud control plane owns mutable control state and
cloud evidence/discovery/verification objects, while the Mac worker owns
immutable job results, manifests, and Codex results. P2.1's
`FileSnapshotStore` proves local content-addressed source snapshot replay but
is not the cloud state store or a GCS adapter. P2.4's
`ControlStateRepository` implements schema-versioned local SQLite control
state, an expiring singleton writer lease, atomic idempotent verification
bundle retention, validated ordered replay, and optimistic conference-state
revision history. P3.1 advances that database to schema version 2 with
deduplicated case current/history/event storage under the same lease. It
deliberately rejects a populated unversioned database and does not migrate or
share the deployed monitor's database. `JobResultRegistry` is a pure executable
model of the job protocol: an identical result replay is accepted as already
seen, while a different result for the same job ID is rejected. P2.5 now
composes retained verification replay with optimistic state updates locally.
GCS generation preconditions, cloud restore/upload, deployed integration,
case-intent consumption, and job-result consumption remain future work.

Schema version 2 has no deployed migration or current operator action. A valid
local version-1 control database migrates on open, preserving its verification
and conference-state data. Before any future durable operator database is
opened by version-2 code, stop overlapping writers and take a backup; rollback
after migration requires restoring that backup because older code must reject,
not downgrade or delete, a newer schema.

Evaluate Firestore or PostgreSQL only after a concrete trigger: multiple
control-plane writers, unavoidable overlapping state updates, a real-time
dashboard/API, multiple workers requiring transactional coordination, or
observed state-loss/recovery problems.

## Action routing

The router consumes verified state and emits typed actions, never shell text:

```text
recheck_at
notify_transition
create_or_update_case
queue_existing_scraper
queue_codex_diagnosis
request_human_review
prepare_promotion_candidate
```

The `ActionType` vocabulary and strict job payload contracts are implemented.
P2.5 now provides a pure router for stable immutable action intents. Its closed
payload dataclasses cannot contain shell commands, and no router output is
persisted, submitted, or executed. P3.1 can persist separately supplied case
observations but is not an action consumer. Router-to-case integration,
notification delivery, job creation/submission, and command selection remain
their later packages.
Job payload contracts continue to enumerate approved fields for existing
scraper, validation, and Codex-diagnosis jobs and cannot contain arbitrary
shell commands.

Examples:

- accepted list but no PDF: case plus scheduled recheck;
- PDF ready and supported scraper: existing-scraper job;
- transient HTTP failure: retry/backoff, no Codex;
- structural parse/validation failure after retries: Codex candidate;
- multiple venues fail with the same infrastructure symptom: systemic
  incident, circuit breaker, and one notification.

## Reminder lifecycle

Default reminder policy:

| Case age | Behavior |
|---|---|
| First observation | Immediate notification |
| Weeks 1-4 | Weekly digest |
| Weeks 5-12 | Monthly digest; mark `stalled` |
| After week 12 | `dormant`; quarterly or change-only notification |

Use `last_meaningful_change_at`, not merely `last_checked_at`, to calculate
age. New evidence reactivates a dormant case. Human controls must support
snooze, monthly, ignore, reactivate, resolve, and won't-fix outcomes.

P3.1 implements the timestamp distinction, new-evidence dormant reactivation,
and resolve/snooze/ignore/reactivate controls. P3.2 applies the configured
default windows as a pure projection: weekly slots occur at days 7, 14, 21,
and 28 after the last meaningful change; monthly slots are anchored to that
same timestamp while the case is `stalled`; at day 84 the case becomes
`dormant`, with later slots every configured dormant interval. Exact boundary
behavior is policy-derived rather than hardcoded, active snoozes are excluded,
and expired snoozes resume at their age-appropriate cadence. The digest keeps
stable case, evidence, and slot references and groups every due case in
weekly/monthly/dormant urgency order.

P3.2 records no last-delivery state, so replay produces the same due slot until
the clock crosses the next slot. Persistent delivery idempotency, retries,
redaction, immediate notifications, monthly override, won't-fix control,
case/action integration, and all transports remain P3.3 or later work.

## Cost and execution guardrails

Initial defaults are policy configuration, not hardcoded constants:

```yaml
discovery:
  max_calls_per_day: 20
  max_calls_per_venue_per_day: 20
  max_concurrency: 2
  max_second_provider_calls_per_day: 5

codex:
  max_runs_per_day: 3
  max_runs_per_venue_per_day: 1
  max_concurrency: 1
  same_error_cooldown_days: 7
  max_runtime_minutes: 60
```

These values live in `automation/config/policies.v1.json` and are validated by
`automation/configuration.py`. `DiscoveryService` consumes them through a
process-safe attempt ledger when a caller supplies one; every remote retry then
reserves another attempt before I/O. The explicit `run_discovery` CLI is a
manual development surface and deliberately supplies no ledger, so it is
unmetered. No deployed scheduler consumes discovery yet. Future scheduled or
automatic callers must supply the configured ledger and limits.

The system must cache discovery results, fingerprint errors, and refuse
recursive Codex triggering. If three or more venues fail in one run, open a
systemic incident and suppress per-venue Codex runs until the common cause is
classified. Budget exhaustion queues work as `agent_pending`; it must not
silently discard it.

Phase 1 error artifacts retain only a bounded category, HTTP status when
available, a fingerprint, and allowlisted structural diagnostics such as
finish reason, shape, length, and aggregate token counts; provider
response/error text is excluded. Both provider failures and deterministic
normalization rejection are recorded. Historical development ledger files are
not deleted, but the manual CLI neither reads nor writes them.

Gemini prompt v14 uses two remote calls per uncached discovery. An automatic
caller with a ledger reserves both atomically before the first call; the manual
development CLI does not. The second call has no Search or executable tool,
disables model thinking for deterministic transcription, and consumes the
first response's grounded text-segment-to-source mapping. It emits compact
source IDs rather than copying redirect URIs. The adapter maps IDs back to exact
URIs, derives source authority from the venue catalog, downgrades unsupported
status facets to `unknown`, and derives `ended` from a grounded conference-end
date and observation time. The request carries the catalog lifecycle kind:
annual non-event milestones may fall in the preceding calendar year, while a
continuous venue is normalized to unknown conference status with no conference
milestones. Exact public readiness and venue identity remain unverified
evidence until Phase 2 fetches and inspects the cited resource.

## Crawl and publication policy

LLM discovery never grants permission to fetch or publish. Before automated
requests to a domain, policy must define or review:

- `robots.txt` and applicable source terms;
- identifiable User-Agent and maintainer contact;
- per-domain concurrency, minimum delay, jitter, and request budget;
- `Retry-After`, 429, 403, and CAPTCHA stop behavior;
- API preference and prohibition on bypassing authentication, paywalls, or
  access controls;
- cache, ETag, hash, and resume behavior to avoid repeated downloads.

Permission dimensions are separate:

```text
monitor
metadata_fetch
pdf_fetch_for_processing
store_internal_copy
redistribute_metadata
redistribute_pdf
```

A public PDF URL does not establish redistribution permission. Default public
delivery should link to the authoritative PDF unless its redistribution terms
have been reviewed. Store provenance, source URL, retrieval date, and any
known license/rights metadata.

The Phase 0 policy schema defaults every unclassified domain to
`review_required`, grants no domain permissions by default, requires
`Retry-After`/429 and CAPTCHA stop behavior for approved domain entries, and
defaults PDF delivery to an authoritative link with redistribution disabled
until review.

## Codex boundary

Codex runs locally on the Mac mini using saved local Codex authentication or a
separately provisioned API credential. The first implementation should use
`codex exec --json`; adopt the SDK only when persistent thread control adds
clear value.

Each code-changing job must:

1. create a dedicated branch and git worktree;
2. receive only the relevant snapshot, failure report, and repository context;
3. use the least-capable sandbox that can perform the task;
4. avoid deployment and unrelated secrets;
5. add or update reproducible fixtures/tests;
6. run the repository completion checks;
7. output a schema-constrained result and diff summary; and
8. stop for review rather than merge or deploy.

Simple execution of an unchanged existing scraper does not require Codex.

## Security boundaries

- Cloud control plane: provider credentials, Prefect, SMTP, and read/write
  access only to control-plane storage.
- Mac worker: repository/data access, Prefect worker credentials, and local
  Codex authentication; no public inbound command endpoint.
- Codex subprocess: relevant repository worktree and task-scoped environment;
  no provider, SMTP, deployment, or unrelated secret environment variables.
- Logs/artifacts: redact tokens, passwords, cookies, and authorization headers.
- `.env`, `~/.codex/auth.json`, Prefect blocks, and Secret Manager values must
  never be copied into prompts, fixtures, commits, or notification bodies.
