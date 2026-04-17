"""Dead-topic anomaly detector.

Fires when a previously active topic stops publishing for longer than
a configurable timeout period.
"""

from __future__ import annotations

import logging
from datetime import datetime

from blackboxrs.core.clock import Clock
from blackboxrs.core.config import DeadTopicConfig
from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)


class DeadTopicDetector(BaseDetector):
    """Fires when a previously active topic goes silent.

    Every incoming event that carries a ``topic`` key in its ``data``
    payload is used to update the last-seen timestamp for that topic.
    When :meth:`check` is called, any topic whose last-seen time
    exceeds ``timeout_sec`` is flagged as dead.

    Because the detector is driven by the event stream it can only
    fire when *other* events arrive.  A fully silent bus produces no
    anomalies (by design -- the bus itself would need a heartbeat for
    that).

    Args:
        config: A :class:`DeadTopicConfig` holding ``timeout_sec``.
    """

    def __init__(self, config: DeadTopicConfig) -> None:
        self._timeout_sec = config.timeout_sec
        self._last_seen: dict[str, datetime] = {}
        self._alerted: set[str] = set()

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "dead_topic"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Update topic tracking and check for dead topics.

        The detector first updates its internal last-seen table for the
        topic carried by *event* (if any), then scans the entire table
        for topics that have exceeded the timeout.

        Only one anomaly event is emitted per topic until that topic
        becomes active again.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event for the first dead topic discovered in this
            pass, or ``None`` if all topics are alive.
        """
        now = Clock.now()

        # Update last-seen for the topic in this event (if present).
        topic: str | None = event.data.get("topic")
        if topic is not None:
            self._last_seen[topic] = now
            # If we previously alerted on this topic, clear it since it's alive.
            self._alerted.discard(topic)

        # Scan for dead topics.
        for tracked_topic, last_ts in self._last_seen.items():
            elapsed = (now - last_ts).total_seconds()
            if elapsed <= self._timeout_sec:
                continue
            if tracked_topic in self._alerted:
                continue

            self._alerted.add(tracked_topic)

            anomaly = AnomalyData(
                detector=self.name,
                metric=f"dead_topic:{tracked_topic}",
                value=elapsed,
                threshold=self._timeout_sec,
                message=(
                    f"Topic {tracked_topic} has been silent for "
                    f"{elapsed:.1f}s (timeout {self._timeout_sec}s)"
                ),
            )

            logger.warning(
                "Dead topic detected: %s silent for %.1fs",
                tracked_topic,
                elapsed,
            )

            return BlackBoxEvent.anomaly_event(
                event_type="anomaly.dead_topic",
                data=anomaly.model_dump(),
                severity="warning",
                **event.metadata,
            )

        return None
