# P2.7 production crawl-policy review — 2026-07-14

This record documents the bounded public research used to author
`automation/config/production_crawl_policy.v1.json`. It is production policy
evidence, not a record of a live automatic-verification run. The review made
only unauthenticated reads of the public `robots.txt`, home, terms, copyright,
or license pages linked below. It used no PDF retrieval, provider call,
credential, access-control bypass, bulk crawl, or verifier execution.

Every entry was reviewed on 2026-07-14 and expires through the P2.7 policy's
90-day maximum review age. A missing robots file means only that no directive
was found at the standard URL; it is not a redistribution grant. All approved
entries use an identifiable User-Agent with the public OpenPapers repository
as contact, one request at a time, manual policy checking of each redirect,
bounded per-run requests, immutable content-addressed caching, no automatic
retry, and mandatory 403/429/`Retry-After`/CAPTCHA stops. Exact delays and
budgets live in the machine-validated artifact.

## Reviewed domains and decisions

| Domain and catalog role | Robots evidence | Terms/copyright evidence | Production decision |
|---|---|---|---|
| `aaai.org` — official | [robots](https://aaai.org/robots.txt): public paths permitted with a 43200-second crawl delay | [website terms](https://aaai.org/about-aaai/aaai-website-terms-of-use-agreement/), [copyright notice](https://aaai.org/about-aaai/copyright-notice/): copying/storage restricted | Approve metadata fetch only, with the published delay; no PDF storage or redistribution |
| `aclanthology.org` — archival | [robots](https://aclanthology.org/robots.txt): not found | [copyright FAQ](https://aclanthology.org/faq/copyright/): ACL materials from 2016 are CC BY 4.0; older/third-party material varies | Approve metadata and bounded PDF processing/internal evidence; no redistribution |
| `aclweb.org` — official | [robots](https://aclweb.org/robots.txt): ten-second delay and private/search exclusions | [public portal](https://www.aclweb.org/portal/): no broader automated-use grant found | Approve metadata only at the published delay |
| `aistats.org` — official | [robots](https://aistats.org/robots.txt): not found | [public site](https://aistats.org/): no automated-use terms found | Approve metadata only; PMLR/OpenReview have separate entries |
| `auai.org` — official | [robots](https://auai.org/robots.txt): not found | [public site](https://www.auai.org/): no automated-use terms found | Approve metadata only; PMLR has a separate entry |
| `cvpr.thecvf.com` — official | [robots](https://cvpr.thecvf.com/robots.txt): public event paths allowed with search/admin/trap exclusions | [public site](https://cvpr.thecvf.com/): copyright retained | Approve metadata only at a conservative ten-second delay |
| `eccv.ecva.net` — official | [robots](https://eccv.ecva.net/robots.txt): public event paths allowed with search/admin/trap exclusions | [public site](https://eccv.ecva.net/): copyright retained | Approve metadata only at a conservative ten-second delay |
| `ecva.net` — archival | [robots](https://ecva.net/robots.txt): explicitly addresses Googlebot only | [public site](https://www.ecva.net/): copyright retained | `review_required`; the OpenPapers agent is not covered |
| `emnlp.org` — official | [robots](https://emnlp.org/robots.txt): not found | [public site](https://emnlp.org/): no automated-use terms found | Approve metadata only; ACL Anthology has a separate entry |
| `iccv.thecvf.com` — official | [robots](https://iccv.thecvf.com/robots.txt): public event paths allowed with search/admin/trap exclusions | [public site](https://iccv.thecvf.com/): copyright retained | Approve metadata only at a conservative ten-second delay |
| `iclr.cc` — official | [robots](https://iclr.cc/robots.txt): public event paths allowed with search/admin/trap exclusions | [public site](https://iclr.cc/): no broader automated-use grant found | Approve metadata only; OpenReview has a separate entry |
| `icml.cc` — official | [robots](https://icml.cc/robots.txt): public event paths allowed with search/admin/trap exclusions | [public site](https://icml.cc/): no broader automated-use grant found | Approve metadata only; PMLR/OpenReview have separate entries |
| `ijcai.org` — official | [robots](https://ijcai.org/robots.txt): ten-second delay and administrative exclusions | [public site](https://www.ijcai.org/): copyright retained | Approve metadata only at the published delay |
| `jmlr.org` — official | [robots](https://jmlr.org/robots.txt): not found | [public site](https://www.jmlr.org/): copyright retained | Approve metadata only |
| `learningtheory.org` — official | [robots](https://learningtheory.org/robots.txt): not found | [public site](https://learningtheory.org/): no automated-use terms found | Approve metadata only; PMLR has a separate entry |
| `naacl.org` — official | [robots](https://naacl.org/robots.txt): not found | [public site](https://naacl.org/): no automated-use terms found | Approve metadata only; ACL Anthology has a separate entry |
| `neurips.cc` — official | [robots](https://neurips.cc/robots.txt): public event paths allowed with search/admin/trap exclusions and named-bot delays | [public site](https://neurips.cc/): copyright retained | Approve metadata only at a conservative ten-second delay |
| `ojs.aaai.org` — archival | [robots](https://ojs.aaai.org/robots.txt): cache path excluded | [AAAI copyright notice](https://aaai.org/about-aaai/copyright-notice/): copying/storage restricted | Approve metadata only; no PDF storage or redistribution |
| `openaccess.thecvf.com` — archival | [robots](https://openaccess.thecvf.com/robots.txt): not found | [public repository](https://openaccess.thecvf.com/): copyright retained by authors/other holders | Approve metadata only; no PDF storage or redistribution grant inferred |
| `openreview.net` — archival | [robots](https://openreview.net/robots.txt): email-bearing query URLs excluded | [terms](https://openreview.net/legal/terms): public site/API access is subject to access controls and provider limits | Approve metadata and bounded PDF processing/internal evidence; prefer API; no redistribution |
| `papers.nips.cc` — archival | [robots](https://papers.nips.cc/robots.txt): not found | [public repository](https://papers.nips.cc/): copyright retained | Approve metadata only |
| `proceedings.mlr.press` — archival | [robots](https://proceedings.mlr.press/robots.txt): not found | [PMLR publication agreement](https://proceedings.mlr.press/pmlr-license-agreement.html): CC BY 4.0 with attribution/source-link requirements | Approve metadata and bounded PDF processing/internal evidence; no redistribution in P2.7 |
| `vertexaisearch.cloud.google.com` — grounding redirect | [robots](https://vertexaisearch.cloud.google.com/robots.txt): grounding redirect paths disallowed | [Google terms](https://policies.google.com/terms): not reached because robots closes the path | `denied`; automatic verification must use already-resolved catalog URLs |

## Operational boundary

`automation/production_verification.py` rejects this review if any catalog
domain is missing, any required review/permission/rate/stop/cache/resume field
is absent, a domain role disagrees with the venue catalog, the per-entry date
differs, the review is stale/future-dated, a closed domain grants permission,
or any entry grants redistribution. The loader then projects only the narrow
runtime fields accepted by `CrawlPolicyGate`.

The P2.7 effect remains uninstalled and fixture/fake-tested. P2.8 owns the
automatic discovery-to-verification-to-state/action composition; P2.8S owns
the separately authorized live canary. This review authorizes neither package.
