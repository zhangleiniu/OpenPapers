# P2.8S live discovery/verification canary — 2026-07-14

This record summarizes the one explicitly authorized P2.8S live canary: a
manual `--live` invocation of `automation.run_production_wakeup_canary`
running the exact, unmodified P2.8 composition
(`automation.production_wakeup.run_production_control_wakeup`) with both of
its private test-only injection seams left empty, so it built the real
`ProductionDiscoveryEffect` (Vertex AI Gemini Search Grounding, Application
Default Credentials) and the real `ProductionVerificationEffect`
(`automation.live_fetch.LiveHttpFetcher`, gated by the P2.7 non-shadow
production crawl policy). The command wrote only to a fresh, exactly marked
private root outside the repository and outside the machine's real
production internal root; it created no job, ran no scraper, sent no
notification, and wrote no production or canonical state.

## Method and safety boundary

- Root: a fresh directory under `$SCRAPER_DATA_ROOT/automation/
  production-wakeup-canary/2026-07-14`, stamped on first use with
  `automation/production_wakeup_canary.py`'s
  `.p2-8s-live-canary.v1.json` marker (schema version, purpose, the fixed
  `colt`/2025 venue/year, and the frozen `scheduled_for` timestamp). This
  path is disjoint from the repository, the canonical `data/` tree, and the
  machine's real installed production internal root
  (`/opt/openpapers-shadow/runtime/...`, guarded by its own
  `.production-control.v1.json` marker under a root-owned directory this
  command never touched). The command also refuses outright if a
  production-control or host-shadow marker is ever found inside the
  supplied root.
- Venue/year: preselected in code, not operator-chosen — `colt`/2025. P5.S
  had already proven (181/181 valid archival PDFs) that this exact
  venue/year is reachable through domains the P2.7 production crawl policy
  already approves (`learningtheory.org` for metadata,
  `proceedings.mlr.press` for metadata and bounded PDF processing). No venue
  substitution or retry-until-success occurred.
- Seed: the marked root's brand-new local-owned SQLite database had no
  conference-state row at all, so `automation/production_wakeup_canary.py`
  stored exactly one canonical all-`unknown` schema-v1 row for `colt`/2025
  with `next_check_at` equal to the stamped `scheduled_for`, making it the
  sole due item for the first wakeup. No other seed, synthetic action, or
  forced outcome was used.
- Command: `python -m automation.run_production_wakeup_canary --live
  --canary-root "$SCRAPER_DATA_ROOT/automation/production-wakeup-canary/2026-07-14"`,
  run twice from the repository's own `.venv` (not the installed production
  virtualenv) with Application Default Credentials already configured on
  this host.

## Aggregate result

| Measure | Result |
|---|---:|
| Venue/year exercised | `colt` / 2025 (preselected) |
| Wakeup selections | 1 |
| Verification bundles produced | 9 |
| Verified | 0 |
| Review required | 9 |
| Rejected | 0 |
| Actions/jobs retained | 0 |
| Discovery API calls (first run) | 1 logical call, 2 billed Vertex AI requests (search-grounding + structuring; `GeminiSearchGroundingProvider.attempt_cost == 2`) |
| Live HTTPS verification fetches | 0 |
| Second (replay) run | 0 further discovery or fetch calls; `replayed: true`, zero new selections |
| Jobs/scrapers/notifications/production writes | 0/0/0/0 |

The first invocation printed:

```json
{"outcome": "no_action", "refusal_category": null, "replayed": false,
 "selection_count": 1, "verification_ids": ["verification:c598ee5...", "... 8 more"]}
```

The second invocation, against the same marked root, printed:

```json
{"outcome": "replayed", "refusal_category": null, "replayed": true,
 "selection_count": 0, "verification_ids": []}
```

The private root's `automation/discovery-budget.v1.json` ledger shows
exactly two reserved attempts, both timestamped at the single frozen
`scheduled_for`/`observed_at` moment, confirming the second invocation made
no new provider reservation. The `automation/discovery/artifacts/` tree
contains exactly one cached discovery artifact. No
`automation/verification-snapshots/` directory and no verification health
ledger were ever created, confirming zero live HTTP fetch attempts.

## What the live discovery/verification pair actually found

The real Gemini Search Grounding call returned a normal, well-formed,
citation-bearing discovery result: three candidate milestones (conference
start/end, acceptance notification), a conference-identity claim, two
paper-list claims, a metadata claim, and two proceedings claims (correctly
naming PMLR as the eventual publication venue). Every one of those eight
grounding sources, however, was returned by the Vertex AI grounding API only
as an opaque `https://vertexaisearch.cloud.google.com/grounding-api-redirect/...`
wrapper URL — the API did not additionally expose an already-resolved
`learningtheory.org` or `proceedings.mlr.press` URL alongside it, only a
`domain` label.

The P2.7 production crawl-policy review already denies automated fetching of
that grounding-redirect domain (its published robots policy disallows the
redirect path), and by design "automatic verification must use
already-resolved catalog URLs" rather than follow it. Because every citation
in this real response was still redirect-wrapped, `automation/
production_verification.py`'s deterministic target/domain classification
correctly could not match any of the nine targets to a supported catalog
source shape; every one closed as `overall_status: review_required` with
`reason_code: unsupported_source_shape`, before any crawl-policy fetch claim
or live HTTP request was attempted. No fact was rejected as false — the
verifier made no confident claim in either direction, which is the intended
fail-closed behavior.

This is not a new gap. The earlier P2.S 15-venue live shadow review
([`phase2-live-review-2026-07-13.md`](./phase2-live-review-2026-07-13.md))
already recorded, for this exact venue: "COLT | Exact milestone unsupported;
PMLR proceedings identity rejected | Discovery archival readiness did not
promote | Support ordinal PMLR titles without weakening venue/year identity,"
and noted more broadly that "the bounded HTML verifier is conservative on
live pages... [thirteen] findings were `unsupported_source_shape`." This
P2.8S run reconfirms that same conservative-coverage boundary using the real
*production-capable* automatic discovery/verification pair (not P2.S's
separate shadow adapter) and the real durable local-owned control-state
retention/reduction path, and additionally shows that the specific mechanism
is Vertex AI Search Grounding returning only redirect-wrapped citations for
this query shape, not resolved catalog hostnames.

## Outcome relative to the P2.8S acceptance criteria

- `--live` was required and enforced; the mandatory refusal test (`--canary-root`
  supplied without `--live`) exits 2 before touching the filesystem or
  constructing any effect.
- The preselected, bounded archival venue/year (`colt`/2025) was exercised
  through already-reviewed sources and existing scraper capability.
- The run did not reach an eligible `queue_existing_scraper` action. Per the
  package definition this is retained as evidence and reported here rather
  than forced: no verifier or crawl-policy rule was relaxed, and no
  alternative venue was substituted after seeing the result.
- Exact completed replay against the same marked root made no second live
  call (proven both by the printed `replayed: true` outcome and by the
  unchanged discovery budget ledger/artifact count above).
- No production database, LaunchDaemon, scraper, notification, canonical
  write, promotion, or Codex path was touched. The canary root is fully
  disjoint from this host's actual installed production internal root.
- This package's own acceptance text is explicit that a no-action outcome
  "fails the canary." Because this run did not retain an eligible action,
  P2.8S does not satisfy its own acceptance criterion and is `Review fix
  required`, not `Complete`; P5.5S's automatic verifier/action-source
  prerequisite remains unsatisfied end-to-end, and installation and
  automatic scraper dispatch remain separately authorized P5.5S work,
  unchanged from before this package.

## Rollback and follow-up

There is no production or canonical state to roll back: the marked canary
root is private, was never referenced by the installed service, and can be
deleted at any time without affecting production, matching every earlier
`--live` canary in this codebase. No operator action is required. The
concrete, reviewable next step this run identifies — resolving Vertex AI
Search Grounding's redirect-wrapped citations to their real catalog hostname
before they reach verification, or adding a supported COLT/PMLR source
profile that can be verified through the redirect-resolution the P2.7 policy
already anticipates — is now scoped as its own Phase 2 work package,
`work-packages.md`'s P2.9 (the fix) and P2.9S (a second, independently
authorized live run against this same `colt`/2025 venue/year), rather than
folded into the still-`Planned` Phase 8 venue-family rollout that both this
review and `phase2-live-review-2026-07-13.md` otherwise reference.
