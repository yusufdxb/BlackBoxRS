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

    Inspects ``ros_monitor`` events with ``event_type == "qos_profile"``
    that carry ``publisher_qos`` and ``subscriber_qos`` dictionaries in
    their ``data`` payload.  Checks reliability and durability dimensions.
    """

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "qos_mismatch"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate a QoS profile event for pub/sub incompatibilities.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event listing all incompatibilities found, or
            ``None`` if the profiles are compatible.
        """
        if event.source != "ros_monitor" or event.event_type != "qos_profile":
            return None

        pub_qos: dict[str, Any] | None = event.data.get("publisher_qos")
        sub_qos: dict[str, Any] | None = event.data.get("subscriber_qos")
        topic: str = event.data.get("topic", "<unknown>")

        if pub_qos is None or sub_qos is None:
            return None

        mismatches: list[str] = []

        reliability_issue = _check_dimension(
            pub_qos, sub_qos, "reliability", _RELIABILITY_RANK
        )
        if reliability_issue:
            mismatches.append(reliability_issue)

        durability_issue = _check_dimension(
            pub_qos, sub_qos, "durability", _DURABILITY_RANK
        )
        if durability_issue:
            mismatches.append(durability_issue)

        if not mismatches:
            return None

        message = (
            f"QoS incompatibility on {topic}: {'; '.join(mismatches)}"
        )

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"qos:{topic}",
            value=len(mismatches),
            threshold=0,
            message=message,
        )

        logger.warning("QoS mismatch detected: %s", message)

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly_qos_mismatch",
            data=anomaly.model_dump(),
            severity="warning",
            **event.metadata,
        )
