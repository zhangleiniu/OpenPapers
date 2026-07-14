# Monitor deployment

This directory contains the optional dependencies and deployment assets for
running the deterministic source monitor in Google Cloud. The core OpenPapers
scrapers do not require them.

## Install and test

From the repository root:

```bash
python -m pip install -r automation/deployment/requirements.txt
python -m unittest discover -s automation/tests -v
```

## Topology

Cloud Scheduler starts a Cloud Run Job. The job executes
`automation.run_monitor_flow`, reports flow and task state to Prefect Cloud,
and persists its SQLite state and immutable source snapshots in GCS.

P4.4's job-result code is not part of this deployed topology. It provides a
fake-tested injected GCS bucket boundary and a local schema-version-4
consumption ledger, but constructs no client and has no configured bucket,
prefix, IAM role, worker credential, flow, or migration here. Do not reuse the
monitor tree or grant a shadow Mac process access to `control/state.sqlite3`.
The accepted local-first design keeps this deployment authoritative until a
later package has passed isolated host drills. Its cutover must back up state,
disable Cloud Scheduler before activating the local writer, verify health, and
retain rollback; both writers must never mutate the same state concurrently.
No such cutover is implemented or authorized by this document.

## Build

The Cloud Build configuration uses the active project ID and publishes the
image to an `openpapers` Artifact Registry repository in `us-central1`:

```bash
gcloud builds submit \
  --project="$GCP_PROJECT_ID" \
  --config=automation/deployment/cloudbuild.yaml .
```

## Required cloud resources

The deployment expects:

- a GCS bucket named `$GCP_PROJECT_ID-openpapers-monitor`;
- an Artifact Registry Docker repository named `openpapers`;
- a runtime service account named `openpapers-monitor` with object access to
  that bucket and Secret Manager accessor permission;
- Secret Manager secrets named `prefect-api-key`, `openreview-username`, and
  `openreview-password`;
- a Prefect `EmailServerCredentials` block when email notification variables
  are configured.

Enable the required APIs and create the non-secret resources once:

```bash
export OPENPAPERS_GCP_REGION=us-central1

gcloud services enable \
  run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  --project="$GCP_PROJECT_ID"

gcloud artifacts repositories create openpapers \
  --project="$GCP_PROJECT_ID" \
  --location="$OPENPAPERS_GCP_REGION" \
  --repository-format=docker

gcloud storage buckets create \
  "gs://$GCP_PROJECT_ID-openpapers-monitor" \
  --project="$GCP_PROJECT_ID" \
  --location="$OPENPAPERS_GCP_REGION" \
  --uniform-bucket-level-access

gcloud iam service-accounts create openpapers-monitor \
  --project="$GCP_PROJECT_ID" \
  --display-name="OpenPapers Monitor"
```

Create the three secrets in Secret Manager and add their values without placing
them in shell history or version control. Grant the runtime service account
access only to those secrets and the monitor bucket.

## Job and schedule

Deploy the image as a single-task Cloud Run Job using the runtime service
account. Configure these non-secret environment variables:

```text
GCP_PROJECT_ID
OPENPAPERS_MONITOR_BUCKET
SCRAPER_DATA_ROOT=/tmp/openpapers-data
PREFECT_API_URL
OPENPAPERS_EMAIL_BLOCK
OPENPAPERS_EMAIL_FROM
OPENPAPERS_EMAIL_TO
```

Map the three credentials from Secret Manager to `PREFECT_API_KEY`,
`OPENREVIEW_USERNAME`, and `OPENREVIEW_PASSWORD`. Schedule the Cloud Run Jobs
`run` API endpoint with Cloud Scheduler. The production schedule is
`0 8 * * *` in `America/Chicago`.
