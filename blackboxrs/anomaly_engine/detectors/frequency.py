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

    Hysteresis: an anomaly is only emitted after
    ``min_consecutive_samples`` consecutive violating samples for the
    same topic.  A healthy sample resets the counter to zero.

    Only ``ros_monitor`` events with ``event_type == "ros.frequency"``
    are inspected.

    Args:
        config: A :class:`FrequencyConfig` holding ``tolerance_percent``
            and ``min_consecutive_samples``.
    """

    #: Fingerprint-stable identity. Two frequency-drop incidents on the
    #: same topic should collide; on different topics they should not.
    signature_fields = ["topic"]

    target_subsystem = "ros"

    def __init__(self, config: FrequencyConfig) -> None:
        self._tolerance_pct = config.tolerance_percent
        self._min_consecutive = config.min_consecutive_samples
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._baselines: dict[str, float] = {}
        # Per-topic violation counter.
        self._violation_counts: dict[str, int] = {}

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "frequency"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate a topic-frequency event.

        Consumes events produced by :class:`RosMonitor` with
        ``event_type == "ros.frequency"``.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event if the frequency dropped below the
            tolerance floor for ``min_consecutive_samples`` consecutive
            samples, else ``None``.
        """
        if event.source != "ros_monitor" or event.event_type != "ros.frequency":
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
            # Healthy sample: reset violation counter for this topic.
            self._violation_counts[topic] = 0
            return None

        # Violating sample: increment counter.
        count = self._violation_counts.get(topic, 0) + 1
        self._violation_counts[topic] = count

        if count < self._min_consecutive:
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

        # Surface signature-field values into ``data`` so they round-trip
        # into the trigger model and the fingerprint algorithm picks them
        # up via ``DetectorTrigger.signature_fields`` + ``data``.
        data = anomaly.model_dump()
        data["topic"] = topic

        metadata = dict(event.metadata)
        metadata.update(self.detector_metadata())

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.frequency",
            data=data,
            severity="warning",
            **metadata,
        )
