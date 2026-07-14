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
Actions remain inert data: no intent is persisted or dispatched, no
notification is delivered, and no scraper runs. PDF evidence retention grants
no redistribution authority. Phase 2 is `Shadow`, not deployed or
implemented; live source-profile coverage remains conservative.

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

P4.1's execution-queue foundation, P4.2's fake-only Mac package, and P4.3's
local safety supervisor are implemented locally and are not wired into the
deployed monitor:

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

Phase 4 remains `Planned`. P4.1-P4.3 establish contracts and fake-tested local
safety behavior, not an operational execution plane. No command is selected or
run, no immutable result is published, and no worker or Prefect resource is
installed or connected.

The following does **not** exist yet:

- scheduled or deployed LLM discovery;
- a scheduled or deployed HTML/PDF verifier and persistent reducer/router;
- scheduled or deployed case/action/reminder integration or notification
  delivery;
- automated routing from discovery to a scrape job;
- provisioned P4 work pools, queues, deployments, or submission wiring;
- an installed or connected Mac mini Prefect worker;
- a Codex execution adapter;
- automatic promotion into the canonical dataset or MustCite deployment.

Never describe a roadmap item as deployed merely because its interface or
schema has been added.

## Target topology

```text
Cloud Scheduler / Prefect
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
notifications  Prefect work queue
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

The cloud control plane is the sole mutable writer of conference and case
state. The Mac mini writes immutable job-result objects and reports Prefect
task state; it must not edit the cloud-owned SQLite database.

## Required reading

Read in this order for a new automation task:

1. repository-level `AGENTS.md`;
2. this page;
3. [architecture.md](./architecture.md) for invariants and component
   boundaries;
4. [roadmap.md](./roadmap.md) for implemented versus planned work and phase
   acceptance criteria;
5. [work-packages.md](./work-packages.md) to select one thread-sized task and
   its dependency, scope, and completion boundary;
6. [development.md](./development.md) for commands, change workflow, and
   handoff requirements;
7. `docs/automation.md` and `automation/deployment/README.md` for the current
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
- do not automatically merge Codex changes or deploy them to production.

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
