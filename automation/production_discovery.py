"""P2.6: guarded, durable automatic discovery effect (fixture/fake-only).

This module builds a production-capable ``DiscoveryEffect`` (see
``automation/local_control_plane.py``) around the existing provider-neutral
``DiscoveryService``, the Gemini Search Grounding adapter, the process-safe
daily attempt ``JsonBudgetLedger``, and ``ArtifactStore``. It adds one new
durable, process-safe automatic-discovery health ledger that answers a
different question than the attempt ledger: not "may another metered remote
call be reserved today?" but "may automatic discovery attempt this venue or
provider now, given recent typed failures?"

The health ledger tracks two independent guardrails:

- a same-venue/same-fingerprint cooldown that blocks one venue after a typed
  failure until an explicit deadline, surviving process restart; and
- a systemic circuit that opens once a configured number of *distinct*
  venues report the same closed provider/infrastructure/output-shape failure
  fingerprint, guarding against one real provider outage rather than
  unrelated per-venue content problems.

Both checks occur before any production provider is constructed or any
budget is reserved. Nothing in this module is installed or called by a
production caller; it is exercised only by tests using fake provider
factories and temporary private roots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.configuration import load_policy_config
from automation.contracts import ContractName, artifact_fingerprint, validate_contract
from automation.discovery import (
    ArtifactStore,
    BudgetExceeded,
    BudgetLimits,
    DiscoveryError,
    DiscoveryProvider,
    DiscoveryRequest,
    DiscoveryService,
    DiscoveryStorageError,
    DiscoveryValidationError,
    JsonBudgetLedger,
    ProviderError,
    _atomic_write,
    _exclusive_lock,
    _read_object,
    format_datetime,
    parse_datetime,
    utc_now,
)
from automation.providers.gemini import GeminiSearchGroundingProvider


HEALTH_LEDGER_VERSION = 1
MAX_VENUE_ENTRIES = 500
MAX_SYSTEMIC_EVENTS = 500

# Closed set of ``DiscoveryError`` categories that describe a provider or
# infrastructure problem independent of any single venue's registered
# domains or content. Venue-specific validation categories (mismatched
# venue/year, unregistered source domains, unsupported claim/milestone
# evidence, milestone scope/year mismatches, missing per-status support, and
# confidence/uncertainty inconsistency) are deliberately excluded: three
# unrelated venue-specific content problems are not evidence of one outage.
SYSTEMIC_ELIGIBLE_CATEGORIES = frozenset({
    "search_api_transient",
    "search_api_failure",
    "structure_api_transient",
    "structure_api_failure",
    "no_response_candidate",
    "missing_grounding_metadata",
    "missing_grounding_sources",
    "missing_grounding_supports",
    "missing_grounded_report",
    "malformed_structured_output",
    "configuration_missing_project",
    "dependency_missing",
    "provider_error",
    "body_shape_mismatch",
    "contract_rejected",
})


class AutomaticDiscoveryError(RuntimeError):
    """Base class for guarded-effect failures raised by this module."""


class AutomaticDiscoveryLedgerError(AutomaticDiscoveryError):
    """Raised when the automatic-discovery health ledger is unsafe or corrupt."""


class AutomaticDiscoveryRefused(AutomaticDiscoveryError):
    """Raised before any provider construction or budget reservation."""

    def __init__(self, message: str, *, reason: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_at = retry_at


def _require_aware(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AutomaticDiscoveryGuardPolicy:
    """Durable guard durations resolved from the versioned policy config."""

    same_failure_cooldown_hours: int
    systemic_circuit_hours: int
    venue_failure_threshold: int

    def __post_init__(self) -> None:
        if self.same_failure_cooldown_hours < 1:
            raise ValueError("same_failure_cooldown_hours must be positive")
        if self.systemic_circuit_hours < 1:
            raise ValueError("systemic_circuit_hours must be positive")
        if self.venue_failure_threshold < 2:
            raise ValueError("venue_failure_threshold must be at least 2")

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any]) -> "AutomaticDiscoveryGuardPolicy":
        """Build the guard policy, requiring the optional automatic block."""
        validate_contract(ContractName.POLICY_CONFIG, policy)
        automatic = policy.get("automatic_discovery")
        if not isinstance(automatic, Mapping):
            raise ValueError(
                "policy is missing the automatic_discovery guard configuration "
                "required for automatic discovery"
            )
        systemic = policy["systemic_failure"]
        return cls(
            same_failure_cooldown_hours=automatic["same_failure_cooldown_hours"],
            systemic_circuit_hours=automatic["systemic_circuit_hours"],
            venue_failure_threshold=systemic["venue_failure_threshold"],
        )


def _error_category(error: DiscoveryError) -> str:
    if isinstance(error, ProviderError):
        return error.category
    if isinstance(error, DiscoveryValidationError):
        return error.category
    return type(error).__name__


def _occurrence_fingerprint(
    *,
    venue_id: str,
    year: int,
    provider: str,
    model: str,
    category: str,
    status: int | None,
) -> str:
    """Fingerprint one venue's typed failure, including its identity."""
    return artifact_fingerprint({
        "venue_id": venue_id,
        "year": year,
        "provider": provider,
        "model": model,
        "category": category,
        "status": status,
    })


def _systemic_fingerprint(
    *,
    provider: str,
    model: str,
    category: str,
    status: int | None,
) -> str | None:
    """Fingerprint a closed systemic identity, or ``None`` when ineligible."""
    if category not in SYSTEMIC_ELIGIBLE_CATEGORIES:
        return None
    return artifact_fingerprint({
        "provider": provider,
        "model": model,
        "category": category,
        "status": status,
    })


class AutomaticDiscoveryHealthLedger:
    """Durable, process-safe automatic-discovery cooldown and circuit state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": HEALTH_LEDGER_VERSION,
            "venues": {},
            "circuit": None,
            "systemic_events": [],
        }

    def _load(self) -> dict[str, Any]:
        ledger = _read_object(self.path, default=self._default())
        self._validate_shape(ledger)
        return ledger

    def _validate_shape(self, ledger: Mapping[str, Any]) -> None:
        if ledger.get("version") != HEALTH_LEDGER_VERSION:
            raise AutomaticDiscoveryLedgerError(
                "automatic-discovery health ledger version is unsupported")
        venues = ledger.get("venues")
        if not isinstance(venues, dict):
            raise AutomaticDiscoveryLedgerError(
                "health ledger venues must be an object")
        for venue_id, entry in venues.items():
            if not isinstance(venue_id, str) or not isinstance(entry, dict):
                raise AutomaticDiscoveryLedgerError(
                    "health ledger venue entry is invalid")
            if entry.get("state") not in {"in_flight", "cooldown", "eligible"}:
                raise AutomaticDiscoveryLedgerError(
                    f"health ledger state for {venue_id} is invalid")
            if set(entry) != {
                "state", "observed_at", "deadline_at",
                "occurrence_fingerprint", "systemic_fingerprint", "category",
            }:
                raise AutomaticDiscoveryLedgerError(
                    f"health ledger entry for {venue_id} has unknown fields")
            parse_datetime(entry["observed_at"])
            if entry["deadline_at"] is not None:
                parse_datetime(entry["deadline_at"])
        circuit = ledger.get("circuit")
        if circuit is not None:
            if not isinstance(circuit, dict) or set(circuit) != {
                "systemic_fingerprint", "venues", "opened_at", "deadline_at",
            }:
                raise AutomaticDiscoveryLedgerError(
                    "health ledger circuit fields are invalid")
            if (
                not isinstance(circuit["venues"], list)
                or not circuit["venues"]
                or any(not isinstance(v, str) for v in circuit["venues"])
            ):
                raise AutomaticDiscoveryLedgerError(
                    "health ledger circuit venues are invalid")
            parse_datetime(circuit["opened_at"])
            parse_datetime(circuit["deadline_at"])
        events = ledger.get("systemic_events")
        if not isinstance(events, list):
            raise AutomaticDiscoveryLedgerError(
                "health ledger systemic_events must be a list")
        for event in events:
            if not isinstance(event, dict) or set(event) != {
                "venue_id", "systemic_fingerprint", "observed_at", "expires_at",
            }:
                raise AutomaticDiscoveryLedgerError(
                    "health ledger systemic event is invalid")
            parse_datetime(event["observed_at"])
            parse_datetime(event["expires_at"])

    def _bound(self, ledger: dict[str, Any]) -> None:
        venues = ledger["venues"]
        if len(venues) > MAX_VENUE_ENTRIES:
            ordered = sorted(venues.items(), key=lambda item: item[1]["observed_at"])
            for venue_id, _ in ordered[: len(venues) - MAX_VENUE_ENTRIES]:
                del venues[venue_id]
        events = ledger["systemic_events"]
        if len(events) > MAX_SYSTEMIC_EVENTS:
            events[:] = events[-MAX_SYSTEMIC_EVENTS:]

    def guard_and_claim(
        self,
        venue_id: str,
        *,
        at: datetime,
        policy: AutomaticDiscoveryGuardPolicy,
    ) -> None:
        """Refuse before I/O, or durably claim one in-flight attempt."""
        resolved_at = _require_aware(at, field="at")
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            circuit = ledger.get("circuit")
            if (
                circuit is not None
                and parse_datetime(circuit["deadline_at"]) > resolved_at
            ):
                raise AutomaticDiscoveryRefused(
                    "automatic discovery systemic circuit is open",
                    reason="systemic_circuit_open",
                    retry_at=parse_datetime(circuit["deadline_at"]),
                )
            entry = ledger["venues"].get(venue_id)
            if entry is not None and entry["state"] in {"in_flight", "cooldown"}:
                deadline = entry["deadline_at"]
                if deadline is not None and parse_datetime(deadline) > resolved_at:
                    reason = (
                        "same_venue_in_flight" if entry["state"] == "in_flight"
                        else "same_venue_cooldown"
                    )
                    raise AutomaticDiscoveryRefused(
                        f"automatic discovery is refused for {venue_id!r}",
                        reason=reason,
                        retry_at=parse_datetime(deadline),
                    )
            ledger["venues"][venue_id] = {
                "state": "in_flight",
                "observed_at": format_datetime(resolved_at),
                "deadline_at": format_datetime(
                    resolved_at
                    + timedelta(hours=policy.same_failure_cooldown_hours)
                ),
                "occurrence_fingerprint": None,
                "systemic_fingerprint": None,
                "category": None,
            }
            self._bound(ledger)
            _atomic_write(self.path, ledger)

    def _finalize_eligible(self, venue_id: str, *, at: datetime) -> None:
        resolved_at = _require_aware(at, field="at")
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            ledger["venues"][venue_id] = {
                "state": "eligible",
                "observed_at": format_datetime(resolved_at),
                "deadline_at": None,
                "occurrence_fingerprint": None,
                "systemic_fingerprint": None,
                "category": None,
            }
            self._bound(ledger)
            _atomic_write(self.path, ledger)

    def finalize_success(self, venue_id: str, *, at: datetime) -> None:
        """Clear a venue's claim after a successful discovery outcome."""
        self._finalize_eligible(venue_id, at=at)

    def finalize_guard_skip(self, venue_id: str, *, at: datetime) -> None:
        """Clear a venue's claim after a guard decision, not a health event.

        Budget exhaustion is a guard decision, not a systemic provider
        failure: it reflects today's spend, not provider health, so it must
        not start a cooldown or contribute to the systemic circuit.
        """
        self._finalize_eligible(venue_id, at=at)

    def finalize_failure(
        self,
        venue_id: str,
        *,
        occurrence_fingerprint: str,
        systemic_fingerprint: str | None,
        category: str,
        at: datetime,
        policy: AutomaticDiscoveryGuardPolicy,
    ) -> None:
        """Record a typed failure, opening the cooldown and possibly circuit."""
        resolved_at = _require_aware(at, field="at")
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            deadline = resolved_at + timedelta(
                hours=policy.same_failure_cooldown_hours)
            ledger["venues"][venue_id] = {
                "state": "cooldown",
                "observed_at": format_datetime(resolved_at),
                "deadline_at": format_datetime(deadline),
                "occurrence_fingerprint": occurrence_fingerprint,
                "systemic_fingerprint": systemic_fingerprint,
                "category": category,
            }
            if systemic_fingerprint is not None:
                events: list[dict[str, Any]] = ledger["systemic_events"]
                events.append({
                    "venue_id": venue_id,
                    "systemic_fingerprint": systemic_fingerprint,
                    "observed_at": format_datetime(resolved_at),
                    "expires_at": format_datetime(deadline),
                })
                active_venues = sorted({
                    event["venue_id"] for event in events
                    if event["systemic_fingerprint"] == systemic_fingerprint
                    and parse_datetime(event["expires_at"]) > resolved_at
                })
                existing_circuit = ledger.get("circuit")
                circuit_still_active = (
                    existing_circuit is not None
                    and parse_datetime(existing_circuit["deadline_at"])
                    > resolved_at
                    and existing_circuit["systemic_fingerprint"]
                    == systemic_fingerprint
                )
                if (
                    len(active_venues) >= policy.venue_failure_threshold
                    and not circuit_still_active
                ):
                    ledger["circuit"] = {
                        "systemic_fingerprint": systemic_fingerprint,
                        "venues": active_venues,
                        "opened_at": format_datetime(resolved_at),
                        "deadline_at": format_datetime(
                            resolved_at
                            + timedelta(hours=policy.systemic_circuit_hours)
                        ),
                    }
            self._bound(ledger)
            _atomic_write(self.path, ledger)

    def venue_state(self, venue_id: str) -> Mapping[str, Any] | None:
        """Return a defensive copy of one venue's retained state, for tests."""
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
        entry = ledger["venues"].get(venue_id)
        return dict(entry) if entry is not None else None

    def circuit_state(self) -> Mapping[str, Any] | None:
        """Return a defensive copy of the retained circuit state, for tests."""
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
        circuit = ledger.get("circuit")
        return dict(circuit) if circuit is not None else None


@dataclass(frozen=True)
class AutomaticDiscoveryConfig:
    """Explicit trusted configuration for the guarded production effect."""

    artifact_root: Path
    budget_ledger_path: Path
    health_ledger_path: Path
    project: str
    location: str
    model: str
    limits: BudgetLimits
    guard_policy: AutomaticDiscoveryGuardPolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_root", Path(self.artifact_root))
        object.__setattr__(
            self, "budget_ledger_path", Path(self.budget_ledger_path))
        object.__setattr__(
            self, "health_ledger_path", Path(self.health_ledger_path))
        if not self.project:
            raise ValueError("automatic discovery config requires a project")
        if not self.location:
            raise ValueError("automatic discovery config requires a location")
        if not self.model:
            raise ValueError("automatic discovery config requires a model")

    @classmethod
    def from_policy(
        cls,
        policy: Mapping[str, Any] | None = None,
        *,
        artifact_root: Path,
        budget_ledger_path: Path,
        health_ledger_path: Path,
        project: str,
        location: str,
        model: str,
    ) -> "AutomaticDiscoveryConfig":
        """Build config from validated policy plus explicit private roots."""
        resolved_policy = policy if policy is not None else load_policy_config()
        return cls(
            artifact_root=artifact_root,
            budget_ledger_path=budget_ledger_path,
            health_ledger_path=health_ledger_path,
            project=project,
            location=location,
            model=model,
            limits=BudgetLimits.from_policy(resolved_policy),
            guard_policy=AutomaticDiscoveryGuardPolicy.from_policy(resolved_policy),
        )


class ProductionDiscoveryEffect:
    """Guarded, production-capable ``DiscoveryEffect`` for automatic wakeups.

    ``discover()`` satisfies the narrow ``DiscoveryEffect`` protocol in
    ``automation/local_control_plane.py``: given a ``DiscoveryRequest`` it
    returns one strict discovery-result mapping or raises. Before any
    production provider is constructed or budget is reserved, it durably
    checks and claims the automatic-discovery health ledger; after the
    underlying ``DiscoveryService`` call returns or raises, it finalizes
    that claim as eligible, a guard skip, or a typed cooldown/circuit
    failure.
    """

    def __init__(
        self,
        config: AutomaticDiscoveryConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        _provider_factory: Callable[[], DiscoveryProvider] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._artifact_store = ArtifactStore(config.artifact_root)
        self._budget_ledger = JsonBudgetLedger(config.budget_ledger_path)
        self._health = AutomaticDiscoveryHealthLedger(config.health_ledger_path)
        # Provider-factory injection is test-only/internal: the production
        # path always resolves to the one fixed Gemini construction below,
        # never a caller-supplied provider, path, or callback.
        self._provider_factory = _provider_factory or self._construct_provider

    def _construct_provider(self) -> DiscoveryProvider:
        environ = {
            "GCP_PROJECT_ID": self._config.project,
            "AUTOMATION_GEMINI_LOCATION": self._config.location,
            "AUTOMATION_GEMINI_MODEL": self._config.model,
        }
        return GeminiSearchGroundingProvider.from_environment(environ)

    def discover(self, request: DiscoveryRequest) -> Mapping[str, Any]:
        """Return one strict discovery result, guarded by durable health state."""
        at = _require_aware(self._clock(), field="clock")
        self._health.guard_and_claim(
            request.venue_id, at=at, policy=self._config.guard_policy)

        provider = self._provider_factory()
        service = DiscoveryService(
            provider,
            self._artifact_store,
            self._budget_ledger,
            self._config.limits,
            clock=self._clock,
        )
        try:
            outcome = service.discover(request)
        except BudgetExceeded:
            self._health.finalize_guard_skip(
                request.venue_id, at=_require_aware(self._clock(), field="clock"))
            raise
        except DiscoveryStorageError:
            raise
        except DiscoveryError as exc:
            finalize_at = _require_aware(self._clock(), field="clock")
            category = _error_category(exc)
            status = getattr(exc, "status_code", None)
            occurrence = _occurrence_fingerprint(
                venue_id=request.venue_id,
                year=request.year,
                provider=provider.name,
                model=provider.model,
                category=category,
                status=status,
            )
            systemic = _systemic_fingerprint(
                provider=provider.name,
                model=provider.model,
                category=category,
                status=status,
            )
            self._health.finalize_failure(
                request.venue_id,
                occurrence_fingerprint=occurrence,
                systemic_fingerprint=systemic,
                category=category,
                at=finalize_at,
                policy=self._config.guard_policy,
            )
            raise
        self._health.finalize_success(
            request.venue_id, at=_require_aware(self._clock(), field="clock"))
        return dict(outcome.primary.result)
