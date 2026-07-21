# Phase 5 existing-scraper shadow review — 2026-07-14

This is the durable, sanitized review record for P5.S, the first real use of
the guarded existing-scraper pipeline. It contains no credential, private host
path, account name, source response body, canonical content, or notification
configuration. The accepted staging tree, raw process log, checkpoints,
candidate artifacts, and immutable local results remain in a private ignored
root on the reviewed Mac.

## Boundary reviewed

P5.1 through P5.4 established strict typed jobs, fixed repository command
selection, isolated staging and supervision, independent staged validation,
local venue/year locking and disk gates, immutable results, and closed
readiness/failure routing. Before P5.S, every execution test used fake
processes and temporary fixture data; no scraper or validator had run through
that boundary.

P5.S adds only a manual `--live` shadow command. It is not imported by the
local LaunchDaemon, scheduler, local control plane, retained cloud monitor, or
MustCite. The command accepts a strict existing-scraper job, explicit trusted
repository/interpreter/canonical/shadow roots, and typed count/runtime values.
It cannot accept shell text, arbitrary argv/environment, a promotion target,
statistics generation, deployment, or Codex work.

The shadow root is repository-external and private. Its state, staging,
artifact, immutable-result, sandbox, and review children are distinct. The
child scraper ran with P5.2's exact non-inherited environment and a fixed
macOS sandbox profile explicitly denying writes to the primary checkout and
canonical dataset root. After the canary, review hardening versioned the
profile additively and changed the reusable boundary to deny all filesystem
writes except below the exact shadow root. Unit tests proved an allowed staging
write and denied ordinary outside-shadow, repository, and canonical writes
through the same concrete launcher. The accepted create-only canary profile is
preserved rather than overwritten.

## Preflight and selection

All repository and host gates passed before the first process start:

- the tracked tree began at the accepted P5.4 commit with no tracked changes;
- 359 repository tests, full core/automation compilation, generated-statistics
  consistency, and diff checks passed;
- the Mac reported sufficient internal and external free space for the bounded
  run;
- the OpenPapers production label was loaded with no overlapping P5 process;
- Cloud Scheduler was live-queried as paused and Cloud Run had zero active
  executions;
- all five expected co-resident MustCite labels were loaded;
- the current process identity, trusted interpreter/repository metadata,
  private root modes, and pairwise staging/artifact/result separation passed;
  and
- a canonical content-tree fingerprint and root metadata were retained before
  execution.

COLT 2025 was selected at archival completeness with PDFs required and exact
expected count 181. Its scraper has a fixed PMLR `v291` mapping, the canonical
coverage report records 181 complete papers and PDFs, and it is substantially
smaller than the monitored ICML/AISTATS/IJCAI years while still exercising
metadata requests, PDF downloads, resume, and every independent validator.

## Fail-closed findings and recovery drill

The first live-gated call refused before scraper/network start because P5.4
had required repository and canonical roots to be pairwise disjoint, although
the supported repository layout uses `repository/data`. The new root contained
only its exact marker, sandbox profile, and empty private children. P5.4 was
aligned with P5.2: repository/canonical containment is now the sole permitted
containment pair; state, staging, artifacts, and results remain disjoint from
both and from each other. A regression reaches the disk gate with this normal
layout without starting or publishing work.

The next confirmed attempt exited nonzero because resolving the virtualenv
symlink selected the base interpreter and therefore omitted an installed
parser dependency. P5.2's no-symlink trusted-executable rule was preserved.
The canary used a mode-0700 regular copy of that interpreter inside the same
ignored virtualenv, verified under an empty environment before retry, and
removed it during scoped rollback. The stopped failure was classified
`transient/process_failed`, cleared its claim, and published no result.

The same immutable job then ran with a one-second bound. The supervisor
returned `transient/process_timed_out`, confirmed the whole process group
stopped, cleared the active claim, and permitted same-root retry. Four private
staging/checkpoint/log entries remained; artifact and result counts remained
zero. No P5 process remained, all partial output was isolated, and the
canonical content-tree fingerprint was unchanged.

## Real result and independent validation

The exact same job ID and staging root resumed with the normal bound. Attempt
3 completed through the real COLT scraper and network path. P5.3 captured a
183-file, 103,031,585-byte candidate containing 181 PDFs plus metadata and
BibTeX. Its retained strict report was `valid` with:

- paper count: 181;
- valid PDF count: 181;
- expected count: 181; and
- issue count: 0.

P5.4 returned `ready/validated_ready` and published one validation manifest
followed by one job result through the private create-only local store. The
store uses fixed P4.4 object names and generation `1`, accepts only
byte-identical replay, and never constructs a cloud client.

An independent invocation of `postprocessing/validate_year.py` against the
staging data root returned archival COLT 2025, 181 papers, and an empty issue
map. A separate review process reopened the candidate inventory, report,
validation manifest, and local result; recomputed their identities and
fingerprints; and passed P5.3/P4.4 cross-artifact validation.

## Exact replay, coexistence, and rollback

Before exact replay, hashes of the entire staging, artifact, and result trees
plus process-log size/mtime were retained. Replay returned
`skipped/duplicate_completed` with no publisher or process claim. Every hash
and the process-log metadata was unchanged, proving no scraper, network,
validation, or result write repeated.

The canonical content-tree fingerprint matched before the first attempt, after
each failure, after success, after independent validation, after exact replay,
and after rollback. No P5 process or active claim remained. The OpenPapers
label, live cloud paused/zero-active state, and 5/5 co-resident service gate
remained healthy.

Scoped rollback created and removed only one empty disposable directory below
the P5.S review subtree, then removed only the temporary ignored regular
interpreter copy. The accepted shadow root and its create-only evidence were
retained. No canonical file, production plist/runtime/state, cloud resource,
MustCite service, shared volume mount, credential, or unrelated label was
changed.

## Validation and conclusion

Pre-execution and final validation each passed the complete 359-test repository
suite. The final 51-test focused P5.S/P5.4/P5.2/P5.3/P4.4 set, hardened
10-test P5.S command/sandbox set, full core/automation compilation,
generated-statistics check, Markdown target check, and diff check also passed.

P5.S passes its manual shadow gate. Phase 5 is `Shadow`, not `Implemented`:
one existing scraper has run safely through the reviewed boundary, but no
verified action is persisted or automatically dispatched, no installed
runtime calls P5.4, no canonical promotion exists, and no result affects
conference state. P6/Codex repair, automatic execution, promotion, statistics
write, MustCite deployment, and Cloud Scheduler enablement remain outside this
review.
