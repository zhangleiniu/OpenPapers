"""One-time approximate event-date initialization for local scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from automation.configuration import load_venue_catalog
from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    EventDateScheduleError,
    EventDateScheduleRecord,
    ControlStateRepository,
    LeaseHandle,
)
from automation.discovery import (
    DiscoveryError,
    DiscoveryRequest,
    request_from_catalog,
    safe_error_summary,
)
from automation.domain import Writer


DATE_INITIALIZER_OWNER_ID = "event-date-initializer"
DEFAULT_DATE_RETRY_DELAY = timedelta(days=30)
DEFAULT_EVENT_CHECK_HOUR = 8
DEFAULT_EVENT_CHECK_TIMEZONE = ZoneInfo("America/Chicago")


@dataclass(frozen=True, order=True)
class EventDateTarget:
    """One explicitly configured conference year that needs a schedule."""

    venue_id: str
    year: int


@dataclass(frozen=True)
class EventDateEstimate:
    """A loose date estimate; it is a scheduling hint, not readiness proof."""

    event_date: date | None
    explanation: str


class EventDateProvider(Protocol):
    """Provider boundary for one cheap approximate-date lookup."""

    name: str
    model: str
    prompt_version: str

    def estimate(self, request: DiscoveryRequest) -> EventDateEstimate:
        """Return an approximate date or a bounded no-date result."""


@dataclass(frozen=True)
class EventDateInitializationOutcome:
    """Bounded summary of one local initialization invocation."""

    records: tuple[EventDateScheduleRecord, ...]
    registered_count: int
    attempted_count: int
    scheduled_count: int
    retry_count: int
    deferred_count: int
    fallback_count: int = 0


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _check_time(event_date: date, observed_at: datetime) -> datetime:
    local = datetime.combine(
        event_date,
        time(hour=DEFAULT_EVENT_CHECK_HOUR),
        tzinfo=DEFAULT_EVENT_CHECK_TIMEZONE,
    ).astimezone(timezone.utc)
    return max(local, observed_at)


def _fallback_interval(catalog: Mapping[str, object], venue_id: str) -> int:
    venue = next(
        (item for item in catalog["venues"] if item["venue_id"] == venue_id),
        None,
    )
    if venue is None:
        return 1
    return venue["lifecycle"].get("interval_years") or 1


def _calendar_fallback_date(
    repository: ControlStateRepository, venue_id: str, year: int, interval: int,
) -> date | None:
    """Reuse the prior confirmed date, shifted forward by the venue's cadence.

    Returns None when there is no prior confirmed estimate to shift from
    (e.g. the venue/year has no history) — the caller keeps retrying
    discovery with no fallback in that case, exactly as before this existed.
    """
    prior = repository.get_event_date_schedule(venue_id, year - interval)
    if prior is None or prior.estimated_event_date is None:
        return None
    prior_date = date.fromisoformat(prior.estimated_event_date)
    try:
        return prior_date.replace(year=prior_date.year + interval)
    except ValueError:
        # Feb 29 landing on a non-leap fallback year.
        return prior_date.replace(year=prior_date.year + interval, day=28)


def _ensure_fallback_schedule(
    repository: ControlStateRepository,
    catalog: Mapping[str, object],
    record: EventDateScheduleRecord,
    now: datetime,
    lease: LeaseHandle,
) -> bool:
    """Give a venue/year a real coding-agent schedule despite a failed
    lookup, so it never sits without a next attempt purely because Gemini
    hasn't found (or currently can't find) a date. This never touches
    ``event_date_schedule`` itself — that record honestly stays 'pending'
    and keeps retrying discovery on its own schedule; only a best-effort
    ``agent_schedule`` row is added, keyed off a calendar-projected date.
    """
    interval = _fallback_interval(catalog, record.venue_id)
    fallback = _calendar_fallback_date(
        repository, record.venue_id, record.year, interval
    )
    if fallback is None:
        return False
    repository.ensure_scheduled_agent_target(
        record.venue_id, record.year,
        next_check_at=_check_time(fallback, now),
        registered_at=now,
        lease=lease,
    )
    return True


def _validate_estimate(estimate: EventDateEstimate) -> None:
    if not isinstance(estimate, EventDateEstimate):
        raise ValueError("event-date provider returned an invalid estimate")
    if estimate.event_date is not None and (
        not isinstance(estimate.event_date, date)
        or isinstance(estimate.event_date, datetime)
    ):
        raise ValueError("event-date provider returned an invalid date")
    if (
        not isinstance(estimate.explanation, str)
        or not estimate.explanation.strip()
        or len(estimate.explanation) > 1000
        or any(character in estimate.explanation for character in ("\x00",))
    ):
        raise ValueError("event-date provider returned an invalid explanation")


def initialize_event_dates(
    state_path: Path,
    targets: Sequence[EventDateTarget],
    provider: EventDateProvider,
    *,
    clock: Callable[[], datetime],
    selection_limit: int = 1,
    retry_delay: timedelta = DEFAULT_DATE_RETRY_DELAY,
    monthly_lookup_limit: int = 10,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> EventDateInitializationOutcome:
    """Register targets and initialize only bounded due, still-pending dates."""
    if (
        not isinstance(selection_limit, int)
        or isinstance(selection_limit, bool)
        or selection_limit < 1
    ):
        raise ValueError("selection_limit must be a positive integer")
    if not isinstance(retry_delay, timedelta) or retry_delay <= timedelta(0):
        raise ValueError("retry_delay must be positive")
    if not isinstance(monthly_lookup_limit, int) or isinstance(
        monthly_lookup_limit, bool
    ) or monthly_lookup_limit < 1:
        raise ValueError("monthly_lookup_limit must be a positive integer")
    now = _utc(clock(), field="event-date clock")
    if any(not isinstance(target, EventDateTarget) for target in targets):
        raise ValueError("event-date targets are invalid")
    unique_targets = tuple(sorted(set(targets)))
    catalog = load_venue_catalog()
    requests: dict[EventDateTarget, DiscoveryRequest] = {}
    for target in unique_targets:
        try:
            requests[target] = request_from_catalog(
                catalog, target.venue_id, target.year
            )
        except DiscoveryError as exc:
            raise ValueError(
                f"unknown venue for event-date initialization: {target.venue_id}"
            ) from exc

    registered_count = 0
    attempted_count = 0
    scheduled_count = 0
    retry_count = 0
    deferred_count = 0
    fallback_count = 0
    frozen_clock = lambda: now
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            DATE_INITIALIZER_OWNER_ID,
            ttl_seconds=lease_ttl_seconds,
        )
        try:
            for target in unique_targets:
                registration = repository.register_event_date_target(
                    target.venue_id,
                    target.year,
                    registered_at=now,
                    lease=lease,
                )
                registered_count += int(registration.applied)

            requested_records = [
                repository.get_event_date_schedule(target.venue_id, target.year)
                for target in unique_targets
            ]
            for record in requested_records:
                assert record is not None
                if record.status == "active":
                    raise EventDateScheduleError(
                        "event-date attempt is active or ambiguously interrupted"
                    )
            due = sorted(
                (
                    record for record in requested_records
                    if record is not None
                    and record.status == "pending"
                    and datetime.fromisoformat(
                        record.next_check_at.replace("Z", "+00:00")
                    ) <= now
                ),
                key=lambda record: (
                    record.next_check_at, record.venue_id, record.year
                ),
            )[:selection_limit]
            month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            next_month = (
                month_start.replace(year=month_start.year + 1, month=1)
                if month_start.month == 12
                else month_start.replace(month=month_start.month + 1)
            )
            monthly_count = repository.event_date_attempt_count(
                started_at_or_after=month_start,
                started_before=next_month,
            )
            for record in due:
                if monthly_count >= monthly_lookup_limit:
                    repository.defer_event_date_schedule(
                        record.venue_id,
                        record.year,
                        retry_at=next_month,
                        deferred_at=now,
                        failure_category="monthly_budget",
                        lease=lease,
                    )
                    deferred_count += 1
                    continue
                request = requests[EventDateTarget(record.venue_id, record.year)]
                claim = repository.claim_event_date_attempt(
                    record.venue_id,
                    record.year,
                    provider_name=provider.name,
                    provider_model=provider.model,
                    prompt_version=provider.prompt_version,
                    claimed_at=now,
                    lease=lease,
                )
                attempted_count += 1
                monthly_count += 1
                try:
                    estimate = provider.estimate(request)
                except DiscoveryError as exc:
                    repository.complete_event_date_retry(
                        claim,
                        failure_category=safe_error_summary(exc),
                        completed_at=now,
                        retry_at=now + retry_delay,
                        lease=lease,
                    )
                    retry_count += 1
                    fallback_count += int(_ensure_fallback_schedule(
                        repository, catalog, record, now, lease
                    ))
                    continue
                _validate_estimate(estimate)
                if estimate.event_date is None:
                    repository.complete_event_date_retry(
                        claim,
                        failure_category="date_not_found",
                        completed_at=now,
                        retry_at=now + retry_delay,
                        lease=lease,
                    )
                    retry_count += 1
                    fallback_count += int(_ensure_fallback_schedule(
                        repository, catalog, record, now, lease
                    ))
                    continue
                repository.complete_event_date_success(
                    claim,
                    estimated_event_date=estimate.event_date.isoformat(),
                    estimated_at=now,
                    next_check_at=_check_time(estimate.event_date, now),
                    lease=lease,
                )
                scheduled_count += 1
            resolved_records = [
                repository.get_event_date_schedule(target.venue_id, target.year)
                for target in unique_targets
            ]
            assert all(record is not None for record in resolved_records)
            records = tuple(
                record for record in resolved_records if record is not None
            )
        finally:
            repository.release_lease(lease)
    return EventDateInitializationOutcome(
        records=records,
        registered_count=registered_count,
        attempted_count=attempted_count,
        scheduled_count=scheduled_count,
        retry_count=retry_count,
        deferred_count=deferred_count,
        fallback_count=fallback_count,
    )
