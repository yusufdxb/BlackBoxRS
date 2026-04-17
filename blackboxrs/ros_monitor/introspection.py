"""ROS 2 graph introspection for BlackBoxRS.

Queries the ROS 2 computation graph to discover topics, nodes,
and QoS profiles.  All rclpy access is guarded so the module
can be imported even when rclpy is not installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from blackboxrs.core.clock import Clock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded rclpy import
# ---------------------------------------------------------------------------
try:
    from rclpy.node import Node
    from rclpy.qos import QoSProfile

    _RCLPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RCLPY_AVAILABLE = False
    Node = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicInfo:
    """Immutable snapshot of a single ROS 2 topic.

    QoS profiles are captured separately for publishers and subscribers
    so consumers can compare each pub/sub pair for compatibility.
    """

    name: str
    msg_type: str
    publisher_count: int = 0
    subscriber_count: int = 0
    publisher_qos_profiles: list[dict] = field(default_factory=list)
    subscriber_qos_profiles: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class GraphSnapshot:
    """Point-in-time view of the ROS 2 computation graph."""

    timestamp: datetime
    topics: list[TopicInfo] = field(default_factory=list)
    node_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# QoS serialisation helper
# ---------------------------------------------------------------------------


def _serialise_qos(endpoint_info) -> dict:
    """Convert an ``rclpy`` endpoint QoS profile to a plain ``dict``.

    Args:
        endpoint_info: An ``rclpy.topic_endpoint_info.TopicEndpointInfo``
            instance (carries a ``.qos_profile`` attribute).

    Returns:
        A JSON-friendly dictionary describing the QoS settings.
    """
    qos: QoSProfile = endpoint_info.qos_profile
    return {
        "reliability": str(qos.reliability),
        "durability": str(qos.durability),
        "history": str(qos.history),
        "depth": qos.depth,
        "lifespan": str(qos.lifespan),
        "deadline": str(qos.deadline),
        "liveliness": str(qos.liveliness),
        "liveliness_lease_duration": str(qos.liveliness_lease_duration),
    }


# ---------------------------------------------------------------------------
# Introspector
# ---------------------------------------------------------------------------


class RosIntrospector:
    """Queries the ROS 2 graph for topology information.

    Args:
        node: An initialised ``rclpy.node.Node`` used to query the graph.

    Raises:
        RuntimeError: If ``rclpy`` is not available in the current
            environment.
    """

    def __init__(self, node: "Node") -> None:  # type: ignore[name-defined]
        if not _RCLPY_AVAILABLE:
            raise RuntimeError(
                "rclpy is not installed; ROS 2 introspection is unavailable"
            )
        self._node = node

    # -- public API ---------------------------------------------------------

    def snapshot(self) -> GraphSnapshot:
        """Capture the current ROS 2 graph state.

        Returns:
            A ``GraphSnapshot`` containing discovered topics and nodes.
        """
        timestamp = Clock.now()

        # Topic names & types ------------------------------------------------
        topic_names_and_types: list[tuple[str, list[str]]] = (
            self._node.get_topic_names_and_types()
        )

        topics: list[TopicInfo] = []
        for topic_name, type_list in topic_names_and_types:
            msg_type = type_list[0] if type_list else "unknown"

            try:
                pub_info = self._node.get_publishers_info_by_topic(topic_name)
                sub_info = self._node.get_subscriptions_info_by_topic(topic_name)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to query endpoint info for %s", topic_name
                )
                pub_info = []
                sub_info = []

            pub_qos = [_serialise_qos(p) for p in pub_info]
            sub_qos = [_serialise_qos(s) for s in sub_info]

            topics.append(
                TopicInfo(
                    name=topic_name,
                    msg_type=msg_type,
                    publisher_count=len(pub_info),
                    subscriber_count=len(sub_info),
                    publisher_qos_profiles=pub_qos,
                    subscriber_qos_profiles=sub_qos,
                )
            )

        # Node names ---------------------------------------------------------
        node_names_and_ns: list[tuple[str, str]] = (
            self._node.get_node_names_and_namespaces()
        )
        node_names = [
            f"{ns.rstrip('/')}/{name}" if ns != "/" else f"/{name}"
            for name, ns in node_names_and_ns
        ]

        return GraphSnapshot(
            timestamp=timestamp,
            topics=topics,
            node_names=node_names,
        )

    def get_qos_profiles(self, topic: str) -> list[dict]:
        """Return serialised QoS profiles for all publishers on *topic*.

        Args:
            topic: Fully-qualified ROS 2 topic name.

        Returns:
            A list of dictionaries, one per publisher, describing the
            QoS profile in use.
        """
        try:
            pub_info = self._node.get_publishers_info_by_topic(topic)
            return [_serialise_qos(p) for p in pub_info]
        except Exception:  # noqa: BLE001
            logger.warning("Could not retrieve QoS profiles for %s", topic)
            return []
