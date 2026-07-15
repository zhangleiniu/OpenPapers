# Automation system

This directory is the zero-context entry point for developing OpenPapers'
optional automation control plane. Read this page before changing files under
`automation/`, then follow the links below for the part being changed.

The automation system is intentionally optional. A user must still be able to
install and run the core scrapers without Prefect, GCP, an LLM provider, or
Codex.

## Product goal

The system should reduce the year-round attention required to keep conference
data current. It will:

1. discover conference lifecycle and publication changes with an LLM-backed
   web search;
2. verify cited evidence with deterministic code before changing state;
3. maintain durable conference-year and unresolved-case state;
4. notify the maintainer immediately for important transitions and send
   fatigue-resistant unresolved summaries;
5. dispatch supported scrapes to the MustCite Mac mini;
6. validate results before they can enter the canonical dataset; and
7. optionally ask a local Codex agent to diagnose structural scraper failures
   in an isolated worktree.

LLM output is discovery evidence, not execution authority. Public web content
is untrusted input. Code changes and public dataset publication remain behind
explicit policy gates.

## Current implementation

The following exists in the repository today:

- `automation/conferences.json` registers ICML 2026, AISTATS 2026, and IJCAI
  2026 with deterministic OpenReview, official-HTML, or PMLR detectors.
- `automation/monitor.py` checks those sources, stores hashes/counts in SQLite,
  and saves immutable source snapshots.
- `automation/prefect_flows.py` contains the deployed monitor flow and an
  approval-gated conference update flow.
- `automation/run_monitor_flow.py` is the Cloud Run Job entry point.
- `automation/deployment/` contains the optional monitor image and GCP build
  documentation.
- monitor state is restored from and persisted to GCS because Cloud Run has an
  ephemeral filesystem.
- Prefect records flow/task runs and events; Resend SMTP is loaded through a
  Prefect `EmailServerCredentials` block.

Phase 0's contract foundation also exists, but is not wired into the deployed
monitor flow:

- `automation/schemas/v1/` defines strict contracts for discovery, conference
  and case state, typed jobs, immutable job results, Codex results, venue
  catalogs, and policy configuration;
- `automation/contracts.py`, `automation/configuration.py`,
  `automation/domain.py`, and `automation/scheduling.py` validate those
  artifacts and implement pure state, evidence-driven scheduling, idempotency,
  ownership, and secret-boundary rules;
- `automation/config/` contains the version 1 venue catalog and conservative
  scheduling, reminder, budget, crawl, and publication defaults; and
- fixture-driven tests under `automation/tests/` prove the Phase 0 rejection
  and replay behavior without a live website, Prefect service, or database.

Phase 1 shadow discovery is implemented at the fixture/fake boundary and is
not wired into the deployed monitor flow:

- `automation/discovery.py` provides the provider-neutral request/response
  boundary, strict grounded-evidence normalization, immutable artifacts,
  expiring cache, process-safe daily/per-venue attempt budgets, bounded
  concurrency/retry behavior, and an optional second-provider exception path;
- `automation/providers/gemini.py` implements a two-stage Gemini path through
  Vertex AI: Search Grounding first, then a no-tool schema-constrained
  structuring call over the grounded report and allowlisted sources. The
  second pass consumes grounded excerpt-to-source mappings, cites short source
  IDs, and lets deterministic code resolve exact URIs and source authority;
- `automation/run_discovery.py` is an explicit `--live`, shadow-only command
  for unmetered manual development against any catalog venue; and
- sanitized provider fixtures and fakes test citations, venue/year rejection,
  cache replay, retry budgets, escalation, and the non-live gate.

Authenticated live artifacts now pass the strict contract for all 15 catalog
venues. Manual review confirmed useful dates and paper-list discovery, and also
recorded readiness false positives and NAACL/ACL identity contamination.
Prompt v14 binds claims to grounded excerpts, derives source authority and
ended status deterministically, supports cross-year annual milestones and
continuous publication, and conservatively downgrades unsupported facets.
Phase 1 is `Shadow`; the review matrix is
[`phase1-live-review-2026-07-13.md`](./phase1-live-review-2026-07-13.md).
Deterministic readiness and identity verification remains Phase 2.

Phase 2.1's verifier foundation, P2.1R contract hardening, P2.2 HTML verifier,
P2.3 PDF verifier, P2.4 control-state repository, P2.5 lifecycle reducer, and
the P2.S opt-in live shadow boundary are implemented locally and are not wired
into the deployed monitor flow:

- version 2 verification request/result contracts keep discovery evidence
  separate from deterministic findings, bind each target to its derived kind,
  and reject executable action fields. Compatibility-aware validation can
  replay semantically consistent version 1 fixtures;
- `automation/verification.py` classifies catalog source trust separately from
  crawl permission, and its crawl-policy gate defaults unknown domains to
  review before an injected fetcher can be called;
- cross-artifact validation rejects evidence-free or status-inconsistent
  results, dangling evidence, kind drift, fetched observations without allowed
  policy provenance, and credential-bearing or signed retained URLs;
- the fetch interface requires one HTTPS request with automatic redirects
  disabled, derives one sanitized redirect edge, and never requests its target,
  so Phase 2.2 can policy-check every hop before following it; and
- the snapshot interface has a local content-addressed, immutable,
  secret-safe implementation that retains a replayable redirect edge and is
  proven with fake responses and temporary fixture storage;
- `automation/html_verification.py` follows only sanitized retained redirect
  edges, independently classifies and policy-gates every hop, and stops before
  an unapproved target, loop, or configured redirect limit; and
- a bounded standard-library HTML parser plus explicit source-shape profiles
  verifies venue/year identity, exact candidate dates, plausible distinct list
  counts, required metadata, and current proceedings entries. Sanitized
  fixtures reproduce the EMNLP future-index, NAACL/ACL identity, and IJCAI
  no-PDF false positives; and
- `automation/pdf_verification.py` deterministically selects a bounded subset
  of exact cited PDF URLs, requires separate `pdf_fetch_for_processing` and
  `store_internal_copy` permissions for every redirect hop, and verifies final
  HTTP status, actual/minimum size, optional Content-Length consistency, and
  the `%PDF-` signature; and
- `automation/control_state.py` owns a versioned SQLite schema for the cloud
  control plane, refuses populated unversioned databases, excludes overlapping
  writers with an expiring singleton lease, atomically retains strict
  discovery/request/result bundles, and stores optimistic conference-state
  revisions with immutable history; and
- `automation/lifecycle.py` independently reclassifies retained positive
  evidence, monotonically promotes facets and milestones, applies the existing
  evidence-backed transition reducer, computes `next_check_at` from the
  retained verification time, and returns stable typed action intents as data.
  `automation/control_plane.py` is the thin lease/revision-aware composition
  boundary that persists that pure result; and
- `automation/live_fetch.py` adds public-address-only DNS validation, pinned
  HTTPS with original-hostname TLS verification, bounded reads, no automatic
  redirects, safe headers, and conservative crawl delays/stops.
  `automation/run_verification_shadow.py` requires `--live` plus explicit,
  non-overlapping discovery/output roots and uses a separate reviewed
  shadow-only crawl policy.

The deterministic content-verifier modules still contain no transport. P2.S
injects the opt-in adapter, retains results in an isolated local root, and can
exercise P2.4/P2.5 without production authority. Its reviewed 15-venue 2026
sample produced 28 strict targets: 2 verified milestones, 22 rejections, 4
review-required results, 8 invalid PDF signatures, and no scraper-queue
intent. The record is
[`phase2-live-review-2026-07-13.md`](./phase2-live-review-2026-07-13.md).
P2.S actions remain inert data: it persists or dispatches no intent, delivers
no notification, and runs no scraper. P5.5 can now retain a strict scraper
action produced by the separate fake-only local composition, but no live or
installed Phase 2 path supplies one and no installed dispatcher consumes it.
PDF evidence retention grants no redistribution authority. Phase 2 is
`Shadow`, not deployed or implemented; live source-profile coverage remains
conservative.

P2.6 has completed a fixture-only, production-capable `DiscoveryEffect`
(`automation/production_discovery.py`) that requires the existing attempt
budget ledger plus a durable, versioned, cross-process cooldown and
systemic-circuit health ledger before any automatic provider call. The guard
refuses before provider construction or budget reservation when a same-venue
cooldown or a distinct-venue systemic circuit is open, and only a closed set
of provider/transport/output-shape failure categories can open the circuit;
venue-specific content problems cannot. Nothing is installed or connected to
the deployed monitor. P2.7 has completed the separate fixture-only
`VerificationEffect` (`automation/production_verification.py`), dated
non-shadow review of all 23 catalog/redirect domains, strict stale/missing
review closure, bounded deterministic P2.2/P2.3 composition, and durable
venue/year/source fetch cooldown. `ecva.net` remains review-required, the
grounding redirect is denied by robots, and no redistribution is granted.
P2.8 has completed the uninstalled automatic composition
(`automation/production_wakeup.py`): it always builds exactly one
`ProductionDiscoveryEffect` and one `ProductionVerificationEffect` from
explicit private configuration and hands them, unmodified, to the existing
`run_local_control_wakeup` boundary, so a caller can configure storage and
Gemini identity but never substitute a different effect, action, or job.
Fixture/fake tests drive the real production pair to a genuine authoritative
`pdf_status=ready` facet that P5.5 retention persists as exactly one
execution job, prove exact zero-call replay, and prove that invalid evidence,
an open discovery circuit, and budget exhaustion all persist no action.
Nothing is installed or connected to the deployed monitor. P2.8S ran its
separately authorized isolated live evidence
(`automation/production_wakeup_canary.py`,
`automation/run_production_wakeup_canary.py`): one 2026-07-14 `--live` run
against a private marked root made a real Gemini Search Grounding call and
retained real verification evidence for the preselected `colt`/2025
venue/year, but every grounded citation was an unresolved
`vertexaisearch.cloud.google.com` redirect wrapper rather than an
already-resolved catalog URL, so the existing deterministic verifier
correctly left every target `review_required` and retained no action. A
replay of the same marked root made zero further live calls. This
reconfirms, on the real production-capable pair, the same COLT source-shape
gap the earlier P2.S 15-venue review already recorded; see
[`p2-8s-live-canary-review-2026-07-14.md`](./p2-8s-live-canary-review-2026-07-14.md).
Because P2.8S's own package text treats a no-action outcome as a failed
canary, it is `Review fix required`, not `Complete`.
P2.9 is now complete at the fixture/fake boundary. Because the installed
`google-genai` response model has no alternate resolved-source field, a pure
closed mapping resolves only the reviewed `colt`/2025 domain labels to the
already-known COLT and PMLR URLs while preserving the opaque wrapper as
non-fetchable provenance. A bounded PMLR profile verifies venue/year identity,
100--500 distinct entries, same-volume PDF links, and three policy-gated PDF
signatures. The sanitized redirect-only fixture reaches a promotable
`pdf_status=ready` result without any grounding-domain fetch. The P2.7 policy
is unchanged and still denies `vertexaisearch.cloud.google.com`; no
redistribution was granted. [`work-packages.md`](./work-packages.md) now marks
P2.9S `Ready` for the separately authorized second live run against the same
`colt`/2025 venue/year. P5.5S remains blocked until that run reaches a genuine
authoritative `pdf_status=ready` facet.

Phase 3.1 persistent unresolved cases, P3.2 reminder/digest policy, P3.3's
fake-only notification delivery boundary, P3.4's persistent shadow-output
integration, and the P3.S isolated delivery canary are implemented locally and
are not wired into the deployed monitor:

- `automation/cases.py` derives one stable case per venue/year/blocker,
  distinguishes repeated checks from meaningful changes, retains new evidence,
  reactivates a dormant case only for new evidence, and implements resolve,
  snooze, ignore, and explicit reactivate controls as pure state changes; and
- control-state schema version 2 adds lease-protected current case rows,
  immutable revisions, and immutable observation/control events. Valid schema
  version 1 databases migrate atomically, repeated event IDs are idempotent,
  conflicting reuse fails closed, and unresolved-only listing excludes
  terminal cases by default; and
- `automation/reminders.py` deterministically ages validated case copies from
  `last_meaningful_change_at`, selects stable weekly, monthly, or dormant
  cadence slots, releases expired snoozes, excludes closed cases, and builds
  one immutable in-memory digest grouped by urgency; and
- `automation/notifications.py` builds strict, stable immediate or grouped
  digest intents from explicitly supplied data, redacts credential-shaped
  message text, classifies bounded transport failures, and coordinates only an
  injected transport after a durable in-flight claim. Control-state schema
  version 3 adds immutable intent/source records and durable numbered-attempt
  history, so delivered, permanent, or unresolved in-flight replay cannot make
  a duplicate call; and
- `automation/notification_integration.py` converts P2.5 transition and case
  actions into stable case events and immediate intents, queries unresolved
  repository cases for due reminders, filters already claimed slots, and
  registers one grouped digest for all remaining due cases. Registration
  retains `pending` intent/source records with zero delivery attempts.
- `automation/resend_notifications.py` is a one-request Resend HTTPS adapter
  with provider idempotency and bounded failure classification, while
  `automation/run_notification_canary.py` requires `--live`, an explicit
  isolated root, and the SHA-256 identity of one approved test recipient. It
  can build and deliver only a fixed synthetic weekly/monthly/dormant digest;
  it cannot select or deliver a retained P3.4 intent.

P3.3 tests use only fake transports and temporary SQLite databases. P3.4 calls
no transport at all: its temporary-database tests persist only pending shadow
output and prove that exact event replay cannot create a second intent. Case
events commit separately from notification registration, so registration
failure cannot erase a case and replay can recover the missing output. P3.S
made one authorized provider-accepted delivery to the approved test recipient
using three non-sensitive synthetic events. Its replay, failure, fatigue, and
rollback record is
[`phase3-delivery-review-2026-07-13.md`](./phase3-delivery-review-2026-07-13.md).
Phase 3 is `Shadow`: P3.S is manual and isolated, and no Phase 3 component is
scheduled, deployed, connected to P3.4 output, or authorized to act on
production state.

P4.1's execution-queue foundation, P4.2's fake-only Mac package, P4.3's local
safety supervisor, P4.4's immutable result protocol, P4.L1's local
ownership/due-work foundation, and P4.L2's fixture-only control composition are
implemented locally. P4.L3's headless service package and P4.LS's isolated
host-shadow boundary are also implemented. None is wired into the deployed
monitor:

- version 2 typed jobs derive a full SHA-256 job ID from their request,
  venue/year, type, requester, input artifacts, and closed payload while
  retaining compatibility validation for version 1 artifacts;
- `automation/job_queue.py` defines the inert `openpapers-mac` Prefect process
  work-pool blueprint and separate scrape, validation, and Codex queues. A
  strict queue envelope rejects pool, queue, job-type, identity, secret, and
  arbitrary-field drift before submission; and
- an explicitly supplied P2.5 existing-scraper action can build a scrape job,
  while an injected Prefect deployment-client adapter uses the job ID as the
  flow-run idempotency key. Tests use only a fake client; P4.1 creates no
  Prefect resource or flow run and supplies no worker or command executor; and
- `automation/mac_worker/` revalidates that envelope in a thin Prefect flow and
  returns only a non-persisted `simulated` fixture observation. Secret-safe
  local health checks, an isolated Prefect dependency, and an inert `launchd`
  template/runbook are fixture-tested without installing or starting a worker;
  and
- `automation/mac_worker/safety.py` adds a private Mac-owned claim/completion
  journal, process-safe venue/year locks, a disk-space gate, and timeout and
  cancellation supervision over an injected fake handle. A confirmed local
  completion suppresses exact replay, while any ambiguous active claim blocks
  another job for that venue/year. Its fixed offline policy leaves undelivered
  work in the Prefect pull queue and creates no local queue copy.
- `automation/job_results.py` adds strict job-manifest v1 and job-result v2
  semantics, fixed create-only GCS-compatible object names/writes, and
  exact-generation reads over an injected bucket. Control-state schema version
  4 records one lease-protected logical consumption, and
  `automation/job_result_consumer.py` is the thin composition boundary. Tests
  use a fake bucket and temporary database only.
- control-state schema version 5 binds each database to one immutable cloud or
  local control owner. Existing version 1-4 databases remain cloud-owned;
  only a new empty database explicitly created for the local role can become
  local-owned; and
- `automation/local_scheduler.py` acquires the local singleton lease for one
  bounded fake-clock wakeup, selects persisted `next_check_at <= now` state,
  suppresses exact and cross-wakeup duplicates, and leaves an interrupted
  wakeup ambiguous rather than expiring it automatically. It returns inert
  due-work data and accepts no effect callback or command.
- control-state schema version 6 retains bounded plan counts separately while
  a selected wakeup remains active. `automation/local_control_plane.py` holds
  that same local lease across injected fake discovery and verification,
  strict retention/lifecycle reduction, case and pending shadow-output
  integration, and one due reminder projection. It completes the wakeup only
  after every selected schedule advances or clears; and
- `automation/local_service/` derives control SQLite and bounded health/run
  records below one private internal root, checks an explicit external
  execution volume before an injected effect, renders a credential-free
  low-impact system LaunchDaemon, and exposes an exact OpenPapers-only rollback
  scope. Its ordinary standalone command has no concrete effect and fails
  closed; and
- P4.LS adds an exact private `isolated_shadow` marker, a concrete
  `--isolated-shadow` mode that invokes only the bounded P4.L1 scheduler, and a
  renderer for that fixed mode. One authorized Mac installation uses a
  root-owned read-only runtime, a dedicated non-login role, isolated local-owned
  SQLite, and a private directory on the external volume.
- P4.LC adds a separate production marker/configuration/secret boundary, a
  durable daily claim around the existing six-source deterministic monitor and
  TLS SMTP notifications, and the same hourly local scheduler against separate
  schema-v6 control state. Two generation-stable backups, zero-active cloud
  gates, initial/final local health, and a 96-second timed rollback passed; and
- P5.1 adds `automation/command_registry.py`, a pure approved-command registry
  that maps strict version-2 scrape and validation jobs to the fixed `main.py`
  and `postprocessing/validate_year.py` repository entry points. It returns an
  inert job-bound specification requiring isolated staging and rejects Codex,
  arbitrary shell, paths, flags, and environment expansion; and
- P5.2 adds `automation/staging_executor.py`, which binds only a strict
  existing-scraper job to a trusted repository/interpreter and a private,
  canonical-disjoint per-job data root. Strict atomic checkpoints support
  same-root resume, exact process-success suppression, timeout/cancellation,
  and ambiguous-stop closure over an injected process boundary; and
- P5.3 adds `automation/staging_validation.py`, which requires that exact
  process-success checkpoint, safely inventories the staged tree, builds a
  strict candidate manifest, and independently produces a versioned validation
  report plus validation-job manifest for the applicable completeness/count/
  metadata/duplicate/PDF checks; and
- P5.4 adds `automation/execution_pipeline.py`, which holds the existing P4.3
  lock, disk gate, and exact claim across injected P5.2 execution, P5.3
  validation, and P4.4 publication. Closed readiness routes and explicit
  transient/operational/structural classes preserve same-root retry,
  ambiguity closure, partial-output isolation, and byte-identical recovery
  from manifest-only publication failure; and
- P5.S adds `automation/execution_shadow.py` plus an explicit manual `--live`
  command. It binds P5.4 to a private marked root, a macOS child sandbox that
  denies repository/canonical writes, and a create-only local result store.
  One COLT 2025 archival job passed confirmed timeout recovery, 181/181
  independent validation, immutable publication, exact duplicate suppression,
  coexistence checks, and scoped rollback without canonical change.

The sanitized host-shadow, backup, cutover, rollback, and final-runtime
evidence is recorded in
[`phase4-local-cutover-review-2026-07-14.md`](./phase4-local-cutover-review-2026-07-14.md).

Phase 4 is `Implemented`. These packages establish contracts and fake-tested
execution-safety/result behavior plus one operational local scheduler and
deterministic baseline monitor. P5.1 implements scraper/validator command
selection, and P5.2 implements the isolated existing-scraper staging/process
boundary. P5.3 implements independent fixture-only staged validation and
manifest generation. P5.4 implements fixture-only guarded composition,
immutable result construction, and readiness/failure routing. P5.S has
completed one authorized manual shadow; later work still owns automatic
runtime integration.
P4.L2 composes only fixture effects and pending notification records; every
recheck, review, and scrape action remains inert typed data. No command is
selected by the installed runtime or run, no delivery attempt occurs, no live
immutable result is published or consumed, and no new GCS result resource,
worker, or Prefect queue resource is installed or connected. P5.2's concrete
subprocess adapter has no CLI or caller and was exercised only through fake
launchers and temporary fixture roots; no scraper or validator has run. The
production daemon preserves only the existing deterministic
monitor/notification baseline plus local due-work selection; it cannot resolve
or execute a typed job or scraper. No production caller invokes P5.3.
P5.4 still has no installed caller. Only the separate P5.S manual boundary has
called it with a real process; that boundary is not imported by the production
daemon, publishes only to private local files, and cannot promote canonical
data or change conference state. Its sanitized record is
[`phase5-existing-scraper-shadow-review-2026-07-14.md`](./phase5-existing-scraper-shadow-review-2026-07-14.md).

P5.5 has completed additive local-owned action/job persistence (control-state
schema version 7) and one-job dispatch/reconciliation through injected fake
effects; it installs no caller and runs no scraper. The later installed
automatic shadow (P5.5S) remains blocked until a production deterministic
verifier/action-source gate can prove that execution originated from
retained, crawl-policy-allowed evidence rather than manual or synthetic
input.

P4.O is `Paused`. Its operator feasibility gate found that the acceptable
Prefect Cloud plan cannot create the required hybrid process pool; the failed
apply created none of the planned pool, queues, or deployments. The accepted
[local-first decision](./local-first-decision.md) replaces that transport with
a bounded local scheduler and system LaunchDaemon. P4.L1 and P4.L2 implement
the isolated scheduler and fixture-only domain composition, while P4.L3
implements the credential-free service package. P4.LS installed its
scheduler-only shadow and completed duplicate, SSH-disconnect, reboot,
missing-volume, ambiguous-recovery, bounded-record, scoped-rollback, and
co-resident health drills. P4.LC then completed the separately authorized
no-overlap cutover and timed rollback. The local LaunchDaemon is authoritative;
the Cloud Scheduler job is paused and retained only for rollback. P5.1 is
complete at the pure selection boundary, P5.2 is complete at the isolated
fake-tested staging/process boundary, P5.3 is complete at the fixture-only
validation/manifest boundary, P5.4 is complete at the fixture-only guarded
composition/result-routing boundary, P5.S has completed one real manual
shadow, and P5.5 has completed fake-only durable action/job persistence and
bounded dispatch. Phase 5 is `Shadow`, not automatically connected or
implemented.

The following does **not** exist yet:

- scheduled or deployed LLM discovery;
- a scheduled or deployed HTML/PDF verifier and persistent reducer/router;
- scheduled or deployed case/action/reminder integration or notification
  delivery;
- automated routing from discovery to a scrape job;
- live discovery/verification, Phase 3 case delivery, or typed job execution
  effects in the installed OpenPapers LaunchDaemon;
- an installed or automatically connected scraper/validator execution adapter;
- a connected automatic result/readiness route from staged output;
- a Codex execution adapter;
- automatic promotion into the canonical dataset or MustCite deployment.

Never describe a roadmap item as deployed merely because its interface or
schema has been added.

## Target topology

```text
system LaunchDaemon / local due scheduler
          |
          v
LLM discovery with citations
          |
          v
deterministic evidence verification
          |
          v
conference state machine and action policy
       /      \
      v        v
notifications  typed local action
                    |
                    v
             MustCite Mac mini
             scraper -> validator
                    |
             structural failure only
                    v
             Codex in a worktree
                    |
                    v
             patch/report for review
```

The Mac is the sole production writer after P4.LC. The retained Cloud Scheduler
job is paused; rollback may resume it only after the local label is stopped.
Conversely, local activation requires cloud to be paused with zero active
executions. Immutable results remain separate from mutable control state.

## Required reading

Read in this order for a new automation task:

1. repository-level `AGENTS.md`;
2. this page;
3. [architecture.md](./architecture.md) for invariants and component
   boundaries;
4. [local-first-decision.md](./local-first-decision.md) for the accepted Phase
   4 scheduler, ownership, migration, and cost decision;
5. [roadmap.md](./roadmap.md) for implemented versus planned work and phase
   acceptance criteria;
6. [work-packages.md](./work-packages.md) to select one thread-sized task and
   its dependency, scope, and completion boundary;
7. [development.md](./development.md) for commands, change workflow, and
   handoff requirements;
8. `docs/automation.md` and `automation/deployment/README.md` for the current
   production implementation.

For a venue-specific scrape, also read `docs/<venue>.md`, `docs/pipeline.md`,
and `docs/validation.md`.

## Current manual shadow operations and next slice

Install the optional automation dependencies separately. The first command
below proves the remote-call gate; the second permits an unmetered manual
development call:

```bash
python -m pip install -r automation/requirements.txt
python -m automation.run_discovery --venue icml
python -m automation.run_discovery --live --venue icml --year 2026
```

The live command requires a GCP project setting and Application Default
Credentials. It does not read or write the production-oriented budget ledger,
and an explicit `--venue` may select any venue in the catalog. Its default
artifact root is
`$SCRAPER_DATA_ROOT/automation/discovery`, or `data/automation/discovery` when
that variable is absent. It records no credential values or raw SDK transport
objects.

The initial Phase 1 review across every registered venue family is complete.
Repeat observations remain useful for measuring drift and missed sources.
Do not weaken citation or venue/year validation merely to make a provider
sample pass. P2.2 now provides fixture-backed deterministic candidate-date and
HTML-readiness verification, while P2.3 provides fixture-backed PDF permission,
status, size, signature, and sampling verification. P2.4 can retain those
artifacts locally for replay, and P2.5 can promote authoritative findings into
local conference state and inert action intents. The explicitly authorized
P2.S review is complete; it observed all 15 venues through isolated roots and
confirmed that the known readiness false positives do not create queue
intents. P3.1 can persist explicitly supplied case observations and human
controls under the local lease, and P3.2 can project them through
clock-controlled weekly/monthly/dormant policy into grouped digest data.
P3.3 can turn explicitly supplied event/digest data into a strict redacted
intent and exercise durable delivery through an injected transport. P3.4 can
consume P2.5 transition/case action data, persist cases independently, filter
repository reminder slots, and retain pending immediate/digest shadow output
without claiming a delivery attempt. P3.S adds one concrete transport only
behind a manual synthetic-only canary; it cannot read P3.4 output. None is
connected to the deployed monitor. No Phase 2 command can execute an action or
write production state, and no Phase 3 path can deliver a production event.
P4.L2 can replay those accepted domains under one isolated local lease using
fake effects and temporary SQLite; it still cannot call a live provider,
deliver a notification, submit a job, or execute an action.
P4.L3 wraps an injected wakeup behind private internal paths, a missing-volume
gate, bounded secret-safe records, and a rendered system plist; its ordinary
repository command deliberately has no concrete effect. P4.LS's separately
marked scheduler-only mode is installed against isolated state and has passed
host lifecycle drills, but it has no live domain effect or production
authority.

Keep Phase 1 additive. It may report what a verified later phase could do, but
must not create a job, write lifecycle state, invoke a scraper, or promote data.

## Decision summary

These decisions are current unless deliberately amended in the architecture
document:

- use LLM search as broad discovery, then verify its citations;
- keep deterministic detectors as cheap validators/fallbacks;
- keep SQLite and GCS initially, using a single-writer model rather than
  prematurely adding Firestore;
- write Mac job results as immutable, idempotent objects;
- decay unresolved reminders from weekly to monthly to dormant;
- enforce provider and Codex budgets, cooldowns, concurrency limits, and a
  systemic-failure circuit breaker;
- enforce per-domain crawl policy before automated fetching;
- distinguish permission to fetch a PDF from permission to redistribute it;
- run Codex only on the Mac mini, with least privilege and an isolated git
  worktree;
- do not automatically merge Codex changes or deploy them to production;
- keep any dashboard/monitoring UI as a separate, independently maintained
  application outside this repository; this repository's Phase 9 only exports
  a narrow, public-safe, pull-based status snapshot for that consumer to read.

## Sources of truth

- Canonical dataset coverage and counts: `statistics.md`.
- Core scraper completion contract: repository `AGENTS.md`.
- Current automation behavior: executable code and its tests.
- Target automation behavior: this directory.
- Current thread-sized execution boundary:
  [`work-packages.md`](./work-packages.md).
- Runtime credentials and secrets: their external secret stores, never docs or
  version control.
- External deployment state: GCP and Prefect themselves. Repository docs may
  describe the expected topology but must not be treated as proof that a
  resource is currently healthy.
