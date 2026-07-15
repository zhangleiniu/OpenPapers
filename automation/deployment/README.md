# Cloud monitor rollback deployment

This directory contains the optional dependencies and assets for the retained
Cloud Run version of the deterministic source monitor. It is a rollback path,
not the target automation control plane. Core OpenPapers scrapers do not
require these dependencies.

## Current topology

Cloud Scheduler can start one Cloud Run Job. The job runs
`automation.run_monitor_flow`, records flow/task state in Prefect Cloud, and
persists the monitor's SQLite state and immutable source snapshots in GCS.

After the authorized local cutover on 2026-07-14:

- the Mac LaunchDaemon is the intended sole production writer;
- the Cloud Scheduler trigger is paused;
- the Cloud Run job, secrets, GCS monitor tree, and deployment assets are
  retained for rollback;
- external GCP state, not this file, determines whether that rollback path is
  currently healthy.

Never enable the cloud schedule while the local OpenPapers production label is
loaded. Rollback must stop and verify local before resuming cloud; local
activation must pause and drain cloud before opening local state.

This deployment has no approximate-date scheduler, coding-agent invocation,
Mac worker, scrape-job transport, or run-report path. Historical documents may
refer to those abandoned prototypes, but none is part of this image.

## Install and test

Install this dependency set only when maintaining or exercising the rollback
component:

```bash
python -m pip install -r automation/deployment/requirements.txt
python -m unittest discover -s automation/tests -v
```

## Build

The Cloud Build configuration publishes the image to an `openpapers` Artifact
Registry repository in the configured regional location:

```bash
gcloud builds submit \
  --project="$GCP_PROJECT_ID" \
  --config=automation/deployment/cloudbuild.yaml .
```

`OPENPAPERS_GCP_REGION` is separate from `GCP_LOCATION`: Cloud Run and
Artifact Registry require a region, while Vertex AI discovery may use
`global`. The deployment default is `us-central1`.

## Expected cloud resources

The rollback topology expects externally managed equivalents of:

- one GCS bucket for monitor state and snapshots;
- one Artifact Registry Docker repository;
- one least-privilege Cloud Run runtime service account;
- Secret Manager entries for Prefect and OpenReview credentials;
- one Cloud Run Job and one normally paused Cloud Scheduler trigger;
- a Prefect email block only when the rollback monitor is configured to send
  mail through Prefect.

Do not copy real names, project identifiers, credentials, or secret values into
the repository, documentation, prompts, fixtures, or logs. Inspect existing
resources before creating or changing anything.

The container receives non-secret deployment configuration through environment
variables and credentials through Secret Manager. Expected names in the
current implementation include:

```text
GCP_PROJECT_ID
OPENPAPERS_GCP_REGION
OPENPAPERS_MONITOR_BUCKET
SCRAPER_DATA_ROOT=/tmp/openpapers-data
PREFECT_API_URL
PREFECT_API_KEY                 (secret)
OPENREVIEW_USERNAME             (secret)
OPENREVIEW_PASSWORD             (secret)
OPENPAPERS_EMAIL_BLOCK          (optional)
OPENPAPERS_EMAIL_FROM           (optional)
OPENPAPERS_EMAIL_TO             (optional)
```

Prefect reads `PREFECT_API_KEY`, not `PREFECT_KEY`. Never bake it into the
image or store it in `.env` for deployment.

## Operational use

Normal development and target-system work must not mutate this deployment.
An actual rollback, cloud health check, schedule change, image deployment, or
secret/IAM change requires explicit operator authorization and a fresh
single-writer/co-resident-service safety check.

The accepted local-first decision and current rollback invariants are in
[`docs/automation-system/local-first-decision.md`](../../docs/automation-system/local-first-decision.md).
