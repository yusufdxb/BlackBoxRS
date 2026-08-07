"""Deterministic preflight graph probes for benchmark scenarios.

The live preflight checks query rclpy graph APIs. Local benchmark runs may
not have ROS 2 available, so supported closed-loop scenarios use an
explicit in-process graph probe. This still exercises the real
``PreflightRunner`` and check implementations; only graph discovery is
replaced by deterministic benchmark input.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

from blackboxrs.prevention.checks import qos_match, topic_present


@dataclass(frozen=True)
class QoSEndpoint:
    """Small object compatible with qos_match's rclpy endpoint access."""

    reliability: str = "RELIABLE"
    durability: str = "VOLATILE"

    @property
    def qos_profile(self) -> SimpleNamespace:
        return SimpleNamespace(
            reliability=self.reliability,
            durability=self.durability,
        )


class GraphProbe:
    """Deterministic graph state used by benchmark preflight checks."""

    def __init__(
        self,
        *,
        publishers: dict[str, int] | None = None,
        qos_publishers: dict[str, list[QoSEndpoint]] | None = None,
        qos_subscribers: dict[str, list[QoSEndpoint]] | None = None,
    ) -> None:
        self._publishers = publishers or {}
        self._qos_publishers = qos_publishers or {}
        self._qos_subscribers = qos_subscribers or {}

    def get_publishers_info_by_topic(self, topic: str):
        if topic in self._qos_publishers:
            return list(self._qos_publishers[topic])
        return [SimpleNamespace()] * self._publishers.get(topic, 0)

    def get_subscriptions_info_by_topic(self, topic: str):
        return list(self._qos_subscribers.get(topic, []))


@contextmanager
def patched_graph_probe(probe: GraphProbe) -> Iterator[None]:
    """Patch rclpy graph access for deterministic benchmark preflight."""

    def _with_graph_node(fn, *, settle_sec=0.0, name=""):
        return fn(probe)

    with ExitStack() as stack:
        stack.enter_context(patch.object(topic_present, "RCLPY_AVAILABLE", True))
        stack.enter_context(patch.object(topic_present, "with_graph_node", _with_graph_node))
        stack.enter_context(patch.object(qos_match, "RCLPY_AVAILABLE", True))
        stack.enter_context(patch.object(qos_match, "with_graph_node", _with_graph_node))
        yield
