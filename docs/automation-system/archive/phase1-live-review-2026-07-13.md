# Phase 1 live review — 2026-07-13

This record summarizes the first live Gemini shadow sample across every venue
family in the version 1 catalog. The manual development command was unmetered,
explicitly invoked with `--live --force`, and wrote discovery artifacts only.
No result changed conference state, scheduling, jobs, scrapers, or deployment.

## Method

- Ran one contract-valid 2026 observation for all 15 catalog venues.
- Inspected normalized statuses, typed claims, candidate milestones,
  uncertainties, source classes, and grounding references.
- Resolved selected grounding redirects and fetched selected registered public
  pages to test the most consequential readiness claims.
- Recorded semantic false positives rather than weakening or bypassing local
  validation.

Artifacts are stored below the configured discovery root under
`artifacts/gemini-search-grounding/<venue>/` and are intentionally outside the
repository.

## Review matrix

| Venue | Prompt | Contract result | Manual review | Follow-up |
|---|---:|---|---|---|
| NeurIPS | v13 | Accepted | Plausible future dates; readiness stayed unknown | Verify official dates in Phase 2 |
| ICML | v13 | Accepted | Ended date derived correctly; readiness claims remain candidates | Verify OpenReview/PMLR list, metadata, and PDFs |
| ICLR | v13 | Accepted | Dates plausible; released/partial/provisional facets not independently verified | Verify OpenReview records and PDFs |
| AAAI | v14 | Accepted | Cross-year 2025 notification accepted for the 2026 event; readiness stayed unknown | Verify archival release separately |
| CVPR | v13 | Accepted | Dates and release candidates found | Verify CVF list, metadata, PDFs, and proceedings |
| ICCV | v13 | Accepted | Correct negative-year behavior: no official 2026 event or milestones | Preserve odd-year lifecycle handling |
| COLT | v13 | Accepted | PMLR volume/list/PDF candidates found | Verify volume 336 and PDF signatures |
| UAI | v13 | Accepted | Future dates found; readiness stayed unknown | Verify after release milestones |
| AISTATS | v13 | Accepted | Ended date derived correctly; partial readiness remains unverified | Verify OpenReview/PMLR sources |
| JMLR | v14 | Accepted | Continuous publication correctly produced no conference milestones; 2026 papers, abstracts, and PDF candidates found | Verify sampled paper records and PDFs |
| ECCV | v13 | Accepted | Future dates found; paper list reported unavailable and other facets unknown | Recheck near verified milestones |
| ACL | v13 | Accepted | Dates and ACL Anthology readiness candidates found | Verify anthology volumes, counts, and PDFs |
| EMNLP | v13 | Accepted | **False positive:** future companion-volume promise was labeled archival readiness | Verifier must require a currently public proceedings index |
| NAACL | v13 | Accepted | **Identity contamination:** ACL 2026 collaboration was treated as a distinct NAACL 2026 lifecycle | Verifier must require venue-specific identity evidence |
| IJCAI | v12 | Accepted | Accepted-papers page is real and contains about 990 titles with authors and abstracts; **false positive:** `pdf_status=ready` despite zero PDF links on that page | Verify actual list completeness and keep PDF unknown until links pass checks |

## Findings

The discovery provider is useful for locating event dates, accepted-paper
pages, and likely archival sources. Grounding excerpt/source binding and local
venue/year/source validation eliminate several structural failure modes.

The sample also confirms why discovery cannot update state directly:

- future publication language can be mistaken for current readiness;
- submission-system capabilities can be mistaken for public metadata or PDFs;
- a related conference can contaminate venue identity; and
- a cited page can support a paper list without supporting PDF availability.

Phase 1 is therefore ready for `Shadow`, not `Implemented`. Phase 2 must fetch
the cited resource and deterministically verify venue identity, list entries,
required metadata, PDF links/signatures, and proceedings indexes before any
state transition or action.
