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

P2.1R has closed the initial verifier-contract review findings, P2.2 and P2.3
have completed deterministic HTML and PDF verification, and P2.4 has completed
the independent persistent-state slice. P2.5 is the only current `Ready`
package; it may consume retained verified evidence through the reducer but
must return typed actions as data without executing them.

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

## Phase 2 packages — verification and lifecycle state

Phase gate: no verified result may affect state or actions until P2.5. Live
network observations occur only in P2.S and remain isolated from production.

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P2.1 | Complete | Phase 1 | Verifier contracts, source trust, crawl gate, one-request fetch boundary, immutable local snapshots, and P2.1R semantic hardening. |
| P2.2 | Complete | P2.1R | Deterministic redirect, venue/year identity, HTML list-count, metadata, and proceedings-index verification. Sanitized EMNLP, NAACL/ACL, and IJCAI regressions; no PDF verification, state write, action, or live run. |
| P2.3 | Complete | P2.1R | PDF permission, URL/status, size, `%PDF-` signature, and deterministic sampling. No HTML identity logic, state write, redistribution grant, or live run. |
| P2.4 | Complete | P2.2, P2.3 | Single-writer SQLite repository, schema/migration, evidence history, lease, idempotent consumption, and replay. Temporary databases in tests; no deployed migration. |
| P2.5 | Ready | P2.4 | Verified evidence to state reducer, milestone scheduling, and typed action routing. Actions are returned as data and never executed. Replay all catalog venue/lifecycle shapes with fixtures. |
| P2.S | Planned | P2.5 | Explicitly authorized 15-venue shadow review using approved crawl policy and isolated state/artifact roots. Record agreement and false positives; perform no job, scraper, notification, or production-state write. |

Phase 2 closes only when the roadmap acceptance criteria pass and P2.S has a
reviewed record. Change the phase to `Shadow` before `Implemented` when live
observation exists but production action remains disabled.

## Phase 3 packages — cases and notifications

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P3.1 | Planned | Phase 2 gate | Persistent unresolved-case domain and repository with deduplication plus resolve, snooze, ignore, and reactivate controls. No email delivery. |
| P3.2 | Planned | P3.1 | Clock-controlled weekly, monthly, and dormant reminder policy plus grouped digest generation. No transport adapter. |
| P3.3 | Planned | P3.2 | Immediate/digest delivery boundary with idempotency, retry classification, redaction, and fake transport tests. No real email without explicit authorization. |
| P3.4 | Planned | P3.3 | Integrate transitions, cases, reminders, and notification intents; prove one event creates at most one notification. Shadow output before any live delivery. |
| P3.S | Planned | P3.4 | Separately authorized delivery canary and fatigue review using non-sensitive test events. Record results and rollback without changing case semantics. |

Case creation and message delivery remain separate effects. A transport failure
must not erase or duplicate the durable case.

## Phase 4 packages — Mac mini execution plane

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P4.1 | Planned | Phase 3 gate | Prefect work-pool and typed queue protocol, cloud submission boundary, and immutable job identity. No Mac installation. |
| P4.2 | Planned | P4.1 | Mac worker package, health checks, and `launchd` runbook using fake jobs. No scraper or Codex execution. |
| P4.3 | Planned | P4.2 | Venue/year locks, disk checks, timeout, cancellation, duplicate-delivery behavior, and offline queue semantics. |
| P4.4 | Planned | P4.3 | Immutable GCS job-result/manifest publishing and cloud result consumer with generation preconditions and exactly-once logical consumption. |
| P4.O | Planned | P4.4 | Explicit Mac/Prefect/GCS installation and reboot, SSH-disconnect, offline-worker, and recovery drills. External resources are changed only in this operator-authorized package. |

Code implementation, Mac installation, cloud configuration, and operational
drills are distinct tasks even when performed by the same maintainer.

## Phase 5 packages — execute existing scrapers

| ID | Status | Depends on | Objective and completion boundary |
|---|---|---|---|
| P5.1 | Planned | Phase 4 gate | Approved command registry mapping typed jobs to fixed repository entry points. Reject arbitrary shell, paths, flags, and environment expansion. |
| P5.2 | Planned | P5.1 | Staging executor for existing scrapers with isolated data roots, checkpoints, resume, timeout, and cancellation. No canonical promotion. |
| P5.3 | Planned | P5.2 | Independent validation and manifest generation for counts, metadata, duplicate IDs, PDF existence/size/signature, and applicable completeness levels. |
| P5.4 | Planned | P5.3 | Readiness routing and end-to-end job to staging to validation to immutable result, with transient/operational/structural failure classification. |
| P5.S | Planned | P5.4 | Approved shadow/canary executions of already-supported scrapers. Invalid or partial output remains outside canonical data. |

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
