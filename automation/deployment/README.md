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

This retained rollback topology lets Cloud Scheduler start a Cloud Run Job.
The schedule is paused after P4.LC. When resumed, the job executes
`automation.run_monitor_flow`, reports flow and task state to Prefect Cloud,
and persists its SQLite state and immutable source snapshots in GCS.

P4.4's job-result code is not part of this deployed topology. It provides a
fake-tested injected GCS bucket boundary and schema-version-4 consumption
tables, but constructs no client and has no configured bucket, prefix, IAM
role, worker credential, flow, or migration here. P4.L1 advances the local
repository schema to version 5 with an immutable owner and bounded scheduler
journal. P4.L2 adds schema-version-6 active plan state and fixture-only
discovery/verification/lifecycle/case/reminder composition. P4.L3 adds the
credential-free local service renderer, private internal paths, bounded
records, missing-volume closure, and exact rollback scope. P4.LS adds a
marker-gated scheduler-only mode installed on one authorized Mac against
isolated local state; its reboot/SSH/missing-volume/recovery/rollback and
co-resident health drills passed. P4.LC adds the separately marked local
production effect; it restores a validated copy of this legacy monitor tree
without treating it as schema-v6 control state. None of the P4.L modules is
imported by the retained cloud job. The local production service makes only
the existing deterministic monitor/TLS SMTP calls plus local due selection; it
makes no discovery, verifier, case-delivery, job, or command call.
P4.LC completed the authorized no-overlap cutover on 2026-07-14. The local
LaunchDaemon is now authoritative and this Cloud Scheduler job is paused. The
Cloud Run job, secrets, and GCS monitor tree are retained as the tested
rollback path. Timed rollback proved that local can be stopped before cloud is
resumed and recovered; final activation paused/drained cloud again before
local opened the refreshed generation. Never enable this schedule while the
local production label is loaded.

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
