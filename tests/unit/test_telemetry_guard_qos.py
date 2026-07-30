"""ROS QoS compatibility policy tests for telemetry graph qualification."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from blackboxrs.prevention.telemetry_guard import _inspect_graph
from blackboxrs.prevention.telemetry_health import TelemetryHealthContract


rclpy_qos = pytest.importorskip(
    "rclpy.qos", reason="QoS compatibility tests require rclpy"
)


@dataclass
class _Endpoint:
    topic_type: str
    qos_profile: object


class _Node:
    def __init__(self, endpoint: _Endpoint) -> None:
        self.endpoint = endpoint

    def get_topic_names_and_types(self):
        return [("/utlidar/robot_pose", [self.endpoint.topic_type])]

    def get_publishers_info_by_topic(self, _topic):
        return [self.endpoint]


def _contract() -> TelemetryHealthContract:
    return TelemetryHealthContract(
        topic="/utlidar/robot_pose",
        expected_type="geometry_msgs/msg/PoseStamped",
        expected_qos={
            "history": "keep_last",
            "depth": 1,
            "reliability": "reliable",
            "durability": "volatile",
        },
        declared_context_label="bounded-evaluation",
        startup_grace_sec=0.5,
        stale_timeout_sec=0.15,
        minimum_rate_hz=15.0,
        rate_window_sec=2.0,
        header_progress_timeout_sec=0.15,
        lifecycle_stages=["startup", "runtime"],
    )


def _qos(*, depth=1, reliability=None, durability=None):
    return rclpy_qos.QoSProfile(
        history=rclpy_qos.HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=reliability or rclpy_qos.ReliabilityPolicy.RELIABLE,
        durability=durability or rclpy_qos.DurabilityPolicy.VOLATILE,
    )


def _compatible_count(publisher_qos, subscriber_qos, *, topic_type=None):
    expected_type = "geometry_msgs/msg/PoseStamped"
    result = _inspect_graph(
        _Node(_Endpoint(topic_type or expected_type, publisher_qos)),
        _contract(),
        subscriber_qos,
        qos_check_compatible=rclpy_qos.qos_check_compatible,
        QoSCompatibility=rclpy_qos.QoSCompatibility,
    )
    return result["compatible_publisher_count"]


def test_compatible_depth_difference_is_admitted():
    assert _compatible_count(_qos(depth=10), _qos(depth=1)) == 1


def test_compatible_durability_difference_is_admitted():
    assert (
        _compatible_count(
            _qos(durability=rclpy_qos.DurabilityPolicy.TRANSIENT_LOCAL),
            _qos(durability=rclpy_qos.DurabilityPolicy.VOLATILE),
        )
        == 1
    )


def test_incompatible_reliability_is_rejected():
    assert (
        _compatible_count(
            _qos(reliability=rclpy_qos.ReliabilityPolicy.BEST_EFFORT),
            _qos(reliability=rclpy_qos.ReliabilityPolicy.RELIABLE),
        )
        == 0
    )


def test_incompatible_durability_is_rejected():
    assert (
        _compatible_count(
            _qos(durability=rclpy_qos.DurabilityPolicy.VOLATILE),
            _qos(durability=rclpy_qos.DurabilityPolicy.TRANSIENT_LOCAL),
        )
        == 0
    )


def test_exact_type_mismatch_is_rejected_before_qos():
    assert (
        _compatible_count(
            _qos(),
            _qos(),
            topic_type="std_msgs/msg/String",
        )
        == 0
    )
