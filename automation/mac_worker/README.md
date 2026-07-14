# Mac worker package

This directory contains the accepted P4.2 receiving prototype and P4.3 local
safety semantics. P4.4's immutable publisher is implemented separately in
`automation/job_results.py`. Nothing here is installed, scheduled, connected
to Prefect Cloud or GCP, or able to execute a scraper, validator, or Codex
process.

The Prefect flow and launchd template are retained as tested historical
interfaces. Do not install the template or provision its work pool. The P4.O
operator attempt established that the required hybrid process work pool is not
available on the acceptable Prefect Cloud plan, so P4.O is paused. The adopted
target is a credential-free local scheduler that will reuse the validated job,
safety, and result contracts without a Prefect transport. See
`docs/automation-system/local-first-decision.md` and the P4.L packages in
`docs/automation-system/work-packages.md`.

## Current package boundary

- `runtime.py` revalidates a P4.1 queue envelope and returns only a stable
  `simulated` observation.
- `prefect_support.py` contains the optional Prefect imports, fixture-only
  flow, and a local-settings probe that makes no API request.
- `health.py` checks bounded local prerequisites without reading Codex
  authentication contents or reporting configured paths, settings, URLs, or
  credentials.
- `safety.py` provides a private local claim/completion journal, venue/year
  locks, disk gates, bounded timeout/cancellation supervision over injected
  handles, and completed-job replay suppression. It accepts no command, and
  its local marker is not a P4.4 result.
- `requirements.txt` is isolated from the core scraper and deployed-monitor
  dependency sets.
- `launchd/org.openpapers.prefect-worker.plist.example` remains an inert,
  placeholder-bearing historical template. It must not be loaded as-is.

The template refuses to create a missing work pool, disables runtime package
installation, and disables Prefect's optional HTTP health server. These
properties remain useful evidence, but the Prefect-specific process is no
longer the target service.

## Local health

This command starts no worker and makes no network request:

```bash
.venv/bin/python -m automation.mac_worker \
  --repository-root "$PWD" \
  --data-root "$SCRAPER_DATA_ROOT" \
  --codex-auth-path "$HOME/.codex/auth.json"
```

It reports only stable check names, pass/fail states, and bounded reason codes.
The runtime check requires macOS and Python 3.12. The Codex marker check proves
only that a private owner-readable regular file exists; it neither reads the
file nor proves that authentication is current. The Prefect check is relevant
only to the frozen prototype and makes no API call.

## Reusable P4.3 safety contract

The local supervisor revalidates one immutable envelope, takes a non-blocking
venue/year lock, and checks minimum free bytes and free fraction before writing
a durable claim. Only typed confirmed success becomes a completed marker;
exact completed replay skips the starter.

Confirmed stopped failure, cancellation, or timeout may clear the claim so an
explicit retry can use the same job ID. An active claim on reopen, invalid
outcome, post-start supervision error, cancellation failure, or unconfirmed
stop remains `recovery_required` and blocks that venue/year. Never age out or
delete such a claim merely to make work run again.

The original offline policy delegated undelivered work to Prefect. The local
replacement must instead derive due work from durable local control state; it
must not introduce a second local queue or create replacement job identities.
That replacement behavior belongs to P4.L1 and later packages and is not
implemented here.

## Current operational state

No OpenPapers plist was installed or loaded, no worker was started, no P4.3
operator journal was created, and no live manifest/result was published or
consumed during P4.O. The attempted Prefect apply failed before resource
creation; a subsequent inspection found the planned pool, queues, and
deployments absent. The existing deployed deterministic monitor remains the
only production scheduler/writer until a separately authorized cutover.

Host-specific account, path, and cleanup evidence belongs only in the ignored
local operations record. Never put profile contents, API keys, `.env` values,
project identifiers, or credential output in this repository.
