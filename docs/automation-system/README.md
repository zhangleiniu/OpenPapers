# Automation system

This directory is the zero-context entry point for OpenPapers' optional
automation control plane. Read this page before changing `automation/`, then
[`architecture.md`](./architecture.md) for the safety boundaries and
[`roadmap.md`](./roadmap.md) for implementation status.

The core scrapers remain independently installable and runnable. Prefect,
GCP, an LLM provider, email, and a coding-agent CLI are never core
dependencies.

## Product goal

The maintainer already handles a newly published conference by giving Codex or
Claude Code a venue and year. The agent inspects the repository and the web,
decides whether papers are available, reuses or repairs a scraper, runs it,
and explains the outcome. The automation system should decide *when that
existing workflow is worth running* and invoke it in a contained environment;
it should not reproduce the agent's reasoning with venue-specific verification
code.

For each configured `venue/year`, the intended flow is:

1. Obtain one approximate event date from a cheap web-search/LLM discovery
   call, such as the answer to "ICML 2026 date". This is a scheduling hint,
   not proof that papers are downloadable.
2. Persist the estimate as `next_check_at` and do no more network or model
   work for that venue/year before it is due. A local scheduler may wake
   frequently, but it only reads SQLite and exits when nothing is due.
3. At or after `next_check_at`, invoke a coding agent in an isolated git
   worktree/branch. Give it the repository, venue, year, and a standing task
   prompt. The agent may investigate the web, inspect or edit scrapers, run
   tests, and attempt the scrape.
4. Capture a small machine-readable disposition plus the agent's natural-
   language explanation and worktree changes:
   `success`, `not_ready`, `needs_human`, or `failed`.
5. On `not_ready`, use the agent's suggested retry time or a bounded default
   delay of a few days. On success or human intervention, stop automatic
   retries. Send one email describing every run outcome.

The approximate date intentionally does not need deterministic citation or
readiness verification. A wrong estimate costs at most one contained agent
run, after which the agent can report that publication is not ready and
recommend when to retry.

## Scheduling semantics

`next_check_at` is the source of truth for due work. There is no weekly
pre-date discovery loop and no daily check of every conference:

```text
date missing -> discover approximate event date once -> sleep until due
                                                      |
                                                      v
                                              run coding agent
                                 +--------------------+------------------+
                                 |                    |                  |
                              success             not_ready       needs_human/failed
                                 |                    |                  |
                         clear next_check_at   retry in a few days   email and stop or
                                                                    bounded backoff
```

A date may be rediscovered only when it is missing, invalid, explicitly
superseded, or a later agent result says that the estimate changed. Adding a
new conference year also creates a new estimate; it does not make older
completed years active again.

## Current implementation

The following exists and runs today:

- `automation/monitor.py` and `automation/conferences.json`: the original
  deterministic source monitor. It checks registered OpenReview, official
  HTML, and PMLR sources for hash/count changes and stores immutable
  snapshots.
- `automation/local_service/production.py` and
  `automation/local_service/agent_control.py`: the installed Mac LaunchDaemon
  production composition. The service wakes hourly; once daily at or after 08:00
  America/Chicago it runs the baseline monitor and emails change/error events
  over TLS SMTP. The agent-control v2 boundary validates installed state and
  returns before every new external adapter while its gate is disabled.
- `automation/local_scheduler.py` and `automation/control_state.py`: local,
  lease-protected due-work selection and versioned SQLite storage. The schema
  is now version 10 and adds approximate-date and coding-agent schedules,
  immutable attempts, execution artifacts, and run-report delivery state,
  while still
  containing case, verification, notification, and execution-job tables
  inherited from the abandoned design. They are not evidence that those old
  workflows are active.
- `automation/event_dates.py` and
  `automation/providers/gemini.py::GeminiEventDateProvider`: an installed-disabled,
  fake-tested one-time date initializer. Given explicit venue/year targets, it
  calls the provider only when a pending target is due, stores an 08:00
  America/Chicago check time, sleeps on replay, and schedules a 30-day retry
  for expected no-date/provider failures. An isolated ICML 2026 live canary on
  2026-07-15 returned the correct main-conference start date after the adapter
  was adjusted for the provider's optional explanation field; it did not
  retain state or change the installed service.
- `automation/due_policy.py`: an installed-disabled, effect-free agent-run policy.
  It claims at most one due schedule, durably applies `success`, `not_ready`,
  `needs_human`, and `failed`, and enforces default/suggested retries, bounded
  failure backoff, a global active slot, a monthly ceiling, and a recent
  distinct-venue systemic-failure circuit. It does not start an agent.
- `automation/codex_agent.py`: an installed-disabled, fake-tested Codex-only runner.
  It uses real isolated Git worktrees in tests, pins Codex's workspace sandbox
  and structured result schema, durably records bounded review artifacts, and
  verifies the primary checkout did not change. Four authorized ICML 2026
  canary starts verified
  isolation and exposed CLI flag placement, portable-schema, and overly strict
  optional-field handling; none modified either checkout. The final call was
  cleanly accepted as `not_ready`. Its installed caller is disabled.
- `automation/agent_worktree_retention.py`: an explicit, installed-disabled retention
  effect that removes only terminal schema-10 worktrees beneath the exact
  configured runs root, using age and count bounds. Unregistered worktrees,
  including the retained ICML 2026 canary, are never candidates.
- `automation/agent_run_notifications.py`: an installed-disabled run-report composer
  and delivery coordinator. Terminal runner completion creates one pending
  report atomically; Resend supplies the selected provider idempotency key,
  and transient/permanent delivery state never changes the run outcome.
- `automation/agent_production.py` and
  `automation/config/agent_targets.v1.json`: an installed-disabled, fake-tested
  production composition and explicit AISTATS/ICML/IJCAI 2026 cohort. A date
  lookup, agent run, report attempt, and retention cleanup are separately
  bounded; its installed caller returns before adapters are constructed.
- `automation/agent_credentials.py` and `automation/agent_canary.py`: private
  dedicated-role credential layout plus three mutually exclusive operator
  canaries. Codex receives an explicit private `CODEX_HOME`, Gemini loads an
  explicit private ADC file, and Resend secrets are marker-bound without
  activation. Each canary requires its own live authorization flag; installed
  Gemini, Codex, and Resend canaries have completed. The Resend canary used one
  provider request addressed to the two approved recipients, and the operator
  confirmed delivery to both.
- `automation/local_service/agent_control.py`: disabled-only marker-last
  replacement primitives for runtime/source refresh and Resend secret
  provisioning. Either an enabled current configuration or an enabled
  candidate is rejected, and an interrupted replacement fails marker
  validation closed.
- `automation/agent_activation.py`: an installed-disabled, fake-tested
  read-only readiness audit plus separately authorized activation, disabled
  rehearsal, and rollback commands. It requires exact schema/idle,
  credential/allowlist, disk/source, fixed-service, and fresh paused/drained
  cloud evidence before the gate can be opened. Its installed disabled
  rehearsal completed and the read-only audit passed; the gate remains false,
  and implementation or rehearsal permission is not activation permission.
- `automation/control_state_migration.py`: a safe-summary read-only audit,
  new-file SQLite backup, and isolated-copy schema rehearsal command. Fixture
  rehearsal passed; the dedicated-role production database was migrated from
  schema 6 to schema 10 with a retained private backup.
- `automation/discovery.py` and `automation/providers/gemini.py`: a budgeted,
  cached Gemini Search Grounding adapter with an explicit manual `--live`
  command. Its current output is stricter than the approximate-date signal the
  target scheduler needs; that scheduling use is not wired.
- `automation/resend_notifications.py`: the selected low-level Resend HTTPS
  adapter for agent-run reports. Its rotated private credential and approved
  two-recipient allowlist are installed, while its automatic caller remains
  disabled by the global external-effects gate.
- `automation/prefect_flows.py`, `automation/run_monitor_flow.py`, and
  `automation/deployment/`: the paused Cloud Run monitor retained solely as a
  rollback path. It is not the target scheduler.

External GCP and Mac state must be inspected before making a live-health
claim; repository files only describe the expected topology.

## Not yet active or built

- Automatic future-year cohort creation.
- Production external effects are not active. The explicit transition tooling
  exists in the repository, but activation has not been authorized or run.
- Migration of `control_state.py` from its vestigial old schema to the small
  date/dispatch/run model.

Never describe one of these as implemented merely because an old schema,
contract, fixture, or archived document exists.

## Documentation map

- [`architecture.md`](./architecture.md): target components and invariants.
- [`roadmap.md`](./roadmap.md): current phase status and acceptance criteria.
- [`development.md`](./development.md): development and validation workflow.
- [`installation-readiness.md`](./installation-readiness.md): read-only audit,
  isolated rehearsal, and separately authorized installation gates.
- [`local-first-decision.md`](./local-first-decision.md): why the production
  scheduler is a single local Mac service rather than Prefect orchestration.
- [`../automation.md`](../automation.md): current deployed monitor behavior and
  rollback boundary.
- [`archive/README.md`](./archive/README.md): abandoned deterministic-
  verification design and its historical documents.

Host-specific `docs/local-p4*-operations.md` files may exist in a maintainer's
checkout. They are intentionally excluded from Git: `local-p4lc` records the
current cutover/rollback evidence, while `local-p4o` and `local-p4ls` are
historical audit records. They are not development guidance and must never
contain or be copied into tracked secrets or host identifiers.

## Sources of truth

- Current behavior: executable code and tests.
- Target automation behavior: this directory's non-archive documents.
- Current deployed topology: [`../automation.md`](../automation.md), checked
  against the actual Mac/GCP state.
- Canonical dataset coverage: `statistics.md`.
- Historical design only: `archive/`.
