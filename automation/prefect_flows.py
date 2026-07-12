"""Prefect flows for monitoring and manually approved dataset updates."""

import asyncio
import html
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from google.cloud import storage
from prefect import flow, get_run_logger, task
from prefect.events import emit_event
from prefect_email import EmailServerCredentials, email_send_message

from automation.monitor import DEFAULT_REGISTRY, DEFAULT_STATE, run as run_monitor


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GCS_PREFIX = "monitor"


def _sync_from_gcs(bucket_name: str, prefix: str, local_dir: Path,
                   project: Optional[str] = None) -> int:
    """Restore the small monitor state tree before a serverless run."""
    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    count = 0
    normalized = prefix.strip("/") + "/"
    for blob in client.list_blobs(bucket, prefix=normalized):
        relative = blob.name[len(normalized):]
        if not relative or relative.endswith("/"):
            continue
        target = local_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(target)
        count += 1
    return count


def _sync_to_gcs(bucket_name: str, prefix: str, local_dir: Path,
                 project: Optional[str] = None) -> int:
    """Persist SQLite and immutable snapshots after a serverless run."""
    if not local_dir.exists():
        return 0
    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    normalized = prefix.strip("/")
    count = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        name = f"{normalized}/{path.relative_to(local_dir).as_posix()}"
        bucket.blob(name).upload_from_filename(path)
        count += 1
    return count


@task(retries=3, retry_delay_seconds=[30, 120, 300])
def restore_monitor_state(bucket_name: str, prefix: str, local_dir: str,
                          project: Optional[str] = None) -> int:
    return _sync_from_gcs(bucket_name, prefix, Path(local_dir), project)


@task(retries=3, retry_delay_seconds=[30, 120, 300])
def persist_monitor_state(bucket_name: str, prefix: str, local_dir: str,
                          project: Optional[str] = None) -> int:
    return _sync_to_gcs(bucket_name, prefix, Path(local_dir), project)


@task(retries=2, retry_delay_seconds=[60, 300])
def check_sources(registry_path: str, state_path: str,
                  venue: Optional[str], year: Optional[int],
                  write_state: bool) -> list[dict]:
    return run_monitor(
        registry_path=Path(registry_path), state_path=Path(state_path),
        venue=venue, year=year, write_state=write_state)


def _emit_source_events(events: list[dict]) -> None:
    for source in events:
        if source["status"] == "error":
            event_name = "openpapers.source.error"
        elif source["changed"]:
            event_name = "openpapers.source.changed"
        else:
            continue
        resource_id = f"openpapers.source.{source['venue']}.{source['year']}"
        emit_event(
            event=event_name,
            resource={
                "prefect.resource.id": resource_id,
                "prefect.resource.name": (
                    f"{source['venue'].upper()} {source['year']} source"),
                "openpapers.venue": source["venue"],
                "openpapers.year": str(source["year"]),
            },
            payload=source,
        )


@task(retries=2, retry_delay_seconds=[30, 120])
def notify_source_event(source: dict, block_name: str,
                        email_from: str, email_to: str) -> None:
    """Send one change/error email from the flow execution environment."""
    if source["status"] == "error":
        event_name = "openpapers.source.error"
    elif source["changed"]:
        event_name = "openpapers.source.changed"
    else:
        return
    credentials = EmailServerCredentials.load(block_name)
    body = (
        f"Event: {event_name}\n"
        f"Venue: {source['venue']}\n"
        f"Year: {source['year']}\n"
        f"Source: {source['source_key']}\n"
        f"Status: {source['status']}\n"
        f"Count: {source['item_count']}\n"
        f"Detail: {source['detail']}\n"
        f"Snapshot: {source['snapshot_path']}"
    )
    asyncio.run(email_send_message.fn(
        email_server_credentials=credentials,
        subject=f"OpenPapers: {event_name}",
        msg=f"<pre>{html.escape(body)}</pre>",
        msg_plain=body,
        email_from=email_from,
        email_to=email_to,
    ))


@flow(name="openpapers-monitor", log_prints=True)
def monitor_flow(
    venue: Optional[str] = None,
    year: Optional[int] = None,
    registry_path: str = str(DEFAULT_REGISTRY),
    state_path: str = str(DEFAULT_STATE),
    bucket_name: Optional[str] = None,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    require_remote_state: bool = False,
    write_state: bool = True,
    email_block_name: Optional[str] = None,
    email_from: Optional[str] = None,
    email_to: Optional[str] = None,
) -> list[dict]:
    """Monitor registered sources and emit change/error events.

    Cloud Run deployments set ``require_remote_state`` and a GCS bucket so
    SQLite and snapshots survive the container's ephemeral filesystem.
    """
    logger = get_run_logger()
    bucket_name = bucket_name or os.getenv("OPENPAPERS_MONITOR_BUCKET")
    email_block_name = (
        email_block_name or os.getenv("OPENPAPERS_EMAIL_BLOCK"))
    email_from = email_from or os.getenv("OPENPAPERS_EMAIL_FROM")
    email_to = email_to or os.getenv("OPENPAPERS_EMAIL_TO")
    if email_block_name and (not email_from or not email_to):
        raise ValueError(
            "OPENPAPERS_EMAIL_FROM and OPENPAPERS_EMAIL_TO are required "
            "when email notifications are enabled")
    project = os.getenv("GCP_PROJECT_ID")
    state = Path(state_path)
    monitor_dir = state.parent

    if require_remote_state and not bucket_name:
        raise ValueError(
            "OPENPAPERS_MONITOR_BUCKET/bucket_name is required on Cloud Run")
    if bucket_name and write_state:
        restored = restore_monitor_state(
            bucket_name, gcs_prefix, str(monitor_dir), project)
        logger.info("Restored %d monitor-state objects from GCS", restored)

    events = check_sources(
        registry_path, state_path, venue, year, write_state)

    if bucket_name and write_state:
        persisted = persist_monitor_state(
            bucket_name, gcs_prefix, str(monitor_dir), project)
        logger.info("Persisted %d monitor-state objects to GCS", persisted)

    _emit_source_events(events)
    if email_block_name:
        for source in events:
            if source["status"] == "error" or source["changed"]:
                notify_source_event(
                    source, email_block_name, email_from, email_to)
    errors = [event for event in events if event["status"] == "error"]
    logger.info(
        "Checked %d sources: %d changed, %d errors",
        len(events), sum(event["changed"] for event in events), len(errors))
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))
    return events


@task(retries=1, retry_delay_seconds=60)
def run_command(arguments: list[str]) -> str:
    """Run one repository CLI command and surface its captured output."""
    result = subprocess.run(
        arguments, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {arguments}\n{output}")
    logging.getLogger(__name__).info(output)
    return output


@flow(name="openpapers-update-conference", log_prints=True)
def update_conference_flow(
    venue: str,
    year: int,
    approved: bool = False,
    download_pdfs: bool = False,
    completeness_level: str = "metadata",
    expected_count: Optional[int] = None,
    update_statistics: bool = False,
) -> dict:
    """Run a conference update only after an explicit approval parameter."""
    if not approved:
        payload = {
            "venue": venue.lower(), "year": year,
            "status": "awaiting_approval",
        }
        emit_event(
            event="openpapers.update.approval-required",
            resource={
                "prefect.resource.id": f"openpapers.update.{venue.lower()}.{year}",
                "prefect.resource.name": f"{venue.upper()} {year} update",
            },
            payload=payload,
        )
        return payload

    scrape = [sys.executable, "main.py", venue.lower(), str(year),
              "--require-complete", "--completeness-level", completeness_level]
    if not download_pdfs:
        scrape.append("--no-pdfs")
    run_command(scrape)

    validate = [
        sys.executable, "postprocessing/validate_year.py", venue.lower(),
        str(year), "--level", completeness_level,
    ]
    if expected_count is not None:
        validate.extend(["--expected-count", str(expected_count)])
    if download_pdfs:
        validate.append("--require-pdfs")
    run_command(validate)

    if update_statistics:
        run_command([
            sys.executable, "postprocessing/generate_statistics.py", "--write"])
        run_command([
            sys.executable, "postprocessing/generate_statistics.py", "--check"])

    return {
        "venue": venue.lower(), "year": year, "status": "validated",
        "download_pdfs": download_pdfs,
        "statistics_updated": update_statistics,
    }
