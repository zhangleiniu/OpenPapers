# Automation and Monitoring

The automation layer is a control plane around the existing deterministic
scrapers. It detects source changes cheaply and emits structured events; it
does not publish datasets or execute LLM-generated code by itself.

This page documents the **current deployed implementation**. Phase 0's strict
contracts, catalog, policy configuration, and pure state/idempotency rules now
exist in the repository, including evidence-driven `next_check_at` scheduling,
but are not wired into this deployment. A Phase 1 Gemini Search Grounding
adapter, cache/budget controls, and an explicit shadow-only command also exist
in the repository. Its 15-venue live review is complete and the phase is in
shadow status, but it is not deployed or scheduled. Phase 2.1/P2.1R supplies
versioned verification request/result schemas, cross-artifact semantic
validation, catalog trust and crawl-policy gates, and fake-tested
fetch/snapshot interfaces with sanitized redirect retention. P2.2 adds a
fake/fixture-only redirect coordinator and bounded HTML verifier for exact
identity, dates, list counts, metadata, and current proceedings indexes. P2.3
adds fake/fixture-only, permission-gated deterministic PDF sampling with
status, size, Content-Length, and `%PDF-` checks. P2.4 adds a separate local,
versioned single-writer SQLite repository with an expiring lease, atomic replayable
verification history, and optimistic conference-state revisions. It is not
wired into this deployment. P2.5 adds a local pure lifecycle reducer,
evidence-time scheduling, inert typed action intents, and a thin P2.4
composition boundary; fixture replay covers all catalog venues, but none of
this code is scheduled or deployed and no intent is persisted, submitted, or
executed. P2.S adds an explicit `--live` manual shadow command with
public-address-only DNS checks, pinned hostname-verified HTTPS, a separately
reviewed crawl policy, immutable snapshots, and isolated state. Its 15-venue
review is complete, but it is not scheduled or deployed and cannot dispatch
an intent. PDF processing and internal-copy permissions remain separate, and
verification grants no redistribution authority. P3.1 adds a local pure case
domain and control-state schema version 2 with lease-protected deduplicated
case current/history/event persistence plus resolve, snooze, ignore, and
reactivate controls. P3.2 adds local clock-controlled case aging and grouped
weekly/monthly/dormant digest data. P3.3 adds a strict redacted notification
intent and control-state schema version 3 for unique sources and immutable
intents plus durable numbered-attempt history, exercised only through injected
fake transports. P3.4 adds a local coordinator that consumes typed P2.5
transition/case action data, persists case events independently, queries due
repository reminders, filters already claimed slots, and retains only pending
immediate/grouped shadow intents with zero attempts. It has no command,
scheduler, or transport call. These local Phase 3 packages are not wired to
this deployment. P3.S adds a separate manual `--live` command and one-request
Resend adapter that can deliver only a fixed three-item synthetic canary after
an approved-recipient fingerprint check. Its first isolated canary was accepted
by the provider, but it cannot read P3.4 output and adds no production
recipient, schedule, Prefect/Cloud Run integration, or production-state
change. P4.1 adds local version 2 immutable job identity, a strict typed queue
envelope, an inert `openpapers-mac` process work-pool blueprint, and an
injected fake-tested Prefect deployment submission adapter. It is not imported
or called by the deployed monitor and creates no Prefect resource or flow run.
P4.2 adds an isolated Mac package whose only flow revalidates and simulates
fixture jobs, plus local secret-safe prerequisite checks and an uninstalled
credential-free `launchd` template/runbook. Nothing was installed, logged in,
loaded, started, or connected, and there is still no executable command
mapping. P4.3 adds a private Mac-local safety journal,
process-safe venue/year locks, a two-threshold disk gate, timeout/cancellation
supervision over injected fake handles, completed-delivery suppression, and a
fixed Prefect pull/offline policy. It is not called by the deployed monitor or
P4.2 fixture flow, selects no command, publishes no job result, and has not been
installed or exercised on a real Mac/Prefect queue. P4.4 adds strict local
manifest/result contracts, an injected GCS-compatible create-only publisher
and exact-generation reader, the schema-version-4 tables for lease-protected
exactly-once logical consumption, and a thin local consumer.
Its tests use a fake bucket and temporary database; it constructs no GCS
client, publishes or consumes no live object, applies no lifecycle transition,
and is not imported by this deployment. P4.O's operator feasibility gate was
rejected before Prefect resource creation because the acceptable cloud plan
does not support its hybrid process pool. The accepted replacement is a
local-first bounded scheduler. P4.L1 adds a schema-version-5 immutable
cloud/local database owner, bounded wakeup/due-selection history, and a
plain-Python fake-clock runner over temporary SQLite. P4.L2 advances that
isolated repository to schema version 6 with active plan state and composes
injected fake discovery/verification, lifecycle reduction, case and pending
shadow-output integration, due reminders, and inert actions under one local
lease. P4.L3 adds the `automation/local_service/` package
with private internal state/record paths, a missing-volume gate, bounded
secret-safe health/run artifacts, a credential-free system LaunchDaemon
renderer, and exact rollback scope. Its ordinary command has no concrete
effect. P4.LS adds an exact private marker and a scheduler-only shadow mode;
one authorized Mac installation passed duplicate, missing-volume,
ambiguous-recovery, scoped rollback/reinstall, SSH-disconnect, reboot, and
co-resident health drills against isolated local-owned state. P4.LC adds the
strict production marker/configuration/secret boundary, restores the legacy
monitor database separately from schema-v6 local control, preserves the
existing daily six-source monitor and TLS SMTP notifications, and durably
suppresses ambiguous or duplicate daily effects. Its authorized no-overlap
cutover, local/co-resident health gates, and 96-second timed rollback passed.
P5.1 adds a pure local registry that maps strict scrape/validation jobs to two
fixed repository entry points and literal typed arguments. It rejects Codex,
shell, paths, caller flags, and environment expansion, and is not imported by
or connected to this deployment. P5.2 adds an isolated existing-scraper
staging executor with trusted runtime binding, private canonical-disjoint
per-job roots, strict atomic checkpoints, same-root resume, timeout,
cancellation, and ambiguous-stop closure. Its subprocess adapter has no CLI or
caller; tests use fake launchers and temporary roots and execute neither a
scraper nor validator. P5.3 adds a separately invoked, fixture-only staged
validation boundary: it requires that process-success checkpoint, retains a
safe bounded inventory and candidate manifest below a separate private root,
then uses a bound validation job to produce a strict versioned independent
report and validation manifest for completeness, count, metadata, duplicate,
and PDF checks. It has no runtime caller and publishes no result.
P5.4 adds a fixture-only local coordinator that holds the existing P4.3
venue/year lock, disk gate, and exact claim across injected P5.2 staging, P5.3
validation, and P4.4 immutable publication. It derives the validation job from
the exact candidate, routes ready/partial/retry/cancelled/ambiguous outcomes
with transient/operational/structural classes, and exactly replays a
manifest-only publication failure. Its tests use fake launchers, disk state,
publishers, and temporary roots; it has no installed caller or authorized real
scrape. P5.S adds a separate manual `--live` command with a private marked
root, canonical/repository write-denying macOS sandbox, and create-only local
result store. Its authorized COLT 2025 archival run recovered from a confirmed
timeout, independently validated 181 papers and PDFs, suppressed exact replay,
and preserved canonical/local/cloud/co-resident gates. It remains uninstalled
and cannot promote data or change control state.

The local LaunchDaemon is now authoritative and the retained Cloud Scheduler
job is paused. Live discovery/verification and Phase 3 case-delivery effects,
automatic scraper/validator execution, connected result wiring, Codex repair
execution, and MustCite deployment are not implemented.
P5.1/P5.2/P5.3/P5.4 remain unconnected to the installed runtime; P5.S is a
manual shadow-only caller and changes no service or deployment.

P2.9 additionally closes the fixture-reproducible COLT/2025 grounding-wrapper
source shape in the uninstalled automatic verifier. It uses only exact
repository-known source URLs, a bounded PMLR listing/PDF profile, and the
existing crawl gates; the Google wrapper remains denied and is never fetched.
P2.9S ran as a separate manual isolated live package. Its real response lacked
the PMLR domain label needed by P2.9's closed mapping, so it fetched only the
official COLT page, produced no PDF target, and retained no action. Exact
replay added no calls, and neither invocation altered the installed
LaunchDaemon or current deployed behavior. P2.10/P2.10S remain unimplemented
fixture/live follow-up packages; P5.5S is still blocked.

Start at the [automation system development guide](./automation-system/README.md) for
the implemented foundation, target architecture, roadmap, and zero-context
development workflow.

## Registry and runtime state

`automation/conferences.json` is versioned configuration. Each conference-year
lists candidate sources and detector settings. Frequently changing state is
stored separately in `$SCRAPER_DATA_ROOT/monitor/state.sqlite3`.

```bash
python automation/monitor.py
python automation/monitor.py --venue ijcai --year 2026
python automation/monitor.py --no-write
```

Each JSON-line event reports source status, item count, content hash, whether
it changed since the previous observation, diagnostic detail, and the most
recent immutable snapshot path. Raw HTML/JSON is saved on first observation
and whenever the source changes, providing a reproducible fixture for repair.
Supported detectors are:

- `openreview_api`: hashes the sorted accepted-note IDs.
- `official_html`: hashes normalized text for a configured repeated item.
- `pmlr_volume`: detects a matching proceedings listing.

## Orchestration boundary

The monitor and scraper remain plain Python commands. P4.LC now runs the
existing deterministic monitor plus the bounded local scheduler from the
headless Mac LaunchDaemon. The retained Cloud Run entry point still uses
Prefect for rollback observability, but its Cloud Scheduler job is paused.
Rollback must stop the local label before resuming cloud; final activation must
pause and drain cloud before starting local.

An agent repair workflow should consume only a change or validation-failure
event plus a saved source snapshot. Generated parser changes must include a
fixture and tests and pass review/CI before execution. Web content must be
treated as untrusted input; an extraction agent should not receive deployment
credentials or unrestricted code-execution authority.

## Prefect flows

`automation/prefect_flows.py` provides two flows:

- `openpapers-monitor` restores its SQLite/snapshot tree from GCS, runs the
  deterministic detectors, persists state, and emits
  `openpapers.source.changed` or `openpapers.source.error` events.
- `openpapers-update-conference` returns `awaiting_approval` unless its
  `approved` parameter is explicitly true. Approved runs invoke the existing
  scraper and independent validator; statistics updates are a separate opt-in.

The Cloud Run image is intentionally monitor-only and uses
`automation/deployment/requirements.txt`, avoiding the large Vertex AI
dependency. Install and test the optional component separately:

```bash
python -m pip install -r automation/deployment/requirements.txt
python -m unittest discover -s automation/tests -v
```

`OPENPAPERS_GCP_REGION` is deliberately separate from `GCP_LOCATION` because
Vertex AI may use `global`, while Cloud Run and Artifact Registry require a
regional location. It defaults to `us-central1`.

The retained rollback monitor uses Cloud Scheduler to start a Cloud Run Job
directly, but that schedule is paused after P4.LC. When explicitly resumed for
rollback, the container runs `automation.run_monitor_flow`, so Prefect Cloud
records the flow/task runs and receives OpenPapers events. This avoids giving
Prefect Cloud a long-lived GCP service-account key. Container and GCP deployment
assets are self-contained under `automation/deployment`; see its
[deployment guide](../automation/deployment/README.md).

The deployed job needs `PREFECT_API_KEY`, `OPENREVIEW_USERNAME`, and
`OPENREVIEW_PASSWORD` from Secret Manager, plus a runtime service account with
access to the monitor GCS bucket. It runs daily at 08:00 America/Chicago with
one task. Cloud Run must use GCS because its local filesystem is ephemeral.

## Email notifications

The flow uses a `prefect-email` `EmailServerCredentials` block and sends change
and error emails from a retried task. For Resend SMTP, configure the block as:

```text
username: resend
password: <Resend API key>
smtp_server: smtp.resend.com
smtp_type: SSL
smtp_port: 465
verify: true
```

Set `OPENPAPERS_EMAIL_BLOCK` to that block's name,
`OPENPAPERS_EMAIL_FROM` to an address on a domain verified by Resend, and
`OPENPAPERS_EMAIL_TO` to the recipient. Resend's API key and SMTP password are
the same secret. Notification failures are retried and fail visibly in
Prefect.

Prefect reads `PREFECT_API_KEY`, not `PREFECT_KEY`. Local CLI login stores the
standard setting in the selected Prefect profile. A serverless deployment must
receive `PREFECT_API_KEY` through Secret Manager rather than `.env` or the
container image.
