"""Frequency-drop anomaly detector.

Learns the expected publication rate for each topic from the first N
samples and then fires when the observed rate drops below a configurable
tolerance percentage of the learned baseline.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from blackboxrs.core.config import FrequencyConfig
from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)

_LEARNING_SAMPLES = 10


class FrequencyDetector(BaseDetector):
    """Fires when topic frequency drops below the expected rate.

    The detector operates in two phases per topic:

    1. **Learning** -- collects the first ``_LEARNING_SAMPLES`` frequency
       readings and averages them to establish a baseline.
    2. **Monitoring** -- compares every subsequent reading against
       ``baseline * (1 - tolerance_percent / 100)``.  If the observed
       frequency falls below the floor, an anomaly event is emitted.

    Only ``ros_monitor`` events with ``event_type == "topic_frequency"``
    are inspected.

    Args:
        config: A :class:`FrequencyConfig` holding ``tolerance_percent``.
    """

    def __init__(self, config: FrequencyConfig) -> None:
        self._tolerance_pct = config.tolerance_percent
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._baselines: dict[str, float] = {}

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "frequency"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate a topic-frequency event.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event if the frequency dropped below the
            tolerance floor, else ``None``.
        """
        if event.source != "ros_monitor" or event.event_type != "topic_frequency":
            return None

        topic: str | None = event.data.get("topic")
        frequency_hz = event.data.get("frequency_hz")
        if topic is None or frequency_hz is None:
            return None

        # -- Learning phase ---------------------------------------------------
        if topic not in self._baselines:
            self._samples[topic].append(frequency_hz)
            if len(self._samples[topic]) < _LEARNING_SAMPLES:
                return None
            # Compute baseline and clean up scratch storage
            baseline = sum(self._samples[topic]) / len(self._samples[topic])
            self._baselines[topic] = baseline
            del self._samples[topic]
            logger.info(
                "Frequency baseline for %s established at %.2f Hz",
                topic,
                baseline,
            )
            return None

        # -- Monitoring phase -------------------------------------------------
        baseline = self._baselines[topic]
        floor = baseline * (1 - self._tolerance_pct / 100.0)

        if frequency_hz >= floor:
            return None

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"frequency:{topic}",
            value=frequency_hz,
            threshold=floor,
            message=(
                f"Topic {topic} frequency dropped to {frequency_hz:.2f} Hz, "
                f"below floor of {floor:.2f} Hz "
                f"(baseline {baseline:.2f} Hz, tolerance {self._tolerance_pct}%)"
            ),
        )

        logger.warning(
            "Frequency anomaly on %s: %.2f Hz < %.2f Hz floor",
            topic,
            frequency_hz,
            floor,
        )

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly_frequency",
            data=anomaly.model_dump(),
            severity="warning",
            **event.metadata,
        )
