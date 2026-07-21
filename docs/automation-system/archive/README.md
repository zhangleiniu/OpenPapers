# Archive: the deterministic-verification automation design

Everything in this directory documents an earlier design for the OpenPapers
automation control plane that has been abandoned. It is kept for history, not
as current guidance — do not read it to understand how automation works today
or what to build next. Start from `docs/automation-system/README.md` instead.

## What was tried

The earlier design tried to fully automate conference monitoring by chaining
deterministic components: LLM-backed discovery (Gemini Search Grounding),
then deterministic HTML/PDF verification of every citation shape the
provider could return, then a typed action/job dispatch protocol, then Mac
execution, with a bounded Codex repair step planned as a last resort.
`roadmap.md` and `work-packages.md` describe that design's phases (P0-P6) and
their acceptance criteria in detail; `architecture.md` and `development.md`
describe its component boundaries and workflow. `original-README.md` was that
design's own zero-context entry point (renamed here only to avoid colliding
with this file).

## Why it was abandoned

The deterministic-verification layer (Phase 2, `P2.6`-`P2.11` in
`work-packages.md`) got stuck in a whack-a-mole loop across three separately
authorized live canaries against one fixed test venue (`colt`/2025):

- `p2-8s-live-canary-review-2026-07-14.md`: every grounding citation came
  back as an unresolved `vertexaisearch.cloud.google.com` redirect wrapper,
  a shape the deterministic parser didn't recognize.
- `p2-9s-live-canary-review-2026-07-14.md`: after fixing that, the next real
  run cited only the official conference page with no PMLR domain label, a
  second unrecognized shape.
- `p2-10s-live-canary-review-2026-07-15.md`: after fixing that too, the next
  real run's own `pdf` claim cited the official page alongside one unrelated
  secondary citation, a third unrecognized shape.

Each fix was narrowly scoped to the exact shape just observed, by design (the
architecture deliberately never let unverified LLM output become authority to
act). But live provider output kept producing new shapes faster than the
verifier could be taught to recognize them, and there was no evidence the
loop would ever terminate.

## What replaced it

Rather than continuing to chase provider output shapes deterministically, the
project now trusts a coding agent (Codex/Claude Code) to judge readiness and
execute directly, the same way the maintainer already does this manually. See
`docs/automation-system/README.md` for the current design. The concrete
consequence: the deterministic verification, typed job/action dispatch, and
cloud/Prefect job-submission code this archive describes was deleted from
`automation/`; only the venue catalog, contracts, SQLite control-state
storage, discovery request/response plumbing, and the local LaunchDaemon
scheduler survived into the new design, largely unchanged.

`local-first-decision.md` (one level up, not archived) documents a separate,
still-current decision — abandoning Prefect Cloud in favor of a local Mac
LaunchDaemon — and remains accurate.
