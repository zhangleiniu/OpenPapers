# P2.9S second live canary review — 2026-07-14

This record summarizes the separately authorized P2.9S live canary: one fresh
manual `--live` invocation of `automation.run_production_wakeup_canary` after
the P2.9 grounding-redirect fix, followed by one exact replay against the same
marked root. The command exercised the fixed `colt`/2025 venue/year through the
unmodified P2.8 composition and real P2.6/P2.7 effects. It retained real
discovery and verification evidence but did not reach `pdf_status=ready` or
retain an execution action.

## Method and safety boundary

- The run used a new private root below the ignored scraper data root, separate
  from the P2.8S root, repository, canonical dataset, and installed production
  internal root. The existing canary boundary stamped its fixed
  `.p2-8s-live-canary.v1.json` marker for historical compatibility and refused
  production/host-shadow markers. No private path is retained here.
- Venue/year remained preselected in code as `colt`/2025. No alternate venue,
  second fresh root, synthetic action, or retry-until-success was used.
- The CLI loaded the project identity from ignored local configuration and
  used existing Application Default Credentials. No project identifier,
  credential, raw provider text, redirect token, or private path was copied
  into this record.
- Before the live request, the P2.9 and canary focused suites (83 tests), full
  automation suite (427 tests), compilation, generated-statistics check, and
  mandatory missing-`--live` refusal all passed.

## Aggregate result

| Measure | Result |
|---|---:|
| Venue/year | `colt` / 2025 |
| Wakeup selections | 1 |
| Verification bundles | 10 |
| Verified / review required / rejected | 4 / 5 / 1 |
| Verification kinds | 4 conference milestones, 2 paper lists, 2 proceedings, 2 source identities |
| PDF verification targets | 0 |
| Live verification requests | 6, all to `learningtheory.org` |
| Grounding-wrapper requests | 0 |
| Discovery call cost | 1 logical call / 2 reserved provider attempts |
| Discovery artifacts | 1 |
| Retained actions/jobs | 0 |
| Dispatch attempts / scraper runs / notifications / production writes | 0 / 0 / 0 / 0 |
| Exact replay | `replayed: true`; zero new selections |

The first command returned `outcome: no_action`, one selection, ten bounded
verification IDs, and no retained job. The second invocation against the same
root returned `outcome: replayed` with zero selections and no retained job.
After replay, the discovery budget still contained exactly two attempts and
the artifact tree still contained exactly one discovery artifact, confirming
that replay did not reserve or create a second discovery observation.

The six allowed HTML observations all targeted `learningtheory.org`; no
observation targeted `vertexaisearch.cloud.google.com`. One immutable HTML
object and replayable manifests were retained. Conference start/end milestones
and two source-identity targets verified. Unsupported source shapes left the
remaining paper-list/proceedings/milestone targets closed. The lifecycle
summary advanced to `conference_ended`, while every readiness facet — including
`pdf_status` — remained `unknown`, with `human_review_required` retained.

## Why P2.9 did not reach the live action path

P2.9's fixture proved an exact, intentionally closed rule: when the provider
returns a grounding wrapper whose `domain` label is
`proceedings.mlr.press`, resolve it to the already-known COLT 2025 PMLR volume,
verify the volume identity/count, extract same-volume links, and apply the
existing PDF permission/signature sampler. The P2.9S provider response had a
different source set. Its seven grounding sources contained one
`learningtheory.org` label and six unrelated labels; none was
`proceedings.mlr.press`. P2.9 therefore correctly resolved only the official
COLT page and did not invent a PMLR citation or PDF claim. The normalized
discovery contained no PDF claim, so no PDF verification request existed and
no scraper action could be retained.

This does not show a crawl-policy or PMLR availability failure. A bounded audit
of the already-retained official COLT HTML found one ordinary link to
`proceedings.mlr.press`. That gives the next deterministic iteration a safer
corroboration path: after official COLT/year identity is verified, extract and
independently policy-gate an exact PMLR volume link present in that retained
official page, rather than depending on a nondeterministic provider domain
label or inferring a URL from venue/year alone. This observation grants no new
request or redistribution permission.

## Acceptance and status

- The command required explicit `--live`; the refusal check exited before
  filesystem or provider construction.
- The fresh isolated root and fixed `colt`/2025 selection met the P2.9S scope.
- The Google grounding wrapper remained denied and was never fetched. All six
  live verification requests used the already-reviewed official COLT domain.
- Exact completed replay made no new selection and left provider reservation,
  discovery artifact, and job counts unchanged.
- No production database, LaunchDaemon, scraper, dispatcher, canonical data,
  statistics, notification, promotion, deployment, or Codex path was touched.
- The run did **not** reach a genuine authoritative `pdf_status=ready` facet or
  retain `queue_existing_scraper`. Under P2.9S's explicit rule, this is a
  failed canary outcome retained as evidence. P2.9S is therefore `Review fix
  required`, P2.8S's finding remains open, and P5.5S remains blocked.

P2.10/P2.10S in [`work-packages.md`](./work-packages.md) define the next
fixture-first fix and its separately authorized live proof. They may not
weaken verification, contact the grounding wrapper, or infer PMLR authority
without a verified official-page link.

## Rollback and recovery

There is no production or canonical state to roll back. The marked root is
ignored, isolated, uninstalled, and contains no dispatch-capable caller. It may
be retained for private audit or deleted without affecting the installed
service. No operator action or schema migration is required. A future live run
requires separate authorization and a fresh root; this completed evidence root
must not be reused to bypass exact replay.
