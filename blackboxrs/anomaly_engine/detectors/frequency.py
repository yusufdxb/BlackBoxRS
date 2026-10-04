"""Frequency-drop anomaly detector.

Learns the expected publication rate for each topic from the first N
samples and then fires when the observed rate drops below a configurable
tolerance percentage of the learned baseline. A separate recovery threshold
prevents repeated triggers while a noisy rate remains near the entry boundary.
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
    2. **Monitoring** -- compares subsequent readings against separate
       entry and recovery floors derived from the baseline.

    An anomaly is only emitted after
    ``min_consecutive_samples`` consecutive violating samples for the
    same topic. The topic then remains latched and emits nothing further
    until its rate reaches the stricter recovery floor. Readings between
    the entry and recovery floors do not re-arm the detector.

    Only ``ros_monitor`` events with ``event_type == "ros.frequency"``
    are inspected.

    Args:
        config: A :class:`FrequencyConfig` holding the entry tolerance,
            recovery tolerance, and required consecutive samples.
    """

    #: Fingerprint-stable identity. Two frequency-drop incidents on the
    #: same topic should collide; on different topics they should not.
    signature_fields = ["topic"]

    target_subsystem = "ros"

    def __init__(self, config: FrequencyConfig) -> None:
        self._tolerance_pct = config.tolerance_percent
        self._recovery_tolerance_pct = config.recovery_tolerance_percent
        self._min_consecutive = config.min_consecutive_samples
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._baselines: dict[str, float] = {}
        self._violation_counts: dict[str, int] = {}
        self._alerted_topics: set[str] = set()

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
            One anomaly event when the frequency first remains below the
            entry floor for ``min_consecutive_samples`` consecutive samples.
            The topic must recover above the exit floor before another event
            can be emitted.
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
        entry_floor = baseline * (1 - self._tolerance_pct / 100.0)
        recovery_floor = baseline * (1 - self._recovery_tolerance_pct / 100.0)

        if topic in self._alerted_topics:
            if frequency_hz >= recovery_floor:
                self._alerted_topics.remove(topic)
                self._violation_counts[topic] = 0
            return None

        if frequency_hz >= entry_floor:
            self._violation_counts[topic] = 0
            return None

        count = self._violation_counts.get(topic, 0) + 1
        self._violation_counts[topic] = count

        if count < self._min_consecutive:
            return None

        self._alerted_topics.add(topic)
        self._violation_counts[topic] = 0

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"frequency:{topic}",
            value=frequency_hz,
            threshold=entry_floor,
            message=(
                f"Topic {topic} frequency dropped to {frequency_hz:.2f} Hz, "
                f"below entry floor of {entry_floor:.2f} Hz "
                f"(baseline {baseline:.2f} Hz, "
                f"recovery floor {recovery_floor:.2f} Hz)"
            ),
        )

        logger.warning(
            "Frequency anomaly on %s: %.2f Hz < %.2f Hz floor",
            topic,
            frequency_hz,
            entry_floor,
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
