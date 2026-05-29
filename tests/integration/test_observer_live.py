"""End-to-end observer-mode-over-DDS integration test.

This file closes the audit's "observer-mode-over-real-DDS" gap. Existing
observer-mode tests only fed synthetic JSONL through the CLI subprocess
path. This file boots a real rclpy publisher node, starts a BlackBoxRS
daemon in ``runtime.role: observer`` mode in the same ROS_DOMAIN_ID, and
asserts the daemon's anomaly engine fires on a real DDS-visible event.

Gated on rclpy availability. Skips cleanly on GitHub hosted runners; runs
inside the Docker Humble CI job.
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from queue import Empty

import pytest

rclpy = pytest.importorskip(
    "rclpy", reason="rclpy not installed; skipping live observer test"
)
pytest.importorskip(
    "std_msgs.msg", reason="std_msgs not installed; skipping live observer test"
)
# tf2_msgs imports transitively pull in numpy via geometry_msgs; if numpy
# is absent the ROS monitor's TfSnapshotter start will throw, so skip.
pytest.importorskip(
    "numpy", reason="numpy not installed; ros monitor TF path requires it"
)

from std_msgs.msg import String  # noqa: E402

from blackboxrs.core.config import (  # noqa: E402
    BlackBoxConfig,
    DeadTopicConfig,
    RosMonitorConfig,
    RuntimeConfig,
)
from blackboxrs.core.event_bus import EventBus  # noqa: E402
from blackboxrs.core.session import Session  # noqa: E402
from blackboxrs.anomaly_engine.engine import AnomalyEngine  # noqa: E402
from blackboxrs.ros_monitor.monitor import RosMonitor  # noqa: E402


def _pick_test_domain_id() -> str:
    seed = f"{os.getpid()}-{time.time_ns()}"
    rng = random.Random(seed)
    return str(rng.randint(50, 200))


_TEST_DOMAIN_ID = _pick_test_domain_id()


@pytest.fixture(autouse=True)
def _isolated_ros_domain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROS_DOMAIN_ID", _TEST_DOMAIN_ID)
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")


def _shutdown_rclpy() -> None:
    try:
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass


def _make_observer_config(tmp_path: Path) -> BlackBoxConfig:
    cfg = BlackBoxConfig()
    cfg.runtime = RuntimeConfig(role="observer", observed_host="test-robot-01")
    cfg.log_dir = str(tmp_path / "logs")
    cfg.ros_monitor = RosMonitorConfig(
        enabled=True, poll_interval_sec=0.5, track_latency=False, topic_filters=[]
    )
    # Tight timeout so the test does not block 10+ seconds waiting for a
    # dead_topic anomaly. The 1.5s value gives the daemon room to register
    # the topic during Docker startup but still keeps the test under 15s.
    cfg.anomaly_engine.dead_topic = DeadTopicConfig(timeout_sec=1.5)
    cfg.apply_runtime_role()
    return cfg


def test_observer_role_disables_system_monitor_and_enables_observer_mode(
    tmp_path: Path,
) -> None:
    """Smoke: observer mode plumbing must take effect when set declaratively."""
    cfg = _make_observer_config(tmp_path)
    assert cfg.runtime.is_observer is True
    assert cfg.system_monitor.enabled is False
    assert cfg.anomaly_engine.observer_mode is True


def test_observer_fires_dead_topic_on_live_dds(tmp_path: Path) -> None:
    """End-to-end: a real publisher goes silent, observer daemon fires."""

    topic = "/observer_e2e/topic_a"
    cfg = _make_observer_config(tmp_path)

    _shutdown_rclpy()

    bus = EventBus(default_queue_maxsize=1024)
    session = Session(
        observed_host=cfg.runtime.observed_host, role=cfg.runtime.role
    )

    anomaly_q = bus.subscribe(channel="anomaly_engine")

    engine = AnomalyEngine(bus, cfg.anomaly_engine, session=session)
    monitor = RosMonitor(bus, cfg.ros_monitor, session)

    publisher_node = None
    publisher = None
    try:
        engine.start()
        monitor.start()

        publisher_node = rclpy.create_node(
            "observer_e2e_publisher", namespace="/observer_e2e"
        )
        publisher = publisher_node.create_publisher(String, topic, 10)

        # Phase 1: keep publishing for ~5 seconds so RosMonitor discovers
        # the topic, the FrequencyTracker locks the rate, and the
        # dead_topic detector learns it is alive. Docker startup can eat a
        # second or two before subscriptions are wired.
        phase1_deadline = time.monotonic() + 5.0
        while time.monotonic() < phase1_deadline:
            msg = String()
            msg.data = "alive"
            publisher.publish(msg)
            rclpy.spin_once(publisher_node, timeout_sec=0.0)
            time.sleep(0.1)

        # Phase 2: stop publishing and wait for the dead_topic timeout
        # (configured to 1.5s above) plus a generous safety margin since
        # the daemon's poll cadence (0.5s) plus frequency emission cadence
        # (1.0s) plus the 1.5s timeout means realistic time-to-fire is
        # 3-4s in the best case. 8s deadline absorbs Docker scheduler jitter.
        dead_topic_event = None
        phase2_deadline = time.monotonic() + 8.0
        while time.monotonic() < phase2_deadline:
            # Continue spinning the publisher node to keep its executor
            # responsive, but do not publish.
            rclpy.spin_once(publisher_node, timeout_sec=0.0)
            try:
                event = anomaly_q.get(timeout=0.2)
            except Empty:
                continue
            if (
                event.event_type == "anomaly.dead_topic"
                and event.data.get("metric", "").endswith(topic)
            ):
                dead_topic_event = event
                break

        assert dead_topic_event is not None, (
            "Observer-mode daemon never emitted anomaly.dead_topic for "
            f"{topic!r} within 5s after publisher silence. Observer-mode "
            "live DDS path is broken."
        )
        meta = dead_topic_event.metadata or {}
        assert meta.get("observed_host") == "test-robot-01", (
            f"observer-mode anomaly metadata missing observed_host: {meta!r}"
        )
        assert meta.get("role") == "observer", (
            f"observer-mode anomaly metadata missing role: {meta!r}"
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
        try:
            engine.stop()
        except Exception:
            pass
        _shutdown_rclpy()
