"""Threshold-based anomaly detector.

Fires when system-level metrics (CPU usage, memory usage, GPU temperature)
exceed their configured static thresholds.

The detector consumes ``BlackBoxEvent`` instances produced by
:class:`blackboxrs.system_monitor.SystemMonitor`, which emit one event per
collector with ``event_type`` of the form ``system.<collector>`` (e.g.
``system.cpu``).  Multiple metrics are checked from each event using the
data-key mapping defined in ``_METRIC_RULES``.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from blackboxrs.core.config import AnomalyThresholds
from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)


class _Rule(NamedTuple):
    """A single threshold rule applied to a system-monitor event."""

    event_type: str         # event_type emitted by SystemMonitor
    data_key: str           # key into event.data carrying the metric value
    threshold_attr: str     # attribute on AnomalyThresholds for the limit
    unit: str               # human-readable unit for messages


# Each rule maps one (event_type, data_key) pair to a configured limit.
# This is the single source of truth for the threshold detector contract.
_METRIC_RULES: tuple[_Rule, ...] = (
    _Rule("system.cpu", "cpu_percent", "cpu_percent", "%"),
    _Rule("system.memory", "memory_percent", "memory_percent", "%"),
    _Rule("system.gpu", "gpu_temp_c", "gpu_temp_c", "C"),
)


class ThresholdDetector(BaseDetector):
    """Fires when system metrics exceed configured thresholds.

    Inspects events produced by :class:`SystemMonitor`
    (``source == "system_monitor"``) whose ``event_type`` matches a
    configured rule.  A single event may carry multiple metrics
    (e.g. ``system.cpu`` includes ``cpu_percent`` plus per-core values);
    the detector emits at most one anomaly per ``check()`` call (the
    first rule whose value exceeds its limit).

    Args:
        thresholds: An :class:`AnomalyThresholds` dataclass holding the
            upper-bound values for each metric.
    """

    def __init__(self, thresholds: AnomalyThresholds) -> None:
        self._thresholds = thresholds

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "threshold"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Check a system-monitor event against static thresholds.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event if any rule is violated, else ``None``.
        """
        if event.source != "system_monitor":
            return None

        for rule in _METRIC_RULES:
            if rule.event_type != event.event_type:
                continue
            value = event.data.get(rule.data_key)
            if value is None:
                continue
            threshold = getattr(self._thresholds, rule.threshold_attr)
            if not isinstance(value, (int, float)) or value <= threshold:
                continue

            anomaly = AnomalyData(
                detector=self.name,
                metric=rule.data_key,
                value=float(value),
                threshold=float(threshold),
                message=(
                    f"{rule.data_key} is {value}{rule.unit}, "
                    f"exceeding threshold of {threshold}{rule.unit}"
                ),
            )

            logger.warning(
                "Threshold exceeded: %s = %s%s (limit %s%s)",
                rule.data_key,
                value,
                rule.unit,
                threshold,
                rule.unit,
            )

            return BlackBoxEvent.anomaly_event(
                event_type="anomaly.threshold",
                data=anomaly.model_dump(),
                severity="warning",
                **event.metadata,
            )

        return None
