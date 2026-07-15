"""P2.8: the fixed automatic verifier/action-source wakeup composition.

This module is the actual automatic gate: it composes the accepted P2.6
``ProductionDiscoveryEffect`` and P2.7 ``ProductionVerificationEffect``
through the existing ``automation.local_control_plane.run_local_control_wakeup``
boundary, which in turn drives P2.5 lifecycle reduction and P5.5 execution
retention. A caller supplies only explicit private storage roots and Gemini
identity; it can never substitute a different discovery/verification effect,
inject a synthetic action or job, or reach P5.4 execution/P5.5 dispatch.

Nothing here is imported by or connected to
``automation/local_service/production.py``. The module is fixture/fake-only
in its own tests (which inject a fake provider/fetcher through the same
private construction seams P2.6/P2.7 already expose) and is not installed or
scheduled anywhere. Completion satisfies only the implementation half of
P5.5S's automatic verifier/action-source prerequisite; P2.8S separately
supplies the live-evidence half.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.configuration import load_policy_config, load_venue_catalog
from automation.control_state import DEFAULT_LEASE_TTL_SECONDS, DEFAULT_SCHEDULER_SELECTION_LIMIT
from automation.discovery import utc_now
from automation.local_control_plane import (
    DEFAULT_VERIFICATION_BUNDLE_LIMIT,
    LocalControlWakeupOutcome,
    run_local_control_wakeup,
)
from automation.production_discovery import AutomaticDiscoveryConfig, ProductionDiscoveryEffect
from automation.production_verification import (
    PRODUCTION_CRAWL_POLICY_PATH,
    AutomaticVerificationConfig,
    ProductionVerificationEffect,
)


class ProductionControlPlaneConfigError(ValueError):
    """Raised when the explicit private wakeup configuration is unsafe."""


def _absolute(path: Path, *, field: str) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        raise ProductionControlPlaneConfigError(f"{field} must be an absolute path")
    if Path(os.path.normpath(resolved)) != resolved:
        raise ProductionControlPlaneConfigError(f"{field} must be a normalized path")
    return resolved


@dataclass(frozen=True)
class ProductionControlPlaneConfig:
    """Explicit private roots and Gemini identity for the one fixed wakeup.

    Every discovery/verification storage path is derived from
    ``automation_root`` so a caller can configure *where* private state
    lives but never steer it to a path chosen by web or model content; only
    ``control_state_path`` and ``automation_root`` themselves are supplied
    directly, and both must be normalized, absolute, and disjoint.
    """

    control_state_path: Path
    automation_root: Path
    gemini_project: str
    gemini_location: str
    gemini_model: str
    policy_review_path: Path = PRODUCTION_CRAWL_POLICY_PATH

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "control_state_path",
            _absolute(self.control_state_path, field="control_state_path"),
        )
        object.__setattr__(
            self, "automation_root",
            _absolute(self.automation_root, field="automation_root"),
        )
        object.__setattr__(
            self, "policy_review_path",
            _absolute(self.policy_review_path, field="policy_review_path"),
        )
        if (
            self.control_state_path == self.automation_root
            or self.control_state_path.is_relative_to(self.automation_root)
            or self.automation_root.is_relative_to(self.control_state_path)
        ):
            raise ProductionControlPlaneConfigError(
                "control state and automation-effect roots must be disjoint"
            )
        if not self.gemini_project:
            raise ProductionControlPlaneConfigError("gemini_project is required")
        if not self.gemini_location:
            raise ProductionControlPlaneConfigError("gemini_location is required")
        if not self.gemini_model:
            raise ProductionControlPlaneConfigError("gemini_model is required")

    @property
    def discovery_artifact_root(self) -> Path:
        return self.automation_root / "discovery"

    @property
    def discovery_budget_ledger_path(self) -> Path:
        return self.automation_root / "discovery-budget.v1.json"

    @property
    def discovery_health_ledger_path(self) -> Path:
        return self.automation_root / "discovery-health.v1.json"

    @property
    def verification_snapshot_root(self) -> Path:
        return self.automation_root / "verification-snapshots"

    @property
    def verification_health_ledger_path(self) -> Path:
        return self.automation_root / "verification-health.v1.json"

    def discovery_config(self, policy: Mapping[str, Any]) -> AutomaticDiscoveryConfig:
        return AutomaticDiscoveryConfig.from_policy(
            policy,
            artifact_root=self.discovery_artifact_root,
            budget_ledger_path=self.discovery_budget_ledger_path,
            health_ledger_path=self.discovery_health_ledger_path,
            project=self.gemini_project,
            location=self.gemini_location,
            model=self.gemini_model,
        )

    def verification_config(self) -> AutomaticVerificationConfig:
        return AutomaticVerificationConfig(
            snapshot_root=self.verification_snapshot_root,
            health_ledger_path=self.verification_health_ledger_path,
            policy_review_path=self.policy_review_path,
        )


def build_production_effects(
    config: ProductionControlPlaneConfig,
    *,
    clock: Callable[[], datetime],
    policy: Mapping[str, Any],
    catalog: Mapping[str, Any] | None,
    _discovery_provider_factory=None,
    _verification_fetcher=None,
) -> tuple[ProductionDiscoveryEffect, ProductionVerificationEffect]:
    """Construct the one fixed production discovery/verification pair.

    The two leading-underscore parameters exist only so this module's own
    tests can inject a fake provider/fetcher through the exact construction
    seam ``ProductionDiscoveryEffect``/``ProductionVerificationEffect``
    already expose for their own tests. No production caller supplies them,
    and ``run_production_control_wakeup`` does not accept a substitute
    discovery or verification effect from its caller.
    """
    discovery_effect = ProductionDiscoveryEffect(
        config.discovery_config(policy),
        clock=clock,
        _provider_factory=_discovery_provider_factory,
    )
    verification_effect = ProductionVerificationEffect(
        config.verification_config(),
        _fetcher=_verification_fetcher,
        _base_policy=policy,
        _catalog=catalog,
    )
    return discovery_effect, verification_effect


def run_production_control_wakeup(
    config: ProductionControlPlaneConfig,
    *,
    scheduled_for: datetime,
    clock: Callable[[], datetime] = utc_now,
    catalog: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    selection_limit: int = DEFAULT_SCHEDULER_SELECTION_LIMIT,
    verification_bundle_limit: int = DEFAULT_VERIFICATION_BUNDLE_LIMIT,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    _discovery_provider_factory=None,
    _verification_fetcher=None,
) -> LocalControlWakeupOutcome:
    """Run the one fixed automatic discovery/verification/P2.5/P5.5 wakeup.

    This is the actual P2.8 composition. It always builds
    ``ProductionDiscoveryEffect`` and ``ProductionVerificationEffect`` from
    ``config`` and hands them to the accepted
    ``automation.local_control_plane.run_local_control_wakeup`` boundary, so
    a caller can configure private storage and Gemini identity but never
    substitute a different effect, provider, fetcher, action, or job.

    ``clock`` is read exactly once; the resulting timestamp is frozen and
    reused for the discovery effect's health-ledger bookkeeping and for the
    wakeup's own ``observed_at``, matching the existing P4.LC/P4.LS pattern
    of threading one consistent timestamp through a composed effect.
    """
    resolved_policy = policy if policy is not None else load_policy_config()
    resolved_catalog = catalog if catalog is not None else load_venue_catalog()
    observed_at = clock()
    frozen_clock = lambda: observed_at
    discovery_effect, verification_effect = build_production_effects(
        config,
        clock=frozen_clock,
        policy=resolved_policy,
        catalog=resolved_catalog,
        _discovery_provider_factory=_discovery_provider_factory,
        _verification_fetcher=_verification_fetcher,
    )
    return run_local_control_wakeup(
        config.control_state_path,
        scheduled_for=scheduled_for,
        clock=frozen_clock,
        discovery_effect=discovery_effect,
        verification_effect=verification_effect,
        catalog=resolved_catalog,
        policy=resolved_policy,
        selection_limit=selection_limit,
        verification_bundle_limit=verification_bundle_limit,
        lease_ttl_seconds=lease_ttl_seconds,
    )
