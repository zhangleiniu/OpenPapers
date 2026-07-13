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
status, size, Content-Length, and `%PDF-` checks. These components have no live
HTTP adapter and are not deployed. P2.4 adds a separate local, versioned
single-writer SQLite repository with an expiring lease, atomic replayable
verification history, and optimistic conference-state revisions. It is not
wired into this deployment. P2.5 adds a local pure lifecycle reducer,
evidence-time scheduling, inert typed action intents, and a thin P2.4
composition boundary; fixture replay covers all catalog venues, but none of
this code is scheduled or deployed and no intent is persisted, submitted, or
executed. PDF processing and internal-copy permissions remain separate, and
verification grants no redistribution authority. A live integrated verifier,
persistent case state, reminder delivery, the Mac mini worker, Codex repair
execution, and MustCite deployment are not implemented.

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

The monitor and scraper remain plain Python commands. Prefect can later wrap
them for schedules, retries, concurrency, logs, notifications, and downstream
deployments without moving parsing logic into Prefect tasks.

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

The production monitor uses Cloud Scheduler to start a Cloud Run Job directly.
The container runs `automation.run_monitor_flow`, so Prefect Cloud still records
the flow and task runs and receives OpenPapers events. This avoids giving
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
