"""Live rclpy integration test: skipped when ROS 2 is unavailable.

Boots a real ``rclpy`` publisher on a unique ROS_DOMAIN_ID and verifies
that ``RosMonitor`` actually observes the topic on the ROS 2 graph,
auto-subscribes to it, and emits ``ros.topology`` + ``ros.frequency``
events on the ``EventBus``.

This closes a real proof gap: prior tests disabled the ROS monitor
entirely because no-one had wired up an honest live path.  The test
now runs when rclpy is importable and an ``std_msgs`` install is on
the Python path.  It is not faked: if rclpy is present but broken in
some way, the test fails rather than silently skipping.
"""

from __future__ import annotations

import os
import random
import time
from queue import Empty

import pytest

rclpy = pytest.importorskip(
    "rclpy", reason="rclpy not installed: skipping live ROS 2 integration"
)
pytest.importorskip(
    "std_msgs.msg",
    reason="std_msgs not installed: skipping live ROS 2 integration",
)

from std_msgs.msg import String  # noqa: E402

from blackboxrs.core.event_bus import EventBus  # noqa: E402
from blackboxrs.core.config import RosMonitorConfig  # noqa: E402
from blackboxrs.core.session import Session  # noqa: E402
from blackboxrs.ros_monitor.monitor import RosMonitor  # noqa: E402


def _pick_test_domain_id() -> str:
    """Return a ROS_DOMAIN_ID legal for ROS 2 and unlikely to collide.

    Legal range is 0–232 (ROS 2 uses it as a DDS partition).  We stay in
    ``[50, 200]`` to avoid both the common ``0`` (default) and ``232``
    (edge) values, and seed the pick with the pytest-session PID + high-
    resolution clock so concurrent CI runs or hand-run pytest invocations
    on the same host get distinct domains.

    The resulting value is stable across tests in the same session so
    the publisher and monitor always share a domain.
    """
    seed = f"{os.getpid()}-{time.time_ns()}"
    rng = random.Random(seed)
    return str(rng.randint(50, 200))


# One domain per pytest session: stable across tests so every
# component booted inside the session sees the same graph.
_TEST_DOMAIN_ID = _pick_test_domain_id()


@pytest.fixture(autouse=True)
def _isolated_ros_domain(monkeypatch: pytest.MonkeyPatch):
    """Isolate this test from any ROS graph the user happens to be running."""
    monkeypatch.setenv("ROS_DOMAIN_ID", _TEST_DOMAIN_ID)
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")


def _shutdown_rclpy() -> None:
    """Best-effort rclpy shutdown: safe to call when not initialised."""
    try:
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass


def test_ros_monitor_observes_live_publisher():
    """RosMonitor must discover a live publisher and emit at least one
    ros.frequency event for its topic within a reasonable window."""

    topic = "/blackbox_ros_live_test/chatter"

    # Fresh rclpy context so this test doesn't depend on leftover state
    # from a previous pytest module.
    _shutdown_rclpy()

    bus = EventBus(default_queue_maxsize=1024)
    ros_q = bus.subscribe(channel="ros_monitor")

    config = RosMonitorConfig(
        enabled=True,
        poll_interval_sec=0.5,
        track_latency=False,
        topic_filters=[],
    )
    session = Session()
    monitor = RosMonitor(bus, config, session)

    publisher_node = None
    publisher = None
    try:
        monitor.start()
        # RosMonitor calls rclpy.init() inside start().  Make a
        # publisher node in the same context.
        publisher_node = rclpy.create_node(
            "blackbox_live_test_publisher",
            namespace="/blackbox_live_test",
        )
        publisher = publisher_node.create_publisher(String, topic, 10)

        deadline = time.monotonic() + 8.0
        frequency_event = None
        topology_event = None
        while time.monotonic() < deadline:
            msg = String()
            msg.data = "ping"
            publisher.publish(msg)

            # The publisher_node is in the same rclpy context but not
            # attached to the monitor's executor, so we must spin it
            # ourselves to keep publication timers healthy.
            rclpy.spin_once(publisher_node, timeout_sec=0.0)

            # Drain whatever events the monitor has queued up.
            try:
                event = ros_q.get(timeout=0.1)
            except Empty:
                continue

            if event.event_type == "ros.topology":
                topology_event = event
            if (
                event.event_type == "ros.frequency"
                and event.data.get("topic") == topic
            ):
                frequency_event = event
                break

        assert topology_event is not None, (
            "RosMonitor never emitted a ros.topology event: graph "
            "discovery broken against a live rclpy context"
        )
        assert frequency_event is not None, (
            f"RosMonitor did not emit ros.frequency for {topic!r} "
            "within 8s: live discovery / subscription path regressed"
        )
        hz = frequency_event.data.get("frequency_hz")
        assert isinstance(hz, (int, float)) and hz > 0.0, (
            f"frequency_hz missing or non-positive: {hz!r}"
        )
    finally:
        try:
            if publisher_node is not None:
                publisher_node.destroy_node()
        except Exception:
            pass
        try:
            monitor.stop()
        except Exception:
            pass
        _shutdown_rclpy()


def test_ros_monitor_respects_topic_filters():
    """Filtered topics must not show up in ros.frequency events."""

    _shutdown_rclpy()

    bus = EventBus(default_queue_maxsize=1024)
    ros_q = bus.subscribe(channel="ros_monitor")

    config = RosMonitorConfig(
        enabled=True,
        poll_interval_sec=0.5,
        track_latency=False,
        # Only accept topics under this namespace: the publisher below
        # uses a different namespace and should be ignored.
        topic_filters=["/blackbox_allowed/*"],
    )
    session = Session()
    monitor = RosMonitor(bus, config, session)

    publisher_node = None
    try:
        monitor.start()
        publisher_node = rclpy.create_node(
            "blackbox_live_filter_test_publisher",
            namespace="/blackbox_denied",
        )
        pub = publisher_node.create_publisher(
            String, "/blackbox_denied/chatter", 10
        )

        deadline = time.monotonic() + 5.0
        saw_denied_freq = False
        while time.monotonic() < deadline:
            msg = String()
            msg.data = "x"
            pub.publish(msg)
            rclpy.spin_once(publisher_node, timeout_sec=0.0)

            try:
                event = ros_q.get(timeout=0.1)
            except Empty:
                continue

            if (
                event.event_type == "ros.frequency"
                and event.data.get("topic") == "/blackbox_denied/chatter"
            ):
                saw_denied_freq = True
                break

        assert not saw_denied_freq, (
            "RosMonitor emitted a ros.frequency event for a topic that "
            "topic_filters should have excluded"
        )
    finally:
        try:
            if publisher_node is not None:
                publisher_node.destroy_node()
        except Exception:
            pass
        try:
            monitor.stop()
        except Exception:
            pass
        _shutdown_rclpy()
