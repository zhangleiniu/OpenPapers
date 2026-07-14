"""Optional, fake-only Mac worker foundation for automation P4.2."""

from automation.mac_worker.health import (
    HealthCheckCode,
    HealthCheckName,
    HealthCheckStatus,
    HealthSignal,
    PrefectConfigurationProbe,
    WorkerHealthConfig,
    WorkerHealthReport,
    collect_worker_health,
)
from automation.mac_worker.runtime import (
    FixtureJobObservation,
    simulate_queue_envelope,
)

__all__ = [
    "FixtureJobObservation",
    "HealthCheckCode",
    "HealthCheckName",
    "HealthCheckStatus",
    "HealthSignal",
    "PrefectConfigurationProbe",
    "WorkerHealthConfig",
    "WorkerHealthReport",
    "collect_worker_health",
    "simulate_queue_envelope",
]
