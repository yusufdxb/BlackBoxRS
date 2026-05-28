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
    target_subsystem: str   # "system" / "gpu" depending on metric domain


# Each rule maps one (event_type, data_key) pair to a configured limit.
# This is the single source of truth for the threshold detector contract.
_METRIC_RULES: tuple[_Rule, ...] = (
    _Rule("system.cpu", "cpu_percent", "cpu_percent", "%", "system"),
    _Rule("system.memory", "memory_percent", "memory_percent", "%", "system"),
    _Rule("system.gpu", "gpu_temp_c", "gpu_temp_c", "C", "gpu"),
)


class ThresholdDetector(BaseDetector):
    """Fires when system metrics exceed configured thresholds.

    Inspects events produced by :class:`SystemMonitor`
    (``source == "system_monitor"``) whose ``event_type`` matches a
    configured rule.  A single event may carry multiple metrics
    (e.g. ``system.cpu`` includes ``cpu_percent`` plus per-core values);
    the detector emits at most one anomaly per ``check()`` call (the
    first rule whose value exceeds its limit).

    Hysteresis: an anomaly is only emitted after
    ``min_consecutive_samples`` consecutive violating samples for the
    same metric key.  A healthy sample resets the counter to zero.

    Args:
        thresholds: An :class:`AnomalyThresholds` dataclass holding the
            upper-bound values for each metric.
    """

    #: Fingerprint-stable identity field. The ``metric`` key in ``data``
    #: is set to e.g. ``cpu_percent`` / ``memory_percent`` / ``gpu_temp_c``
    #: by the rule machinery, which is exactly the granularity we want
    #: failures to collide at.
    signature_fields = ["metric"]

    #: Resolved per-event in :meth:`check` because cpu/memory thresholds
    #: live in the system subsystem and gpu_temp_c lives in gpu. We
    #: still expose a class-level default so generic introspection works.
    target_subsystem = "system"

    def __init__(self, thresholds: AnomalyThresholds) -> None:
        self._thresholds = thresholds
        self._min_consecutive = thresholds.min_consecutive_samples
        # Per-metric violation counter: key is rule.data_key (e.g. "cpu_percent").
        self._violation_counts: dict[str, int] = {}

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "threshold"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Check a system-monitor event against static thresholds.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event if any rule is violated for
            ``min_consecutive_samples`` consecutive samples, else ``None``.
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
                # Healthy sample: reset counter for this metric.
                self._violation_counts[rule.data_key] = 0
                continue

            # Violating sample: increment counter.
            count = self._violation_counts.get(rule.data_key, 0) + 1
            self._violation_counts[rule.data_key] = count

            if count < self._min_consecutive:
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

            metadata = dict(event.metadata)
            metadata.update(self.detector_metadata())
            # Per-event subsystem override: cpu/mem live in "system",
            # gpu_temp_c lives in "gpu". The class default is "system";
            # we override here to keep gpu temperature triggers tagged
            # accurately for fingerprinting and report rendering.
            metadata["target_subsystem"] = rule.target_subsystem

            return BlackBoxEvent.anomaly_event(
                event_type="anomaly.threshold",
                data=anomaly.model_dump(),
                severity="warning",
                **metadata,
            )

        return None
