"""Isolated Prefect imports for the optional P4.2 Mac worker package."""

from __future__ import annotations

from typing import Any, Mapping

from prefect import flow
from prefect.settings import get_current_settings

from automation.job_queue import PREFECT_WORK_POOL_NAME
from automation.mac_worker.runtime import simulate_queue_envelope


class LocalPrefectSettingsProbe:
    """Check local Prefect settings without returning values or making API calls."""

    def is_configured(self, *, work_pool_name: str) -> bool:
        if work_pool_name != PREFECT_WORK_POOL_NAME:
            return False
        settings = get_current_settings()
        return bool(settings.api.url and settings.api.key)


@flow(
    name="openpapers-mac-fixture-job",
    retries=0,
    persist_result=False,
    cache_result_in_memory=False,
    log_prints=False,
)
def openpapers_mac_fixture_job(
    queue_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Prefect entry point that validates and simulates one fake fixture job."""
    return simulate_queue_envelope(queue_envelope).as_dict()
