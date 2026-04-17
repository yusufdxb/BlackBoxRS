"""QoS mismatch anomaly detector.

Fires when a publisher and subscriber on the same topic declare
incompatible Quality-of-Service profiles, which would cause silent
message loss in ROS 2.
"""

from __future__ import annotations

import logging
from typing import Any

from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)

# ROS 2 QoS compatibility matrix.
# A publisher/subscriber pair is incompatible when the subscriber demands
# a stricter guarantee than the publisher offers.
#
# Reliability: RELIABLE pub + BEST_EFFORT sub = OK
#              BEST_EFFORT pub + RELIABLE sub = INCOMPATIBLE
#
# Durability:  TRANSIENT_LOCAL pub + VOLATILE sub = OK
#              VOLATILE pub + TRANSIENT_LOCAL sub = INCOMPATIBLE

_RELIABILITY_RANK: dict[str, int] = {
    "RELIABLE": 2,
    "BEST_EFFORT": 1,
}

_DURABILITY_RANK: dict[str, int] = {
    "TRANSIENT_LOCAL": 2,
    "VOLATILE": 1,
}


def _normalize(value: str) -> str:
    """Strip enum prefixes and normalise to uppercase.

    rclpy serialises enums as e.g.
    ``"ReliabilityPolicy.RELIABLE"`` -- this helper extracts the
    trailing identifier.

    Args:
        value: Raw QoS enum string.

    Returns:
        Uppercase short name, e.g. ``"RELIABLE"``.
    """
    return value.rsplit(".", maxsplit=1)[-1].upper()


def _check_dimension(
    pub_qos: dict[str, Any],
    sub_qos: dict[str, Any],
    dimension: str,
    rank_map: dict[str, int],
) -> str | None:
    """Return a mismatch description if the subscriber is stricter, else ``None``.

    Args:
        pub_qos: Publisher QoS profile dict.
        sub_qos: Subscriber QoS profile dict.
        dimension: QoS dimension key (``"reliability"`` or ``"durability"``).
        rank_map: Maps normalised enum names to strictness ranks.

    Returns:
        A human-readable mismatch string, or ``None`` if compatible.
    """
    pub_raw = pub_qos.get(dimension)
    sub_raw = sub_qos.get(dimension)
    if pub_raw is None or sub_raw is None:
        return None

    pub_level = _normalize(str(pub_raw))
    sub_level = _normalize(str(sub_raw))

    pub_rank = rank_map.get(pub_level)
    sub_rank = rank_map.get(sub_level)
    if pub_rank is None or sub_rank is None:
        return None

    if pub_rank < sub_rank:
        return (
            f"{dimension} mismatch: publisher={pub_level}, "
            f"subscriber={sub_level}"
        )
    return None


class QoSMismatchDetector(BaseDetector):
    """Fires when publisher and subscriber QoS profiles are incompatible.

    Consumes :class:`RosMonitor` ``ros.qos`` events whose ``data``
    payload carries ``publisher_qos_profiles`` and
    ``subscriber_qos_profiles`` lists (each entry is a per-endpoint QoS
    dict produced by :func:`introspection._serialise_qos`).  Compares
    every (publisher, subscriber) pair and emits a single anomaly per
    topic when at least one pair is incompatible.

    Topics with zero publishers or zero subscribers are skipped — there
    is no compatibility relation to check.
    """

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "qos_mismatch"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate a QoS event for pub/sub incompatibilities.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event listing all incompatibilities found, or
            ``None`` if every pub/sub pair is compatible.
        """
        if event.source != "ros_monitor" or event.event_type != "ros.qos":
            return None

        pub_profiles: list[dict[str, Any]] | None = event.data.get(
            "publisher_qos_profiles"
        )
        sub_profiles: list[dict[str, Any]] | None = event.data.get(
            "subscriber_qos_profiles"
        )
        topic: str = event.data.get("topic", "<unknown>")

        if not pub_profiles or not sub_profiles:
            return None

        mismatches: list[str] = []
        for pub_idx, pub_qos in enumerate(pub_profiles):
            for sub_idx, sub_qos in enumerate(sub_profiles):
                pair_label = f"pub#{pub_idx}↔sub#{sub_idx}"
                reliability_issue = _check_dimension(
                    pub_qos, sub_qos, "reliability", _RELIABILITY_RANK
                )
                if reliability_issue:
                    mismatches.append(f"{pair_label}: {reliability_issue}")

                durability_issue = _check_dimension(
                    pub_qos, sub_qos, "durability", _DURABILITY_RANK
                )
                if durability_issue:
                    mismatches.append(f"{pair_label}: {durability_issue}")

        if not mismatches:
            return None

        message = (
            f"QoS incompatibility on {topic}: {'; '.join(mismatches)}"
        )

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"qos:{topic}",
            value=float(len(mismatches)),
            threshold=0.0,
            message=message,
        )

        logger.warning("QoS mismatch detected: %s", message)

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.qos_mismatch",
            data=anomaly.model_dump(),
            severity="warning",
            **event.metadata,
        )
