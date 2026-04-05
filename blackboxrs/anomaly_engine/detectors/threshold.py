"""Threshold-based anomaly detector.

Fires when system-level metrics (CPU usage, memory usage, GPU temperature)
exceed their configured static thresholds.
"""

from __future__ import annotations

import logging

from blackboxrs.core.config import AnomalyThresholds
from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)

# Maps event_type -> (data key for the metric value, threshold attr, unit)
_METRIC_MAP: dict[str, tuple[str, str, str]] = {
    "cpu_usage": ("value", "cpu_percent", "%"),
    "memory_usage": ("value", "memory_percent", "%"),
    "gpu_temperature": ("value", "gpu_temp_c", "C"),
}


class ThresholdDetector(BaseDetector):
    """Fires when system metrics exceed configured thresholds.

    Only inspects events whose ``source`` is ``"system_monitor"`` and
    whose ``event_type`` matches one of the known metric types
    (``cpu_usage``, ``memory_usage``, ``gpu_temperature``).

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
            An anomaly event if the metric exceeds its threshold, else
            ``None``.
        """
        if event.source != "system_monitor":
            return None

        mapping = _METRIC_MAP.get(event.event_type)
        if mapping is None:
            return None

        value_key, threshold_attr, unit = mapping
        value = event.data.get(value_key)
        if value is None:
            return None

        threshold = getattr(self._thresholds, threshold_attr)
        if value <= threshold:
            return None

        anomaly = AnomalyData(
            detector=self.name,
            metric=event.event_type,
            value=value,
            threshold=threshold,
            message=(
                f"{event.event_type} is {value}{unit}, "
                f"exceeding threshold of {threshold}{unit}"
            ),
        )

        logger.warning(
            "Threshold exceeded: %s = %s%s (limit %s%s)",
            event.event_type,
            value,
            unit,
            threshold,
            unit,
        )

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly_threshold",
            data=anomaly.model_dump(),
            severity="warning",
            **event.metadata,
        )
