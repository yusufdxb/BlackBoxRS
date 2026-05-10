"""Tests for the preflight check implementations.

We do not require a live ROS 2 graph in unit tests. Instead we mock
``with_graph_node`` to feed a fake node object to the check function.
The fake node implements only the rclpy graph-introspection methods
the checks actually call.

The "rclpy missing" path is also exercised explicitly: the checks must
return ``("skipped", ...)`` with a clear message when rclpy isn't
importable, and never crash.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from blackboxrs.prevention.checks import (
    node_running,
    qos_match,
    topic_present,
)
from blackboxrs.prevention.rules import (
    PreflightCheck,
    PreventionRule,
    make_rule,
)
from blackboxrs.prevention.runner import PreflightRunner


# ---------------------------------------------------------------------------
# Fake rclpy graph node
# ---------------------------------------------------------------------------


class _FakeQoS:
    def __init__(self, reliability="RELIABLE", durability="VOLATILE"):
        self.reliability = SimpleNamespace(name=f"ReliabilityPolicy.{reliability}")
        self.durability = SimpleNamespace(name=f"DurabilityPolicy.{durability}")


class _FakeEndpointInfo:
    def __init__(self, qos: _FakeQoS) -> None:
        self.qos_profile = qos


class _FakeNode:
    def __init__(self, *, publishers=None, subscribers=None, nodes=None):
        self._publishers = publishers or {}
        self._subscribers = subscribers or {}
        self._nodes = nodes or []  # list of (name, namespace)

    def get_publishers_info_by_topic(self, topic):
        return list(self._publishers.get(topic, []))

    def get_subscriptions_info_by_topic(self, topic):
        return list(self._subscribers.get(topic, []))

    def get_node_names_and_namespaces(self):
        return list(self._nodes)


def _patch_runtime(module, fake_node):
    """Patch ``with_graph_node`` and ``RCLPY_AVAILABLE`` in *module*.

    Returns a context manager that, while active, makes the check
    invoke its query callable against *fake_node* without any rclpy
    machinery.
    """
    def _stub_with_graph_node(fn, *, settle_sec=0.0, name=""):
        return fn(fake_node)

    return (
        patch.object(module, "with_graph_node", _stub_with_graph_node),
        patch.object(module, "RCLPY_AVAILABLE", True),
    )


def _run(module, params, fake_node):
    p1, p2 = _patch_runtime(module, fake_node)
    with p1, p2:
        return module.run(params)


# ---------------------------------------------------------------------------
# topic_present
# ---------------------------------------------------------------------------


def test_topic_present_pass_with_one_publisher():
    fake = _FakeNode(publishers={"/scan": [_FakeEndpointInfo(_FakeQoS())]})
    status, msg = _run(topic_present, {"topic": "/scan"}, fake)
    assert status == "pass"
    assert "/scan" in msg


def test_topic_present_fails_when_below_min():
    fake = _FakeNode(publishers={"/scan": []})
    status, msg = _run(topic_present, {"topic": "/scan", "min_publishers": 1}, fake)
    assert status == "fail"
    assert "0 publisher" in msg


def test_topic_present_skipped_without_rclpy():
    with patch.object(topic_present, "RCLPY_AVAILABLE", False):
        status, msg = topic_present.run({"topic": "/scan"})
    assert status == "skipped"
    assert "rclpy" in msg.lower()


def test_topic_present_error_on_missing_param():
    status, msg = topic_present.run({})
    assert status == "error"


# ---------------------------------------------------------------------------
# qos_match
# ---------------------------------------------------------------------------


def test_qos_match_pass_when_compatible():
    fake = _FakeNode(
        publishers={"/scan": [_FakeEndpointInfo(_FakeQoS("RELIABLE", "VOLATILE"))]},
        subscribers={"/scan": [_FakeEndpointInfo(_FakeQoS("BEST_EFFORT", "VOLATILE"))]},
    )
    # Pub RELIABLE >= Sub BEST_EFFORT, durabilities equal: compatible.
    status, msg = _run(qos_match, {"topic": "/scan"}, fake)
    assert status == "pass", msg


def test_qos_match_fail_when_subscriber_stricter():
    fake = _FakeNode(
        publishers={"/scan": [_FakeEndpointInfo(_FakeQoS("BEST_EFFORT", "VOLATILE"))]},
        subscribers={"/scan": [_FakeEndpointInfo(_FakeQoS("RELIABLE", "VOLATILE"))]},
    )
    status, msg = _run(qos_match, {"topic": "/scan"}, fake)
    assert status == "fail"
    assert "reliability" in msg.lower()


def test_qos_match_skipped_when_no_publishers():
    fake = _FakeNode(
        publishers={"/scan": []},
        subscribers={"/scan": [_FakeEndpointInfo(_FakeQoS())]},
    )
    status, msg = _run(qos_match, {"topic": "/scan"}, fake)
    assert status == "skipped"
    assert "no publishers" in msg.lower()


def test_qos_match_skipped_when_no_subscribers():
    fake = _FakeNode(
        publishers={"/scan": [_FakeEndpointInfo(_FakeQoS())]},
        subscribers={"/scan": []},
    )
    status, msg = _run(qos_match, {"topic": "/scan"}, fake)
    assert status == "skipped"
    assert "no subscribers" in msg.lower()


def test_qos_match_skipped_without_rclpy():
    with patch.object(qos_match, "RCLPY_AVAILABLE", False):
        status, msg = qos_match.run({"topic": "/scan"})
    assert status == "skipped"


# ---------------------------------------------------------------------------
# node_running
# ---------------------------------------------------------------------------


def test_node_running_pass_when_present():
    fake = _FakeNode(nodes=[("planner", "/nav"), ("perception", "/percep")])
    status, msg = _run(node_running, {"name": "/nav/planner"}, fake)
    assert status == "pass"
    assert "planner" in msg


def test_node_running_pass_with_bare_name_any_namespace():
    fake = _FakeNode(nodes=[("planner", "/some_other_ns")])
    status, msg = _run(node_running, {"name": "planner"}, fake)
    assert status == "pass"


def test_node_running_fail_when_missing():
    fake = _FakeNode(nodes=[("other", "/")])
    status, msg = _run(node_running, {"name": "planner"}, fake)
    assert status == "fail"
    assert "planner" in msg


def test_node_running_skipped_without_rclpy():
    with patch.object(node_running, "RCLPY_AVAILABLE", False):
        status, msg = node_running.run({"name": "planner"})
    assert status == "skipped"


# ---------------------------------------------------------------------------
# Runner integration: severity_on_fail promotion
# ---------------------------------------------------------------------------


def test_runner_promotes_fail_to_block():
    rule = make_rule(PreflightCheck(
        name="x", kind="topic_present",
        params={"topic": "/scan"}, severity_on_fail="block",
    ))
    fake = _FakeNode(publishers={"/scan": []})
    p1, p2 = _patch_runtime(topic_present, fake)
    with p1, p2:
        report = PreflightRunner([rule]).run()
    assert report.results[0].status == "block"
    assert report.exit_code == 1


def test_runner_promotes_fail_to_warn():
    rule = make_rule(PreflightCheck(
        name="x", kind="topic_present",
        params={"topic": "/scan"}, severity_on_fail="warn",
    ))
    fake = _FakeNode(publishers={"/scan": []})
    p1, p2 = _patch_runtime(topic_present, fake)
    with p1, p2:
        report = PreflightRunner([rule]).run()
    assert report.results[0].status == "warn"
    assert report.exit_code == 2


def test_runner_pass_keeps_pass():
    rule = make_rule(PreflightCheck(
        name="x", kind="topic_present",
        params={"topic": "/scan"}, severity_on_fail="block",
    ))
    fake = _FakeNode(publishers={"/scan": [_FakeEndpointInfo(_FakeQoS())]})
    p1, p2 = _patch_runtime(topic_present, fake)
    with p1, p2:
        report = PreflightRunner([rule]).run()
    assert report.results[0].status == "pass"
    assert report.exit_code == 0
