# P2.10S third live canary review — 2026-07-15

This record summarizes the separately authorized P2.10S live canary: one
fresh manual `--live` invocation of `automation.run_production_wakeup_canary`
against a brand-new marked root, after the P2.10 official-page-link-derivation
fix, followed by one exact replay against the same root. The command exercised
the fixed `colt`/2025 venue/year through the unmodified P2.8 composition and
the real P2.6/P2.7 effects. It retained real discovery and verification
evidence but again did not reach `pdf_status=ready` or retain an execution
action, this time because the real response itself contained an explicit
`pdf`-kind claim shaped differently from every fixture P2.9/P2.10 cover.

## Method and safety boundary

- The run used a new private root below the ignored scraper data root
  (`.../automation/production-wakeup-canary/p2-10s-2026-07-15`), disjoint from
  the P2.8S root, the P2.9S root, the repository, the canonical dataset, and
  the machine's installed production internal root. `automation/
  production_wakeup_canary.py`'s existing `prepare_canary_root` stamped the
  same fixed `.p2-8s-live-canary.v1.json` marker for historical compatibility
  (schema version, purpose, the fixed `colt`/2025 venue/year, and the frozen
  `scheduled_for` timestamp) and refused outright if a production-control or
  host-shadow marker were ever found inside the supplied root. No private path
  is retained here.
- Venue/year remained preselected in code as `colt`/2025 — unchanged from
  P2.8S and P2.9S. No alternate venue, second fresh root, synthetic action, or
  retry-until-success was used.
- The CLI loaded the project identity from ignored local configuration and
  used existing Application Default Credentials. No project identifier,
  credential, raw provider text, redirect token, or private path was copied
  into this record.
- Before the live request, the P2.9/P2.10/canary focused suites (86 tests),
  the full automation suite (433 tests), compilation, the generated-statistics
  check, and the mandatory missing-`--live` refusal all passed. `automation/
  production_wakeup_canary.py` and `automation/run_production_wakeup_canary.py`
  are unchanged from P2.8S; this run reuses that infrastructure exactly, as
  P2.9S did.

## Aggregate result

| Measure | Result |
|---|---:|
| Venue/year | `colt` / 2025 |
| Wakeup selections | 1 |
| Verification bundles | 10 |
| Verified / review required / rejected | 3 / 6 / 1 |
| Verification kinds | 5 conference milestones, 1 source identity, 1 metadata, 1 paper list, 1 proceedings, 1 pdf |
| PDF verification targets | 1 (zero valid samples; see below) |
| Live verification requests | 6, all to `learningtheory.org` |
| Grounding-wrapper / PMLR requests | 0 |
| Discovery call cost | 1 logical call / 2 reserved provider attempts |
| Discovery artifacts | 1 |
| Retained actions/jobs | 0 |
| Dispatch attempts / scraper runs / notifications / production writes | 0 / 0 / 0 / 0 |
| Exact replay | `replayed: true`; zero new selections |

The first command returned `outcome: no_action`, one selection, ten bounded
verification IDs, and no retained job. The second invocation against the same
root returned `outcome: replayed` with zero selections and no retained job.
After replay, the discovery budget still contained exactly two attempts, the
artifact tree still contained exactly one discovery artifact, and the
verification-health ledger still contained exactly one `learningtheory.org`
source entry in `eligible` state, confirming replay reserved or fetched
nothing new.

The six allowed HTML observations all targeted `learningtheory.org`; no
observation targeted `vertexaisearch.cloud.google.com` or
`proceedings.mlr.press`. Those six requests retained one immutable HTML object
(the page content was identical across the different verification targets
that cited it) plus six replayable manifests. Two conference milestones and
the source-identity target verified; one milestone (the claimed acceptance-
notification date) was independently checked against the fetched page and
**rejected** as unsupported — a correct, strict outcome, not a defect. The
remaining metadata/paper-list/proceedings/two-milestone targets stayed
`review_required` with `unsupported_source_shape`, matching P2.9S's pattern
for citations the deterministic verifier does not resolve. The lifecycle
summary advanced to `conference_ended`, every readiness facet (including
`pdf_status`) remained `unknown`, and `human_review_required` was retained.

## Why P2.10 did not reach the live action path

The real Vertex AI response contained six grounding sources: one
`learningtheory.org` label and five unrelated labels (`riken.jp`,
`paperdigest.org`, `wikicfp.com`, `github.com`, `dblp.org`) — no
`proceedings.mlr.press` label, the same absence P2.9S observed. Unlike P2.9S,
however, this response did not leave the PDF claim to be synthesized from a
`paper_list`/`proceedings` citation. The provider itself emitted its own
`claim_kind: "pdf"` claim, whose `evidence_urls` cited **two** URLs: the
already-resolved official COLT page and one of the unrelated
grounding-redirect wrappers (the `paperdigest.org`-labeled citation).

This is a third distinct citation shape, different from both prior canaries:

- P2.8S: every citation was an unresolved grounding-redirect wrapper with no
  already-resolved catalog URL at all.
- P2.9S: no provider-declared `pdf` claim existed; only `paper_list`/
  `proceedings` claims cited the official page, which is exactly the shape
  P2.10's discovery-time candidate synthesis and verification-time derivation
  were built to handle.
- P2.10S (this run): the provider declared its own `pdf` claim, and that
  claim's cited-URL set is not a singleton — it names the official page
  alongside one additional, unrelated, unresolved citation.

`automation/providers/gemini.py`'s `_add_known_pmlr_pdf_candidate` and
`_add_known_official_page_pdf_candidate` both intentionally skip
discovery-time synthesis whenever the provider already supplied its own `pdf`
claim, so neither added a second, competing candidate here — correct
behavior, since the provider's own claim already exists. But
`automation/production_verification.py`'s official-page derivation only
recognizes a `pdf` target whose **sole** cited URL is the reviewed official
page (`len(target_urls) == 1`); a two-URL claim does not qualify as either the
known PMLR-listing shape or the known official-page shape. The deterministic
verifier therefore treated the raw claim as an ordinary bounded PDF sample:
the official page URL is HTML, not a catalog-recognized PDF-shaped source,
and the second citation remained an unresolved, non-catalog-domain wrapper, so
`build_pdf_sample_plan` selected zero fetchable URLs. The result was
`pdf_sample_incomplete` with `pdf_sampled_count: 0`, `pdf_valid_count: 0`, and
**zero live PDF fetch attempts** — the intended fail-closed outcome. No PDF
request, catalog classification, or crawl-policy decision was skipped or
weakened to reach this result; the verifier correctly declined to guess which
of the two cited URLs, if either, was authoritative.

This does not show a defect in P2.9 or P2.10's accepted logic for the shapes
they were built against — both remain sound and unchanged — but it does show
that Vertex AI Search Grounding's real output space has at least one more
shape neither package's fixtures anticipated: a provider-declared `pdf` claim
whose evidence set already mixes the reviewed official page with an
unresolved secondary citation.

## Acceptance and status

- The command required explicit `--live`; the refusal check exited before
  filesystem or provider construction (confirmed by a fresh check before this
  run).
- The fresh isolated root and fixed `colt`/2025 selection met the P2.10S
  scope; no alternate venue or root was substituted after seeing the result.
- The Google grounding wrapper and `proceedings.mlr.press` both remained
  unfetched. All six live verification requests used the already-reviewed
  official COLT domain.
- Exact completed replay made no new selection and left provider reservation,
  discovery artifact, verification-health, and job counts unchanged.
- No production database, LaunchDaemon, scraper, dispatcher, canonical data,
  statistics, notification, promotion, deployment, or Codex path was touched.
- The run did **not** reach a genuine authoritative `pdf_status=ready` facet or
  retain `queue_existing_scraper`. Under P2.10S's explicit rule — identical to
  P2.8S's and P2.9S's — this is a failed canary outcome retained as evidence,
  not forced. P2.10S is therefore `Review fix required`, not `Complete`; the
  P2.8S and P2.9S review findings remain open, and P5.5S's live action-source
  prerequisite remains unproven.

The concrete, reviewable next step this run identifies is scoped as
`work-packages.md`'s P2.11 (a fixture-only fix generalizing P2.10's
sole-cited-URL derivation to recognize a `pdf` claim that cites the reviewed
official page alongside only non-authoritative secondary citations) and P2.11S
(its own later, separately authorized live proof against this same
`colt`/2025 venue/year).

## Rollback and recovery

There is no production or canonical state to roll back. The marked root is
ignored, isolated, uninstalled, and contains no dispatch-capable caller. It
may be retained for private audit or deleted without affecting the installed
service. No operator action or schema migration is required. A future live
run requires separate authorization and a fresh root; this completed evidence
root must not be reused to bypass exact replay.
