# Phase 2 live verification review — 2026-07-13

This record summarizes the first P2.S live deterministic-verification sample
across all 15 venues in the version 1 catalog. The command was explicitly
invoked with `--live`, used the retained Phase 1 discovery artifacts, and wrote
only to a new isolated shadow root. It created no job, ran no scraper, sent no
notification, and wrote no production state.

## Method and safety boundary

- Reviewed `robots.txt` for every catalog official/archival domain plus the
  Google grounding-redirect domain. Twelve returned an applicable allow
  policy, ten had no robots file, and `ecva.net` remained `review_required`
  after an unsafe redirect response. `eccv.ecva.net` was independently
  reviewed and approved for the bounded sample.
- Used the separate `automation/config/p2s_shadow_policy.v1.json`, with one
  request at a time, a one-second minimum delay plus bounded jitter, explicit
  per-domain budgets, 403/429/CAPTCHA stops, and the repository URL as the
  User-Agent contact. No redistribution permission was granted.
- Selected at most one catalog-bounded lifecycle/identity target and one
  highest-priority readiness target per venue. Provider grounding metadata
  limited candidate selection, but every actual redirect hop was independently
  DNS/SSRF-checked, catalog-classified, and crawl-policy-gated.
- Connected only to DNS results that were entirely global addresses, pinned
  the HTTPS connection to the reviewed address, and still verified TLS against
  the original hostname. Redirects were never followed automatically.
- Retained 56 immutable source snapshots, 28 strict verification artifacts,
  and an isolated versioned SQLite state database. Raw live content remains in
  ignored runtime storage; this sanitized review is the committed record.
- Replayed the completed command against the same root. It returned the first
  summary without a new network observation or a changed timestamp.

A missing robots file was recorded only as the absence of a published robots
directive, not as redistribution permission or blanket approval for a future
production caller. This one-off review was limited to public, unauthenticated
GETs already in the repository's source domain catalog; it did not bypass
terms, paywalls, CAPTCHAs, or access controls. Production scheduling requires a
separate current policy review.

## Aggregate result

| Measure | Result |
|---|---:|
| Catalog venues | 15 |
| Verification targets | 28 |
| Verified | 2 |
| Rejected | 22 |
| Review required | 4 |
| PDF samples rejected for invalid signature | 8 |
| Queue-existing-scraper intents | 0 |
| Jobs/scrapers/notifications/production writes | 0/0/0/0 |

The two verified observations were exact future conference milestones for
EMNLP and IJCAI. They produced inert transition-notice previews and isolated
`scheduled` state only. No paper-list, metadata, proceedings, or PDF facet was
promoted by the live sample.

Seven discovery results that reported `partial` or `ready` PDF status were
deterministically rejected: ICML, ICLR, CVPR, AISTATS, JMLR, ACL, and IJCAI.
In each case the exact cited URL resolved to content without a `%PDF-`
signature. ECCV also had a PDF-shaped claim while its aggregate discovery
status remained unknown; that sample was rejected for the same reason. This is
the desired fail-closed behavior: a page describing or linking papers is not
itself a sampled PDF.

## Review matrix

| Venue | Live deterministic result | Comparison with discovery | Follow-up |
|---|---|---|---|
| NeurIPS | Exact milestone rejected; proceedings shape needs review | No readiness promotion; future date was not present in the exact cited page shape | Add a reviewed proceedings profile during venue rollout |
| ICML | Exact milestone unsupported; PDF citation failed signature | Discovery `pdf_status=partial` did not promote | Verify an exact OpenReview/PMLR PDF URL when discovery can cite one |
| ICLR | Exact milestone unsupported; PDF citation failed signature | Discovery `pdf_status=partial` did not promote | Add the reviewed OpenReview list/PDF source shape |
| AAAI | Milestone remained review-required | Discovery reported the event ended, but the selected page shape was insufficient | Review the official cross-year event-date page shape |
| CVPR | Exact milestone unsupported; PDF citation failed signature | Discovery `pdf_status=partial` did not promote | Add CVF list and exact PDF-link extraction profiles |
| ICCV | Venue/year identity rejected | Agrees with the discovery result that there is no official 2026 ICCV event | Preserve odd-year identity protection |
| COLT | Exact milestone unsupported; PMLR proceedings identity rejected | Discovery archival readiness did not promote | Support ordinal PMLR titles without weakening venue/year identity |
| UAI | Official-page identity rejected for both sampled targets | Discovery readiness stayed unknown; no state promoted | Review the current AUAI title/heading shape |
| AISTATS | Exact milestone unsupported; PDF citation failed signature | Discovery `pdf_status=partial` did not promote | Add exact OpenReview/PMLR list and PDF evidence |
| JMLR | Year identity rejected; PDF citation failed signature | Discovery `pdf_status=ready` did not promote; continuous venue stayed `unknown` | Add volume/year identity and exact paper-PDF sampling for continuous publication |
| ECCV | Exact milestone unsupported; PDF citation failed signature | Discovery PDF status was unknown and stayed unknown | Keep `ecva.net` closed until its crawl review is resolved |
| ACL | Exact milestone unsupported; PDF citation failed signature | Discovery `pdf_status=ready` did not promote | Require an exact ACL Anthology PDF URL, not an event/index page |
| EMNLP | Exact milestone verified; proceedings remained review-required | The Phase 1 archival-readiness false positive did not promote | Add a current ACL Anthology proceedings-index profile when it exists |
| NAACL | Exact milestone unsupported; proceedings fetch exceeded the 5 MiB HTML bound | The Phase 1 ACL/NAACL contamination did not promote any state | Add a bounded venue-specific Anthology profile or API-shaped source; do not raise the cap blindly |
| IJCAI | Exact milestone verified; PDF citation failed signature | Discovery `pdf_status=ready` was correctly rejected | Keep PDF unknown until exact PDF links pass signature sampling |

## Findings and limitations

The live sample validates the safety boundary more strongly than source-shape
coverage. Provider readiness false positives did not become facets or queue
intents, unknown redirects remained closed, the oversized NAACL page stopped at
the byte limit, and JMLR did not acquire an annual-conference transition.

The sample also shows that the bounded HTML verifier is conservative on live
pages. Thirteen findings were `unsupported_source_shape`, four were explicit
venue/year identity mismatches, and one was a JMLR year mismatch. Several are
expected false negatives rather than evidence that the source fact is false.
Source-specific profiles should be added during the Phase 8 venue-family
rollout with saved sanitized fixtures; the P2.S gate does not justify a
permissive generic parser or a larger global byte limit.

Phase 2 therefore moves to `Shadow`, not `Implemented`. The local opt-in live
runtime is neither scheduled nor deployed, its policy is explicitly
shadow-only, and all action output remains inert. Production persistence,
cases/notifications, queue submission, scraper execution, and rollout accuracy
remain later packages.
