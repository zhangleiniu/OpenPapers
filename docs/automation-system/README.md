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

Phase 2.1's initial verifier foundation is committed locally and is also not
wired into the deployed monitor flow:

- strict verification request/result contracts keep discovery evidence
  separate from deterministic findings and reject executable action fields;
- `automation/verification.py` classifies catalog source trust separately from
  crawl permission, and its crawl-policy gate defaults unknown domains to
  review before an injected fetcher can be called;
- the fetch interface requires one HTTPS request with automatic redirects
  disabled so Phase 2.2 can policy-check every redirect before following it;
  and
- the snapshot interface has a local content-addressed, immutable,
  secret-safe implementation proven with fake responses and temporary fixture
  storage.

This foundation does not inspect HTML or PDFs and makes no live request.
Redirect, venue/year identity, list, metadata, and proceedings verification is
Phase 2.2; PDF permission, status, size, signature, and sampling is Phase 2.3.
Review found semantic contract and retained-redirect/URL-redaction gaps in the
initial P2.1 implementation. P2.1R in `work-packages.md` must close them before
P2.2 or P2.3 begins; no consumer should yet treat a verification result as
authoritative.

The following does **not** exist yet:

- scheduled or deployed LLM discovery;
- HTML/PDF evidence validators and a persistent state reducer wired to the
  runtime;
- unresolved cases and reminder decay;
- automated routing from discovery to a scrape job;
- a Mac mini Prefect worker;
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

## Current Phase 1 operation and next slice

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
sample pass. Phase 2.2/2.3, not Phase 1 or the P2.1 interface foundation, will
deterministically verify supported candidate dates and readiness. Phase 2.5
may then promote verified findings into conference state for `next_check_at`
computation.

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
