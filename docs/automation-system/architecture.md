# Automation architecture

This document defines the target boundaries and safety invariants. Most of the
components described here are planned; consult [roadmap.md](./roadmap.md) and
the executable code before assuming a component exists.

## Implemented foundation and Phase 1/2.S/P3.S/P4.LC boundaries

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
verifier, action store/dispatcher, deployed notification service or real
transport, job submission, scraper execution, GCS integration, or
production-state migration.

P3.1 adds a local case domain and extends the same control repository, P3.2
adds a separate pure reminder projection, P3.3 adds an injected delivery
boundary, P3.4 adds local pending-output integration, and P3.S adds one
synthetic-only live canary:

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
  all due cases into deterministic weekly, monthly, and dormant digest data;
  and
- the strict notification-intent contract plus `automation/notifications.py`
  build stable immediate or grouped-digest messages from explicitly supplied
  sources, redact common credential forms before retention, preserve evidence
  and run IDs, classify typed failures, and call only an injected transport
  after a durable claim. Schema version 3 retains immutable intent/source and
  numbered-attempt history under the existing lease; and
- `automation/notification_integration.py` consumes only typed transition and
  case actions, derives one stable observation per blocker, persists case
  events before registering their meaningful immediate output, queries
  unresolved cases, removes reminder slots already claimed by an immutable
  intent, and registers one grouped digest for all remaining due items.
- `automation/resend_notifications.py` implements a one-request concrete
  Resend HTTPS transport with a fixed endpoint, no redirects or automatic
  retry, bounded responses, typed failures, and the stable notification ID as
  the provider idempotency key; and
- `automation/notification_canary.py` constructs only three synthetic cases at
  the weekly, monthly, and dormant boundaries. Its manual CLI requires
  `--live`, a fresh or marked isolated root, and an exact approved-recipient
  fingerprint before it creates the transport.

P3.1 accepts explicitly supplied observations and controls only. P3.2 accepts
explicit case states, policy, and an aware clock only; it does not persist its
aged copies. P3.4 never invokes the P3.3 protocol or claims an attempt: every
shadow record remains `pending` with zero attempts. The unique source mapping
proves one transition, case event, or reminder slot cannot acquire another
intent. P3.S never imports P3.4, reads its database, accepts event input, or
changes case state. Its first review made one provider-accepted delivery with
one attempt and one external request, then proved zero-call replay, typed
failure retention, empty case state, and credential-removal rollback. The
provider receipt proves API acceptance, not independent mailbox delivery.
P3.S also refuses to retry a root whose one attempt ended `retryable`; P3.3's
general explicit-retry capability remains available only outside this canary.
No package wires Phase 3 into the deployed monitor or adds a scheduler,
production recipient configuration, production Prefect integration, or
production-state authority.

P4.1 adds a local execution-queue contract and injected submission boundary:

- `automation/schemas/v2/job.json` replaces caller-selected job identity with
  a recomputable full SHA-256 identity over every execution-semantic field.
  Version 1 jobs remain readable compatibility artifacts, but only version 2
  jobs may cross the P4.1 queue boundary;
- `automation/schemas/v1/job-queue-envelope.json` and
  `automation/job_queue.py` bind each job type to one fixed queue in an inert
  `openpapers-mac` process work-pool blueprint. Pool and queue names are
  orchestration metadata outside the job identity, and semantic validation
  rejects mismatches or arbitrary routing;
- only the existing P2.5 `queue_existing_scraper` action has a producer: it
  becomes a closed archival scrape payload without copying scraper class,
  module, command, path, environment, or credential data into the job; and
- the asynchronous cloud coordinator validates before calling an injected
  submitter. The Prefect deployment adapter first confirms the deployment's
  configured pool and queue, then passes one queue envelope, its fixed queue,
  and the job ID as Prefect's idempotency key. It returns a bounded receipt
  rather than a Prefect model.

P4.1 tests use a fake Prefect client and local sanitized fixtures. No code
constructs a live client, creates a work pool/queue/deployment, installs a Mac
worker, persists or consumes a job, runs a command, publishes a result, or
connects the boundary to the deployed monitor or production control state.
P4.2 adds the receiving package without broadening that authority:

- `automation/mac_worker/runtime.py` revalidates the strict P4.1 envelope and
  produces a deterministic `simulated` observation that is deliberately not a
  job result or manifest;
- `automation/mac_worker/prefect_support.py` is the only Mac-package Prefect
  import. Its flow accepts exactly the submitted `queue_envelope`, disables
  result persistence/retries, and has no executor, command registry, or
  callable-dispatch parameter;
- `automation/mac_worker/health.py` reports bounded local macOS/Python,
  repository, data-root, Prefect-package/configuration, and
  Codex-login-marker signals.
  The Prefect signal is injectable and its concrete implementation reads only
  local settings; the Codex signal checks file metadata without reading the
  authentication file or starting Codex; and
- the isolated dependency file and `launchd` template/runbook define a future
  user-agent procedure with no public inbound endpoint and no credentials in
  the plist. P4.2 itself installs, configures, loads, or starts nothing.

Tests use fake jobs, fake Prefect settings signals, temporary paths, and local
fixtures. P4.3 adds the local safety boundary without adding an executable
command:

- `automation/mac_worker/safety.py` revalidates each envelope before local
  state, then holds a non-blocking process-safe venue/year lock across disk,
  claim, and fake supervision work;
- a private versioned Mac journal writes an exact active claim before calling
  an injected starter and atomically promotes that claim only after confirmed
  success. Confirmed completion suppresses exact replay. A prior active claim
  is never expired automatically and blocks every later job for that
  venue/year until recovery rather than risking overlapping work;
- both a minimum-free-byte and free-fraction threshold must pass under the
  lock. The injected handle receives a bounded runtime and cancellation signal;
  confirmed failure, timeout, or cancellation clears only its claim so the
  same immutable job ID may retry, while an unconfirmed stop or supervision
  fault leaves the claim blocking; and
- the fixed offline contract keeps Prefect as the sole pull-queue owner. No
  delivery means no local claim, buffer, expiry, resubmission, or replacement
  job identity is created.

P4.3 tests use fake handles, fake disk usage, temporary private directories,
and child processes. The local marker is neither a job result nor a manifest
and cannot authorize control state. P4.4 owns and implements the immutable
result boundary; the P4.L packages now own local scheduling, installation, and
operational drills. P5.1 owns pure command selection, P5.2 owns the isolated
existing-scraper staging/process boundary, and later packages own validation
and runtime composition.

P4.4 adds a strict immutable result protocol without connecting it to that
fake worker path:

- job-manifest v1 and job-result v2 derive their fingerprints from every
  semantic field and are cross-validated against the full immutable P4.1 v2
  job. The manifest contains only closed typed artifact summaries and safe
  relative object names; a successful result requires at least one artifact;
- `automation/job_results.py` uses fixed `manifests/<job-id>.json` and
  `job-results/<job-id>.json` names over an injected GCS bucket. It creates the
  manifest first and result second with `if_generation_match=0`, accepts a
  failed precondition only for byte-identical canonical content, and pins
  downloads to each observed positive generation;
- control-state schema version 4 retains one immutable job/manifest/result
  bundle plus exact object names and generations under the existing cloud
  singleton lease. Exact replay survives restart as a no-op; content,
  generation, identity, or stored-fingerprint drift fails closed; and
- `automation/job_result_consumer.py` is a thin read-and-record coordinator.
  Consumption is transport acknowledgement only: it does not apply a lifecycle
  transition, emit an action, or grant promotion authority.

P4.4 tests use a fake bucket and temporary SQLite databases. No code constructs
a GCS client or reads credentials, no live object is written or read, no worker
is installed or connected, and P4.3's fixture completion is still not a P4.4
result. P4.O's live client/IAM/installation path is paused; P4.L1 and later
packages own the local replacement. Real manifest generation and result
interpretation remain Phase 5.

P4.L1 adds the first accepted local-first scheduling code without connecting
the previously accepted domains:

- `local_control_plane` is a target control-state writer role, while schema
  version 5 persists exactly one immutable database owner. Existing version
  1-4 databases are treated as cloud-owned and a local open refuses them before
  migration; only a new empty database explicitly created as local can acquire
  local ownership;
- schema version 5 also retains bounded `active`/`completed` wakeups and stable
  due selections keyed by venue, year, and exact persisted `next_check_at`;
- `automation/local_scheduler.py` observes one injected aware clock, acquires
  the existing singleton lease, records one wakeup, selects at most its hard
  bound of due states, and exits. Exact completed replay returns the first
  outcome, while later wakeups cannot reselect an unchanged schedule; and
- an active wakeup left by interruption is durable ambiguity. It blocks later
  wakeups after lease expiry instead of being reclaimed by age.

P4.L1 accepts no effect callback, command, environment expansion, network
client, or production path. Tests use fixtures, fake clocks, and temporary
SQLite only. P4.L2 owns domain composition; later packages retain service,
host-drill, and production-transfer authority.

P4.L2 composes those accepted local domains without adding a live adapter:

- schema version 6 adds an immutable bounded plan record separate from the v5
  active/completed wakeup row. Due selections and their counts are retained
  while the wakeup remains active, and final completion copies the counts only
  after composed work succeeds;
- `automation/local_control_plane.py` holds one local singleton lease across a
  catalog-bounded injected discovery effect, a separately injected strict
  verification effect, verification retention, lifecycle reduction, case and
  pending shadow-output integration, and one repository reminder projection;
- every fake artifact is revalidated against the existing contracts and exact
  venue/year, future-dated or over-limit output fails closed, and the selected
  schedule must advance or clear before completion; and
- exact completed replay calls no effect. Any failure after selection leaves
  the wakeup active and therefore blocks automatic recovery rather than
  repeating work of uncertain outcome.

P4.L2 tests use only sanitized fixtures, fake effects/clocks, and temporary
SQLite. It makes no delivery attempt, submits no job, interprets no job result,
selects or runs no command, and adds no live provider, network, daemon, host,
external-volume, production-state, or deployment path.

P4.L3 adds the credential-free service package without granting installation
or live-effect authority:

- `automation/local_service/` accepts normalized absolute paths and derives
  control SQLite plus bounded health/run artifacts under one private internal
  root. That root and its control child must be private non-symlinked
  directories, and the root must be disjoint from the configured external
  execution volume;
- a local-only mount probe and typed health vocabulary fail before the
  injected wakeup boundary or control database when macOS, Python, repository,
  internal storage, or the external volume is unavailable;
- health is atomically replaced and run history is an atomic fixed-shape ring
  capped at 256 records. Paths, role names, raw exceptions, provider text, and
  credentials are excluded, and corrupt/unsafe record storage blocks work;
- a pure renderer returns a fixed `org.openpapers.local-control` system
  LaunchDaemon with an explicit role account, hourly calendar wakeup,
  restrictive umask, low-impact hints, no environment/shell/keepalive/socket,
  and `/dev/null` launchd streams; and
- rollback is structured data naming only that label and its canonical plist.
  Internal state/logs, repository, external data, and unrelated labels are
  preserved, and no helper installs, removes, or invokes the service manager.

P4.L3 tests use fake clocks, fake effects, fake volume probes, and temporary
private directories. The standalone command has no concrete effect and
returns `effect_unconfigured` without opening control SQLite.

P4.LS adds and exercises only the isolated host-shadow authority:

- a private exact marker is required before the new `--isolated-shadow` mode
  can open SQLite, and a conflicting, unsafe, or absent marker fails closed;
- the concrete effect invokes only `run_scheduler_wakeup` against the supplied
  isolated local-owned state. It cannot call discovery, verification,
  notification, jobs, commands, Prefect, GCS, Codex, or production state;
- the mount probe permits a private execution directory backed by a non-root
  mounted filesystem, avoiding write permission on the entire shared volume;
- one authorized Mac installation uses a root-owned read-only runtime, a
  dedicated non-login role, bounded private records, and the fixed system
  LaunchDaemon; and
- duplicate wakeup, SSH disconnect, reboot, intentional missing-path,
  ambiguous-wakeup preservation/recovery, exact rollback/reinstall, and
  co-resident-service health drills passed on 2026-07-14. The shared volume was
  never unmounted and unrelated labels were never reloaded.

The P4.LS shadow contains no conference state and has no production authority.
P4.LC has now transferred production authority without overlap: a distinct
private marker/configuration/secret boundary restores and validates the legacy
monitor state separately from schema-v6 control, durably claims one daily
monitor/notification run, and then executes the hourly local scheduler. Live
discovery/verifier/case wiring and command/result execution remain later
packages.

P4.O is paused after its external feasibility gate. The acceptable Prefect
Cloud plan rejected the required hybrid process pool before resource creation;
paying for that tier or self-hosting an orchestration stack is not justified.
The [local-first decision](./local-first-decision.md) replaces the transport,
not the safety model:

- a bounded plain-Python process derives due work from durable `next_check_at`
  state under one local singleton lease and then exits;
- a system LaunchDaemon wakes that process at boot and on a coarse interval,
  running it as a dedicated non-administrator role account without a GUI login
  or inbound listener;
- control SQLite and scheduler logs live on internal storage. The external
  volume contains execution data only; OpenPapers observes it and fails closed
  rather than mounting it;
- typed identity, venue/year exclusion, disk gates, supervision, replay
  suppression, and immutable results remain transport-independent; and
- optional GCS backup/export is a side effect, never queue ownership or a
  correctness dependency.

P4.LC took two generation-stable backups, paused Cloud Scheduler and drained
active executions before each local activation, proved local and co-resident
health, and completed timed rollback in 96 seconds by stopping local before
cloud resume. Final activation paused/drained cloud again and refreshed the
recovered generation. The Mac is now the only production writer; the retained
cloud schedule is paused. Both writers must never be active concurrently.

P5.1 adds command selection without adding execution. The pure
`automation.command_registry` revalidates a complete version-2 job and maps
only `scrape_existing` and `validate_candidate` to fixed repository entry
points. It derives literal arguments from closed typed fields, carries an
explicit isolated-staging requirement, and accepts no interpreter, repository
or data path, caller argv/flags, shell text, or environment. Codex jobs remain
outside Phase 5 and fail closed. The registry is not imported by the installed
LaunchDaemon, starts no process, opens no staging or canonical data, and
publishes no result.

P5.2 adds `automation.staging_executor` as a separate, still-unwired execution
boundary. It accepts only the approved existing-scraper job, binds a trusted
absolute interpreter/repository to one private job-fingerprint staging root,
requires that root to be disjoint from an explicitly declared canonical data
root, and supplies an exact non-inherited environment that disables dotenv and
binds scraper data/log output to staging. Strict atomic checkpoints move
through prepared, running, confirmed stopped, process-succeeded, or ambiguous
states. Confirmed failures, timeouts, and cancellations may resume in the same
root; process success suppresses exact replay; running or ambiguous state never
expires automatically. A standard-library no-shell process adapter exists,
but no CLI, scheduler, P4.3, LaunchDaemon, or production caller can reach it.
Tests use only a fake repository/executable, fake launchers/handles/clocks, and
temporary staging/canonical roots, so no scraper or validator was run. P5.3
owns independent validation and manifests.

## Design principles

1. **Discovery is not proof.** An LLM can find candidate facts and URLs, but a
   deterministic verifier must support an executable state transition.
2. **Business state is independent of orchestration.** OpenPapers owns
   conference, case, job, due-work, and recovery state; a scheduler is only a
   bounded wakeup mechanism.
3. **One mutable owner per state domain.** Never overlap cloud and local SQLite
   writers, and never synchronize a live SQLite tree between hosts.
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
| Scheduler | Mac mini (target) | Wake a bounded run and select persisted due work | Decide conference facts or become a second writer |
| Discovery provider | External API called by local control plane | Return structured claims, citations, and uncertainty | Trigger scraping directly |
| Evidence verifier | Local control plane | Fetch/corroborate sources and classify readiness | Execute arbitrary page instructions |
| State transition service | Active single writer | Apply valid idempotent transitions | Accept unverified LLM claims |
| Action router | Local control plane | Select notify, recheck, typed execution, or review | Submit commands outside approved job types |
| Notification service | Local control plane | Immediate transitions and periodic digests | Send duplicate stateless alerts |
| Dashboard export | Active single writer | Emit a derived, public-safe status snapshot as a side effect of an already-authorized commit | Serve queries, hold state, authenticate a consumer, or push updates |
| Approved command registry | Pure local code | Map typed Phase 5 jobs to fixed repository entry points and literal arguments | Accept shell, paths, caller flags, environment, or Codex jobs |
| Staging executor | Mac mini (unwired) | Bind an approved existing scraper to a private canonical-disjoint root and supervise resumable process state | Validate/promote output, inherit ambient environment, or auto-restart ambiguous work |
| Local execution supervisor | Mac mini | Run approved typed jobs with locks and resource gates | Expose a public command endpoint |
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

The accepted target keeps mutable SQLite and immutable artifacts on the Mac's
internal storage. GCS is optional backup/export, not coordination:

```text
control/state.sqlite3       active single writer only; Mac after cutover
snapshots/...               content-addressed immutable source evidence
discoveries/...             immutable structured LLM responses
job-results/<job-id>.json   local executor writes once; local reducer consumes
manifests/<job-id>.json     immutable scrape/validation manifest
dashboard/status.json       optional public-safe export; never a source of truth
```

Requirements:

- prevent overlapping control-plane runs with a lease;
- keep the live SQLite database on internal storage and back it up only from a
  consistent snapshot;
- use stable job IDs and create-only generation preconditions for immutable
  manifest/result objects when an optional object store is used;
- never synchronize or open one live control SQLite tree from two hosts;
- have the active writer record each strict job result exactly once before a later
  reducer interprets it;
- retain enough immutable input to reproduce a transition or diagnosis.

Phase 0's original executable ownership model assigned mutable control state
to the cloud and immutable results to the Mac. P4.L1 adds an explicit local
control role plus a durable per-database owner without weakening the
no-overlap rule; P4.LC made that local role authoritative while retaining the
paused cloud mode for tested rollback. P2.1's
`FileSnapshotStore` proves local content-addressed source snapshot replay but
is not the cloud state store or a GCS adapter. P2.4's
`ControlStateRepository` implements schema-versioned local SQLite control
state, an expiring singleton writer lease, atomic idempotent verification
bundle retention, validated ordered replay, and optimistic conference-state
revision history. P3.1 advances that database to schema version 2 with
deduplicated case current/history/event storage under the same lease. P3.3
advances it to schema version 3 with immutable notification sources/intents
and numbered delivery attempts. P3.4 reuses that version's pending state for
registration-only shadow output and adds no migration. It
deliberately rejects a populated unversioned database and does not migrate or
share the deployed monitor's database. `JobResultRegistry` is a pure executable
model of the job protocol: an identical result replay is accepted as already
seen, while a different result for the same job ID is rejected. P2.5 now
composes retained verification replay with optimistic state updates locally.
P4.1 derives immutable version 2 job IDs without storing a job or result and
uses the same ID for a fake-tested Prefect submission idempotency key. P4.2's
fixture flow returns no versioned job result. P4.3 stores only a private
Mac-local active/completed safety marker keyed by that identity; it is not
uploaded, consumed by cloud state, or validated as the job-result contract.
P4.4 supplies strict manifests/results, create-only GCS-compatible publishing,
exact-generation reads, and the schema-version-4 exactly-once logical
consumption table. P4.L1 advances the repository to schema version 5 with the
immutable owner and due-work journal. P4.L2 advances it additively to version 6
with active plan state and composes only fake discovery/verification plus
accepted lifecycle/case/reminder and pending-output boundaries. P4.L3 packages
an injected one-shot boundary around private internal paths, an external-volume
gate, bounded records, and a credential-free plist; it adds no schema and its
ordinary CLI has no concrete effect. P4.LS installs only the separately marked
P4.L1 scheduler adapter against a new isolated local-owned schema-v6 database.
It contains no conference state and is not wired to P4.L2, P4.3, P4.4, or a
production flow.
Deployed Phase 3 delivery, live
domain effects, job-result production/interpretation, optional backup/export,
and production cutover remain future work.
P3.S's isolated SQLite root is manual canary evidence, not a cloud state store
or deployed migration.

Schema version 6 has no deployed migration or current operator action. Valid
version-1 through version-4 control databases migrate on open only under the
cloud role and preserve verification, conference, case, notification, and
job-result data. A local role refuses those legacy cloud-owned databases;
valid version-5 databases retain their persisted owner while gaining only the
plan table. Before any future durable operator database is opened by
version-6 code, stop overlapping writers and take a backup; rollback after
migration requires restoring that backup because older code must reject, not
downgrade or delete, a newer schema.

Evaluate Firestore or PostgreSQL only after a concrete trigger: multiple
control-plane writers, unavoidable overlapping state updates, a real-time
dashboard/API, multiple workers requiring transactional coordination, or
observed state-loss/recovery problems.

Phase 9 (not yet implemented) adds a narrow, deliberately non-real-time
exception to that separation-of-concerns rule: the existing single
control-plane writer, as the last step of a commit it is already authorized to
make, also overwrites one denormalized `dashboard/status.json` object in a GCS
location dedicated to this purpose and separate from `control/state.sqlite3`,
snapshots, and discoveries. This is a materialized view for an independent,
separately maintained consumer application (for example a browser or tablet
dashboard); it is not additional source-of-truth state, does not create a new
writer role or lease, and does not require Firestore/PostgreSQL, a query API,
or push delivery. Its schema must exclude evidence/crawl URLs, raw
discovery/verification payloads, case free-text notes, credentials, and
internal file paths, admitting only fields safe for broad or public read
access (venue/year, lifecycle state, `next_check_at`, verified milestone
dates, case counts/urgency by blocker, and recent job-result summaries). The
consumer application itself — its frontend, hosting, and any display
hardware — is out of scope for this repository.

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
payload dataclasses cannot contain shell commands, and no executable router
output is persisted, submitted, or executed. P3.4 consumes only
`notify_transition` and `create_or_update_case`: it persists the latter as
stable case observations and registers pending immediate output for transition
or meaningful case events. Recheck, review, and scraper-queue actions remain
inert. Repository reminder projection can also register one grouped pending
digest after excluding claimed slots. P3.S can deliver only its fixed
synthetic digest and cannot select repository output. P4.1 can convert an
explicitly supplied existing-scraper action to an immutable job and submit it
only through an injected boundary; it is not connected to this router or any
production state. P5.1 can resolve an explicitly supplied strict v2 scrape or
validation job to an inert fixed repository-command specification. P5.2 can
consume only the scrape specification through an explicitly called staging
boundary, but it is not connected to this router, scheduler, worker, P4.3
journal, or production state; no repository caller invokes its dormant
subprocess adapter. Production action persistence/submission, independent
validation, and end-to-end execution remain later packages. Job payload
contracts continue to enumerate approved
fields for existing scraper, validation, and Codex-diagnosis jobs and cannot
contain arbitrary shell commands.

Examples:

- accepted list but no PDF: case plus scheduled recheck;
- PDF ready and supported scraper: existing-scraper job;
- transient HTTP failure: retry/backoff, no Codex;
- structural parse/validation failure after retries: Codex candidate;
- multiple venues fail with the same infrastructure symptom: systemic
  incident, circuit breaker, and one notification.

## Historical Prefect queue prototype

P4.1 fixed the original Mac execution prototype as one `process` work pool named
`openpapers-mac` with capability-separated queues:

```text
scrape_existing     -> openpapers-scrape
validate_candidate  -> openpapers-validation
codex_diagnosis     -> openpapers-codex
```

This mapping remains code and contract data, not provisioned Prefect state. It
is retained for compatibility and as evidence for stable typed routing, but it
is not the adopted transport after the P4.O feasibility result. A
version 2 job includes `request_id`, `job_fingerprint`, and `job_id`; the
fingerprint is canonical SHA-256 over every field except the two derived
identity fields, and `job_id` is `job:<full fingerprint>`. It intentionally
contains no wall-clock creation field, so reconstructing an identical logical
request at another time cannot invent a duplicate identity. Prefect records
submission/run time separately.

The queue envelope contains only its schema version, fixed pool, fixed queue,
and validated job. The deployment ID is injected configuration and never
enters the immutable job. Prefect associates that deployment with its work
pool; the adapter also supplies the fixed queue on flow-run creation. Missing
deployment configuration, an incorrectly assigned deployment, a forged
identity, queue drift, an incorrect idempotency key, or an invalid Prefect
response fails closed. P4.1 itself does not suppress a worker from repeating
completed work; P4.3 duplicate-delivery behavior and P4.4 immutable results
close that later acceptance path.

P4.2 adds only the fake receiving boundary. The Mac-side flow revalidates the
envelope and returns stable fields with `status=simulated` and
`reason_code=fixture_only_no_execution`. It cannot select a command or handler
and Prefect result persistence is disabled, so this observation cannot be
mistaken for P4.4's immutable result protocol. Local health output contains no
paths or settings values. The Codex marker signal proves only secure local file
metadata, and the Prefect signal proves only local configuration; neither is a
live authentication or operational canary.

P4.3 wraps a future approved executor behind an injected starter/handle
protocol; neither the queue envelope nor the Prefect flow accepts a callable or
command. Before the fake starter can run, the supervisor revalidates P4.1,
takes the venue/year lock, verifies disk policy, and durably claims the job.
Only a typed confirmed success becomes a local completed marker. Exact replay
returns `duplicate_completed` without calling the starter. Cleanly stopped
failure, cancellation, and timeout permit retry with the same ID; ambiguous
claims block every job for their venue/year, and unconfirmed stops return
`recovery_required` and are never reclaimed by age. These observations and
markers are not P4.4 results.

P4.4 accepts only the full immutable v2 job plus its strict derived manifest
and v2 result; it is not callable from the fixture flow or P4.3 supervisor.
The Mac-owned publisher creates the manifest before the result commit marker
at fixed names with generation-match-zero. The cloud reader binds downloads to
the generations it observed, then the cloud-only repository records that exact
pair under its lease. Deleting/recreating an object changes its generation and
therefore conflicts with an earlier consumption even when bytes match. A
result consumption is not a conference transition and cannot authorize data
promotion.

Prefect remains authoritative for queued work. Because its future worker pulls
runs, an offline Mac receives no envelope and creates no local state; queued
work must remain visible server-side without a local TTL, buffer, resubmission,
or new job ID. This is fake-tested policy, not an operational offline drill.

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

P3.2 itself records no last-delivery state, so replay produces the same due
slot until the clock crosses the next slot. P3.3 can claim that stable slot as
an immutable notification source, persist an in-flight attempt before the
injected fake is called, retain only bounded failure categories, and suppress
delivered/permanent/in-flight replay. P3.4 instead filters sources already
claimed by any retained intent and registers all remaining due slots as one
pending grouped shadow output. It also registers immediate transition and
meaningful case-event output. Case writes and output registrations are
separate transactions, so a failed registration cannot erase a durable case.
P3.S reviewed a three-item synthetic weekly/monthly/dormant message through a
separate database and transport; it does not grant P3.4 delivery authority.
Monthly override, won't-fix control, production notification integration, and
high-volume fatigue evaluation remain later work.

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

- Retained cloud rollback: only its existing provider, Prefect, SMTP, and
  monitor-storage credentials; no access to local control state. Its schedule
  remains paused while local is active.
- Local scheduler account: repository/data access plus only the provider,
  notification, optional backup/export, and later Codex credentials required
  by an accepted package; no Prefect credential and no inbound command
  endpoint.
- Codex subprocess: relevant repository worktree and task-scoped environment;
  no provider, SMTP, deployment, or unrelated secret environment variables.
- Logs/artifacts: redact tokens, passwords, cookies, and authorization headers.
- `.env`, `~/.codex/auth.json`, Prefect blocks, and Secret Manager values must
  never be copied into prompts, fixtures, commits, or notification bodies.
