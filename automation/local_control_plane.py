"""Fixture-only P4.L2 composition for one bounded local control wakeup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from automation.configuration import load_policy_config, load_venue_catalog
from automation.contracts import ContractName, validate_contract
from automation.control_plane import (
    VerificationConsumptionOutcome,
    consume_verification_record,
)
from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_SCHEDULER_SELECTION_LIMIT,
    ControlStateRepository,
    DueWorkSelection,
    LeaseHandle,
    SchedulerWakeupOutcome,
)
from automation.discovery import DiscoveryRequest, request_from_catalog
from automation.domain import Writer
from automation.lifecycle import ActionIntent
from automation.local_scheduler import (
    LOCAL_SCHEDULER_OWNER_ID,
    scheduler_wakeup_id,
)
from automation.notification_integration import (
    ActionIntegrationOutcome,
    DigestIntegrationOutcome,
    integrate_action_intents,
    persist_due_digest_shadow,
)
from automation.verification import validate_verification_result


DEFAULT_VERIFICATION_BUNDLE_LIMIT = 16
MAX_VERIFICATION_BUNDLE_LIMIT = 100


class LocalControlCompositionError(ValueError):
    """Raised when injected evidence cannot safely complete selected work."""


@dataclass(frozen=True)
class VerificationBundle:
    """One strict request/result pair returned by an injected fake verifier."""

    request: Mapping[str, Any]
    result: Mapping[str, Any]


class DiscoveryEffect(Protocol):
    """Narrow fixture boundary for one catalog-bounded discovery result."""

    def discover(self, request: DiscoveryRequest) -> Mapping[str, Any]:
        """Return one strict discovery result without changing control state."""


class VerificationEffect(Protocol):
    """Narrow fixture boundary for deterministic verification artifacts."""

    def verify(
        self,
        discovery: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> Sequence[VerificationBundle]:
        """Return bounded strict bundles without applying actions."""


@dataclass(frozen=True)
class SelectionCompositionOutcome:
    """Persistent reductions and inert actions for one due selection."""

    selection: DueWorkSelection
    verification_ids: tuple[str, ...]
    consumptions: tuple[VerificationConsumptionOutcome, ...]
    action_integration: tuple[ActionIntegrationOutcome, ...]
    actions: tuple[ActionIntent, ...]
    final_state_revision: int


@dataclass(frozen=True)
class LocalControlWakeupOutcome:
    """Bounded result of a first composition or an effect-free exact replay."""

    scheduler: SchedulerWakeupOutcome
    selections: tuple[SelectionCompositionOutcome, ...]
    digest: DigestIntegrationOutcome | None
    replayed: bool


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise LocalControlCompositionError(
            f"{field} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _artifact_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise LocalControlCompositionError(f"{field} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalControlCompositionError(f"{field} is not a valid timestamp") from exc
    return _utc(parsed, field=field)


def _bundle_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_VERIFICATION_BUNDLE_LIMIT
    ):
        raise LocalControlCompositionError(
            "verification bundle limit is outside the supported range"
        )
    return value


def _validate_discovery(
    discovery: Mapping[str, Any],
    request: DiscoveryRequest,
    observed_at: datetime,
) -> dict[str, Any]:
    if not isinstance(discovery, Mapping):
        raise LocalControlCompositionError(
            "discovery effect must return a mapping"
        )
    payload = dict(discovery)
    validate_contract(ContractName.DISCOVERY_RESULT, payload)
    if (
        payload["venue_id"] != request.venue_id
        or payload["year"] != request.year
    ):
        raise LocalControlCompositionError(
            "discovery result identity does not match selected work"
        )
    if _artifact_time(payload["checked_at"], field="discovery checked_at") > observed_at:
        raise LocalControlCompositionError(
            "discovery result cannot be observed in the future"
        )
    return payload


def _verification_bundles(
    effect: VerificationEffect,
    discovery: Mapping[str, Any],
    *,
    observed_at: datetime,
    limit: int,
) -> tuple[VerificationBundle, ...]:
    supplied = effect.verify(discovery, observed_at=observed_at)
    if isinstance(supplied, (str, bytes)) or not isinstance(supplied, Sequence):
        raise LocalControlCompositionError(
            "verification effect must return a sequence of bundles"
        )
    bundles = tuple(supplied)
    if not bundles:
        raise LocalControlCompositionError(
            "verification effect returned no deterministic evidence"
        )
    if len(bundles) > limit:
        raise LocalControlCompositionError(
            "verification effect exceeded the bundle limit"
        )
    seen: set[str] = set()
    for bundle in bundles:
        if not isinstance(bundle, VerificationBundle):
            raise LocalControlCompositionError(
                "verification effect returned an untyped bundle"
            )
        validate_verification_result(
            bundle.result, bundle.request, discovery
        )
        verification_id = bundle.result["verification_id"]
        if verification_id in seen:
            raise LocalControlCompositionError(
                "verification effect returned a duplicate identity"
            )
        seen.add(verification_id)
        if _artifact_time(
            bundle.result["verified_at"], field="verification verified_at"
        ) > observed_at:
            raise LocalControlCompositionError(
                "verification result cannot be observed in the future"
            )
    return bundles


def _compose_selection(
    repository: ControlStateRepository,
    selection: DueWorkSelection,
    *,
    discovery_effect: DiscoveryEffect,
    verification_effect: VerificationEffect,
    catalog: Mapping[str, Any],
    policy: Mapping[str, Any],
    lease: LeaseHandle,
    observed_at: datetime,
    bundle_limit: int,
    wakeup_id: str,
) -> SelectionCompositionOutcome:
    current = repository.get_conference_state(selection.venue_id, selection.year)
    if (
        current is None
        or current.revision != selection.state_revision
        or current.state_fingerprint != selection.state_fingerprint
        or current.state["next_check_at"] != selection.next_check_at
    ):
        raise LocalControlCompositionError(
            "due selection no longer matches its retained conference state"
        )

    discovery_request = request_from_catalog(
        catalog, selection.venue_id, selection.year
    )
    discovery = _validate_discovery(
        discovery_effect.discover(discovery_request),
        discovery_request,
        observed_at,
    )
    bundles = _verification_bundles(
        verification_effect,
        discovery,
        observed_at=observed_at,
        limit=bundle_limit,
    )

    consumptions: list[VerificationConsumptionOutcome] = []
    integrations: list[ActionIntegrationOutcome] = []
    actions: list[ActionIntent] = []
    verification_ids: list[str] = []
    for bundle in bundles:
        repository.accept_verification(
            discovery,
            bundle.request,
            bundle.result,
            lease=lease,
            received_at=observed_at,
        )
        record = next(
            item
            for item in repository.replay_verifications(
                venue_id=selection.venue_id,
                year=selection.year,
            )
            if item.result["verification_id"]
            == bundle.result["verification_id"]
        )
        consumption = consume_verification_record(
            repository,
            record,
            catalog=catalog,
            policy=policy,
            lease=lease,
        )
        integration = integrate_action_intents(
            repository,
            consumption.reduction.actions,
            lease=lease,
            occurred_at=bundle.result["verified_at"],
            run_ids=(wakeup_id, selection.selection_id),
        )
        verification_ids.append(bundle.result["verification_id"])
        consumptions.append(consumption)
        integrations.append(integration)
        actions.extend(consumption.reduction.actions)

    final = repository.get_conference_state(selection.venue_id, selection.year)
    if final is None or final.state["next_check_at"] == selection.next_check_at:
        raise LocalControlCompositionError(
            "completed domain work did not advance the selected schedule"
        )
    return SelectionCompositionOutcome(
        selection=selection,
        verification_ids=tuple(verification_ids),
        consumptions=tuple(consumptions),
        action_integration=tuple(integrations),
        actions=tuple(actions),
        final_state_revision=final.revision,
    )


def run_local_control_wakeup(
    state_path: Path,
    *,
    scheduled_for: datetime,
    clock: Callable[[], datetime],
    discovery_effect: DiscoveryEffect,
    verification_effect: VerificationEffect,
    catalog: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    selection_limit: int = DEFAULT_SCHEDULER_SELECTION_LIMIT,
    verification_bundle_limit: int = DEFAULT_VERIFICATION_BUNDLE_LIMIT,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> LocalControlWakeupOutcome:
    """Compose one fake-only local wakeup and complete it after durable work."""
    observed_at = _utc(clock(), field="local control clock")
    scheduled = _utc(scheduled_for, field="scheduled_for")
    if scheduled > observed_at:
        raise LocalControlCompositionError(
            "scheduled_for cannot be later than the local control clock"
        )
    if not callable(getattr(discovery_effect, "discover", None)):
        raise LocalControlCompositionError(
            "discovery effect must provide discover()"
        )
    if not callable(getattr(verification_effect, "verify", None)):
        raise LocalControlCompositionError(
            "verification effect must provide verify()"
        )
    bundle_limit = _bundle_limit(verification_bundle_limit)
    resolved_catalog = catalog if catalog is not None else load_venue_catalog()
    resolved_policy = policy if policy is not None else load_policy_config()
    validate_contract(ContractName.VENUE_CATALOG, resolved_catalog)
    validate_contract(ContractName.POLICY_CONFIG, resolved_policy)
    frozen_clock = lambda: observed_at
    wakeup_id = scheduler_wakeup_id(scheduled)

    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            LOCAL_SCHEDULER_OWNER_ID,
            ttl_seconds=lease_ttl_seconds,
        )
        try:
            start = repository.begin_scheduler_wakeup(
                wakeup_id,
                scheduled_for=scheduled,
                due_cutoff_at=observed_at,
                selection_limit=selection_limit,
                lease=lease,
            )
            if not start.applied and start.record.status == "completed":
                replay = repository.finish_scheduler_wakeup(
                    wakeup_id,
                    lease=lease,
                    completed_at=observed_at,
                )
                return LocalControlWakeupOutcome(
                    scheduler=replay,
                    selections=(),
                    digest=None,
                    replayed=True,
                )

            plan = repository.plan_scheduler_wakeup(
                wakeup_id,
                lease=lease,
                selected_at=observed_at,
            )
            selections = tuple(
                _compose_selection(
                    repository,
                    selection,
                    discovery_effect=discovery_effect,
                    verification_effect=verification_effect,
                    catalog=resolved_catalog,
                    policy=resolved_policy,
                    lease=lease,
                    observed_at=observed_at,
                    bundle_limit=bundle_limit,
                    wakeup_id=wakeup_id,
                )
                for selection in plan.selections
            )
            digest = persist_due_digest_shadow(
                repository,
                policy=resolved_policy,
                lease=lease,
                now=observed_at,
                run_ids=(wakeup_id,),
            )
            completed = repository.finish_scheduler_wakeup(
                wakeup_id,
                lease=lease,
                completed_at=observed_at,
            )
            return LocalControlWakeupOutcome(
                scheduler=completed,
                selections=selections,
                digest=digest,
                replayed=False,
            )
        finally:
            repository.release_lease(lease)
