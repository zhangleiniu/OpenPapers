"""Uninstalled production composition for the agent-driven control path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from automation.agent_run_notifications import deliver_agent_run_email
from automation.agent_credentials import AgentCredentialContext
from automation.agent_worktree_retention import (
    WorktreeRetentionPolicy,
    prune_agent_worktrees,
)
from automation.codex_agent import (
    CodexInvoker,
    CodexRunConfig,
    SubprocessCodexInvoker,
    run_claimed_codex_agent,
)
from automation.configuration import load_venue_catalog
from automation.control_state import ControlStateRepository
from automation.domain import SecretBoundaryError, Writer, assert_secret_free
from automation.due_policy import DuePolicy, claim_due_agent_run
from automation.event_dates import (
    EventDateProvider,
    EventDateTarget,
    initialize_event_dates,
)
from automation.local_service.service import LocalEffectOutcome, LocalEffectStatus
from automation.notifications import NotificationTransport
from automation.resend_notifications import (
    ResendNotificationTransport,
    recipient_fingerprints,
)


DEFAULT_AGENT_TARGETS = (
    Path(__file__).with_name("config") / "agent_targets.v1.json"
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COHORT_TIMEZONE = ZoneInfo("America/Chicago")


class AgentProductionConfigurationError(ValueError):
    """Raised when uninstalled production inputs are not exact and bounded."""


def _cohort_year_applies(lifecycle: Mapping[str, Any], year: int) -> bool:
    """Return whether a periodic venue (e.g. biennial ICCV/ECCV) occurs in year."""
    interval = lifecycle.get("interval_years")
    if interval is None:
        return True
    return (year - lifecycle["cycle_anchor_year"]) % interval == 0


@dataclass(frozen=True)
class AgentProductionConfiguration:
    targets: tuple[EventDateTarget, ...]
    targets_sha256: str
    gemini_project_id: str
    gemini_location: str
    gemini_model: str
    monthly_date_lookup_limit: int
    minimum_free_bytes: int
    codex: CodexRunConfig
    due_policy: DuePolicy
    retention: WorktreeRetentionPolicy
    resend_recipient_sha256s: tuple[str, ...]

    @property
    def resend_recipient_sha256(self) -> str:
        """Compatibility view for legacy single-recipient callers."""
        if len(self.resend_recipient_sha256s) != 1:
            raise AgentProductionConfigurationError(
                "multiple recipient approvals require the plural interface"
            )
        return self.resend_recipient_sha256s[0]


@dataclass(frozen=True, repr=False)
class AgentProductionSecrets:
    resend_api_key: str
    email_from: str
    email_to: str | tuple[str, ...]

    def __post_init__(self) -> None:
        for value, field in (
            (self.resend_api_key, "Resend API key"),
            (self.email_from, "email sender"),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip() \
                    or any(character in value for character in "\r\n\x00"):
                raise AgentProductionConfigurationError(f"{field} is invalid")
        try:
            recipients = tuple(
                sorted(
                    (self.email_to,) if isinstance(self.email_to, str)
                    else tuple(self.email_to),
                    key=str.casefold,
                )
            )
            recipient_fingerprints(recipients)
        except (TypeError, ValueError) as exc:
            raise AgentProductionConfigurationError(
                "email recipients are invalid"
            ) from exc
        object.__setattr__(self, "email_to", recipients)


class NotificationTransportFactory(Protocol):
    def __call__(self) -> NotificationTransport: ...


def load_agent_targets(
    path: Path = DEFAULT_AGENT_TARGETS,
    *,
    today: date | None = None,
) -> tuple[EventDateTarget, ...]:
    """Load explicit targets or expand one bounded annual cohort policy."""
    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentProductionConfigurationError("agent targets are unavailable") from exc
    if raw != json.dumps(payload, indent=2, ensure_ascii=False).encode() + b"\n":
        raise AgentProductionConfigurationError("agent targets are not canonical")
    catalog = load_venue_catalog()
    known = {item["venue_id"] for item in catalog["venues"]}
    lifecycle_by_id = {item["venue_id"]: item["lifecycle"] for item in catalog["venues"]}
    targets: list[EventDateTarget] = []
    if isinstance(payload, dict) and set(payload) == {"schema_version", "targets"} \
            and payload.get("schema_version") == 1 \
            and isinstance(payload.get("targets"), list) and payload["targets"]:
        for item in payload["targets"]:
            if not isinstance(item, dict) or set(item) != {"venue_id", "year"}:
                raise AgentProductionConfigurationError("agent target fields are invalid")
            venue_id, year = item["venue_id"], item["year"]
            if venue_id not in known or not isinstance(year, int) \
                    or isinstance(year, bool) or not 2020 <= year <= 2200:
                raise AgentProductionConfigurationError(
                    "agent target identity is invalid"
                )
            targets.append(EventDateTarget(venue_id, year))
    elif isinstance(payload, dict) and set(payload) == {"schema_version", "cohort"} \
            and payload.get("schema_version") == 2 \
            and isinstance(payload.get("cohort"), dict):
        cohort = payload["cohort"]
        if set(cohort) != {
            "venue_ids", "initial_year", "rollover_month",
            "years_ahead_after_rollover",
        }:
            raise AgentProductionConfigurationError("agent cohort fields are invalid")
        venue_ids = cohort["venue_ids"]
        initial_year = cohort["initial_year"]
        rollover_month = cohort["rollover_month"]
        years_ahead = cohort["years_ahead_after_rollover"]
        resolved_today = today or datetime.now(_COHORT_TIMEZONE).date()
        if not isinstance(resolved_today, date) or isinstance(resolved_today, datetime) \
                or not isinstance(venue_ids, list) or not 1 <= len(venue_ids) <= 100 \
                or any(not isinstance(venue, str) or venue not in known
                       for venue in venue_ids) \
                or venue_ids != sorted(set(venue_ids)) \
                or not isinstance(initial_year, int) or isinstance(initial_year, bool) \
                or not 2020 <= initial_year <= 2200 \
                or not isinstance(rollover_month, int) \
                or isinstance(rollover_month, bool) \
                or not 1 <= rollover_month <= 12 \
                or not isinstance(years_ahead, int) or isinstance(years_ahead, bool) \
                or not 1 <= years_ahead <= 3:
            raise AgentProductionConfigurationError("agent cohort policy is invalid")
        active_year = max(initial_year, resolved_today.year)
        final_year = active_year + (
            years_ahead if resolved_today.month >= rollover_month else 0
        )
        if final_year > 2200:
            raise AgentProductionConfigurationError("agent cohort year is invalid")
        targets.extend(
            EventDateTarget(venue_id, year)
            for venue_id in venue_ids
            for year in range(active_year, final_year + 1)
            if _cohort_year_applies(lifecycle_by_id[venue_id], year)
        )
    else:
        raise AgentProductionConfigurationError("agent targets are invalid")
    resolved = tuple(targets)
    if len(set(resolved)) != len(resolved) or resolved != tuple(sorted(resolved)):
        raise AgentProductionConfigurationError(
            "agent targets must be unique and canonically ordered"
        )
    return resolved


def load_agent_production_configuration(
    payload: Mapping[str, Any],
    *,
    targets_path: Path = DEFAULT_AGENT_TARGETS,
    target_date: date | None = None,
) -> AgentProductionConfiguration:
    """Validate private, credential-free production policy configuration."""
    common = {
        "schema_version", "targets_sha256", "gemini_project_id",
        "gemini_location", "gemini_model", "codex_binary",
        "monthly_date_lookup_limit",
        "codex_timeout_seconds", "codex_max_output_bytes",
        "codex_max_changed_files", "monthly_run_limit",
        "default_not_ready_delay_hours", "minimum_retry_delay_hours",
        "max_suggested_retry_delay_days", "failure_backoff_hours",
        "max_consecutive_failures", "systemic_failure_threshold",
        "systemic_failure_window_hours", "systemic_circuit_delay_hours",
        "minimum_free_bytes", "retention_max_retained",
        "retention_max_age_days", "retention_max_removals_per_run",
    }
    version = payload.get("schema_version") if isinstance(payload, Mapping) else None
    expected = common | ({"resend_recipient_sha256"} if version == 2 else {
        "resend_recipient_sha256s"
    })
    if not isinstance(payload, Mapping) or set(payload) != expected \
            or version not in {2, 3}:
        raise AgentProductionConfigurationError("agent configuration is invalid")
    try:
        assert_secret_free(dict(payload))
    except SecretBoundaryError as exc:
        raise AgentProductionConfigurationError(
            "agent configuration contains credential-shaped data"
        ) from exc
    targets_path = Path(targets_path)
    try:
        targets_bytes = targets_path.read_bytes()
    except OSError as exc:
        raise AgentProductionConfigurationError("agent targets are unavailable") from exc
    targets_hash = hashlib.sha256(targets_bytes).hexdigest()
    if payload["targets_sha256"] != targets_hash:
        raise AgentProductionConfigurationError("agent target fingerprint changed")
    targets = load_agent_targets(targets_path, today=target_date)
    if hashlib.sha256(targets_path.read_bytes()).hexdigest() != targets_hash:
        raise AgentProductionConfigurationError("agent targets changed while loading")
    project = payload["gemini_project_id"]
    location = payload["gemini_location"]
    model = payload["gemini_model"]
    raw_fingerprints = payload.get("resend_recipient_sha256s")
    if version == 3 and not isinstance(raw_fingerprints, list):
        raise AgentProductionConfigurationError("agent adapter identity is invalid")
    fingerprints = ((payload["resend_recipient_sha256"],) if version == 2
                    else tuple(raw_fingerprints))
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (
        project, location, model
    )) or not 1 <= len(fingerprints) <= 10 \
            or tuple(sorted(fingerprints)) != fingerprints \
            or len(set(fingerprints)) != len(fingerprints) or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in fingerprints
            ):
        raise AgentProductionConfigurationError("agent adapter identity is invalid")
    if not isinstance(payload["codex_binary"], str):
        raise AgentProductionConfigurationError("Codex binary path is invalid")
    codex_binary = Path(payload["codex_binary"])
    if not codex_binary.is_absolute() or Path(os.path.normpath(codex_binary)) != codex_binary:
        raise AgentProductionConfigurationError("Codex binary path is invalid")
    try:
        date_limit = payload["monthly_date_lookup_limit"]
        if not isinstance(date_limit, int) or isinstance(date_limit, bool) \
                or not 1 <= date_limit <= 100:
            raise ValueError("monthly date lookup limit is invalid")
        minimum_free_bytes = payload["minimum_free_bytes"]
        if not isinstance(minimum_free_bytes, int) or isinstance(
            minimum_free_bytes, bool
        ) or not 10_000_000_000 <= minimum_free_bytes <= 10_000_000_000_000:
            raise ValueError("minimum free space is invalid")
        codex = CodexRunConfig(
            codex_binary=str(codex_binary),
            timeout_seconds=payload["codex_timeout_seconds"],
            max_output_bytes=payload["codex_max_output_bytes"],
            max_changed_files=payload["codex_max_changed_files"],
        )
        failure_hours = payload["failure_backoff_hours"]
        if not isinstance(failure_hours, list) or not failure_hours or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in failure_hours
        ):
            raise ValueError("failure backoff is invalid")
        due = DuePolicy(
            default_not_ready_delay=timedelta(
                hours=payload["default_not_ready_delay_hours"]
            ),
            minimum_retry_delay=timedelta(
                hours=payload["minimum_retry_delay_hours"]
            ),
            max_suggested_retry_delay=timedelta(
                days=payload["max_suggested_retry_delay_days"]
            ),
            failure_backoff=tuple(timedelta(hours=value) for value in failure_hours),
            max_consecutive_failures=payload["max_consecutive_failures"],
            monthly_run_limit=payload["monthly_run_limit"],
            systemic_failure_threshold=payload["systemic_failure_threshold"],
            systemic_failure_window=timedelta(
                hours=payload["systemic_failure_window_hours"]
            ),
            systemic_circuit_delay=timedelta(
                hours=payload["systemic_circuit_delay_hours"]
            ),
        )
        age_days = payload["retention_max_age_days"]
        if not isinstance(age_days, int) or isinstance(age_days, bool) \
                or not 1 <= age_days <= 365:
            raise ValueError("retention age is invalid")
        retention = WorktreeRetentionPolicy(
            max_retained=payload["retention_max_retained"],
            max_age=timedelta(days=age_days),
            max_removals_per_run=payload["retention_max_removals_per_run"],
        )
    except (TypeError, ValueError) as exc:
        raise AgentProductionConfigurationError("agent policy is invalid") from exc
    return AgentProductionConfiguration(
        targets, targets_hash, project, location, model, date_limit,
        minimum_free_bytes, codex, due, retention, fingerprints,
    )


class AgentProductionEffect:
    """Compose one bounded target-system wakeup with injected effects."""

    def __init__(
        self,
        *,
        repository_root: Path,
        configuration: AgentProductionConfiguration,
        event_date_provider: EventDateProvider,
        codex_invoker: CodexInvoker,
        transport_factory: NotificationTransportFactory,
    ) -> None:
        self._repository_root = Path(repository_root).resolve()
        if not isinstance(configuration, AgentProductionConfiguration):
            raise AgentProductionConfigurationError("agent configuration type is invalid")
        self._configuration = configuration
        self._event_date_provider = event_date_provider
        self._codex_invoker = codex_invoker
        self._transport_factory = transport_factory

    def run(
        self,
        *,
        state_path: Path,
        execution_root: Path,
        scheduled_for: datetime,
        observed_at: datetime,
    ) -> LocalEffectOutcome:
        del scheduled_for
        state_path = Path(state_path)
        execution_root = Path(execution_root).resolve()
        runs_root = execution_root / "agent-runs"
        if execution_root == self._repository_root \
                or execution_root.is_relative_to(self._repository_root) \
                or runs_root == self._repository_root \
                or runs_root.is_relative_to(self._repository_root) \
                or self._repository_root.is_relative_to(runs_root):
            raise AgentProductionConfigurationError(
                "agent execution and managed run roots must be disjoint "
                "from the repository"
            )
        if shutil.disk_usage(execution_root).free \
                < self._configuration.minimum_free_bytes:
            raise AgentProductionConfigurationError(
                "agent execution volume has insufficient free space"
            )
        actions = 0
        initialized = initialize_event_dates(
            state_path,
            self._configuration.targets,
            self._event_date_provider,
            clock=lambda: observed_at,
            selection_limit=1,
            monthly_lookup_limit=self._configuration.monthly_date_lookup_limit,
        )
        actions += initialized.attempted_count + initialized.deferred_count
        run_id = None
        if initialized.attempted_count == 0:
            claimed = claim_due_agent_run(
                state_path, clock=lambda: observed_at,
                policy=self._configuration.due_policy,
            )
            if claimed.claim is not None:
                run_id = claimed.claim.run_id
                run_claimed_codex_agent(
                    state_path,
                    self._repository_root,
                    runs_root,
                    claimed.claim,
                    clock=lambda: observed_at,
                    invoker=self._codex_invoker,
                    policy=self._configuration.due_policy,
                    config=self._configuration.codex,
                )
                actions += 1
        if run_id is None:
            with ControlStateRepository(
                state_path, writer=Writer.LOCAL_CONTROL_PLANE,
                clock=lambda: observed_at,
            ) as repository:
                pending = repository.pending_agent_run_reports(limit=1)
            run_id = pending[0].run_id if pending else None
        if run_id is not None:
            delivery = deliver_agent_run_email(
                state_path, run_id, self._transport_factory(),
                clock=lambda: observed_at,
            )
            actions += int(delivery.attempted)
        removed = prune_agent_worktrees(
            state_path,
            self._repository_root,
            runs_root,
            clock=lambda: observed_at,
            policy=self._configuration.retention,
        )
        actions += len(removed)
        return LocalEffectOutcome(
            LocalEffectStatus.COMPLETED if actions else LocalEffectStatus.NO_DUE_WORK,
            actions,
        )


def build_live_agent_production_effect(
    *,
    repository_root: Path,
    configuration: AgentProductionConfiguration,
    secrets: AgentProductionSecrets,
    credentials: AgentCredentialContext,
) -> AgentProductionEffect:
    """Construct real adapters without invoking them or installing a caller."""
    if recipient_fingerprints(secrets.email_to) \
            != configuration.resend_recipient_sha256s:
        raise AgentProductionConfigurationError("agent recipient approval changed")
    if not isinstance(credentials, AgentCredentialContext):
        raise AgentProductionConfigurationError("agent credential context is invalid")
    from automation.providers.gemini import GeminiEventDateProvider

    provider = GeminiEventDateProvider.from_environment({
        "GCP_PROJECT_ID": configuration.gemini_project_id,
        "AUTOMATION_GEMINI_LOCATION": configuration.gemini_location,
        "AUTOMATION_GEMINI_MODEL": configuration.gemini_model,
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials.google_adc),
    })
    return AgentProductionEffect(
        repository_root=repository_root,
        configuration=configuration,
        event_date_provider=provider,
        codex_invoker=SubprocessCodexInvoker(credentials.codex_environment()),
        transport_factory=lambda: ResendNotificationTransport(
            api_key=secrets.resend_api_key,
            email_from=secrets.email_from,
            email_to=secrets.email_to,
        ),
    )
