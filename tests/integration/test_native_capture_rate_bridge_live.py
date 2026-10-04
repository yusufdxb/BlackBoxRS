"""Live C++ rate-status pipe to Python detector integration test."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty

import pytest

rclpy = pytest.importorskip(
    "rclpy", reason="rclpy not installed; skipping live native rate bridge test"
)
pytest.importorskip(
    "std_msgs.msg", reason="std_msgs not installed; skipping live native rate bridge test"
)

from std_msgs.msg import String  # noqa: E402

from blackboxrs.anomaly_engine.detectors.frequency import FrequencyDetector  # noqa: E402
from blackboxrs.core.config import (  # noqa: E402
    CaptureConfig,
    FrequencyConfig,
    RuntimeConfig,
)
from blackboxrs.core.event_bus import EventBus  # noqa: E402
from blackboxrs.core.session import Session  # noqa: E402
from blackboxrs.recording.native_process import NativeCaptureProcess  # noqa: E402


def _shutdown_rclpy() -> None:
    try:
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolated_ros_domain(monkeypatch: pytest.MonkeyPatch):
    seed = f"{os.getpid()}-{time.time_ns()}"
    monkeypatch.setenv("ROS_DOMAIN_ID", str(random.Random(seed).randint(50, 200)))
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")


def _native_package_is_built() -> bool:
    ros2 = shutil.which("ros2")
    if ros2 is None:
        return False
    result = subprocess.run(
        [ros2, "pkg", "prefix", "blackbox_capture_cpp"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def test_native_capture_rate_pipe_drives_python_frequency_detector(tmp_path: Path):
    if not _native_package_is_built():
        pytest.skip("blackbox_capture_cpp is not built in the sourced ROS workspace")

    topic = "/blackbox_native_rate_e2e/chatter"
    _shutdown_rclpy()
    rclpy.init()
    publisher_node = rclpy.create_node("blackbox_native_rate_e2e_publisher")
    publisher = publisher_node.create_publisher(String, topic, 10)
    publish_stop = threading.Event()
    publish_interval = [0.01]

    def publish_loop() -> None:
        message = String()
        message.data = "native-rate-sample"
        while not publish_stop.is_set():
            publisher.publish(message)
            rclpy.spin_once(publisher_node, timeout_sec=0.0)
            publish_stop.wait(publish_interval[0])

    publisher_thread = threading.Thread(target=publish_loop, daemon=True)
    publisher_thread.start()

    event_bus = EventBus(default_queue_maxsize=4096)
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(
            backend="cpp",
            topics=[topic],
            native_output_dir=str(tmp_path / "native"),
            native_startup_timeout_sec=8.0,
            native_shutdown_timeout_sec=5.0,
        ),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
        rate_topic_filters=[topic],
        rate_summary_period_ms=100,
    )
    detector = FrequencyDetector(
        FrequencyConfig(tolerance_percent=50.0, min_consecutive_samples=1)
    )

    try:
        process.start()
        assert process.rate_bridge_active is True

        activation = None
        learned_samples = 0
        learning_deadline = time.monotonic() + 10.0
        while learned_samples < 10 and time.monotonic() < learning_deadline:
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            if event.event_type == "capture.native_rate_bridge_active":
                activation = event
            if (
                event.event_type == "ros.frequency"
                and event.data.get("topic") == topic
                and event.data.get("frequency_hz", 0.0) >= 40.0
            ):
                assert event.metadata["frequency_source"] == "native_cpp"
                assert event.metadata["rate_coverage_complete"] is True
                assert detector.check(event) is None
                learned_samples += 1

        assert activation is not None
        assert activation.data["transport"] == "dedicated_pipe"
        assert activation.data["coverage_complete"] is True
        assert learned_samples == 10, "native C++ rate windows never reached Python"

        publish_interval[0] = 0.1
        anomaly = None
        anomaly_deadline = time.monotonic() + 5.0
        while anomaly is None and time.monotonic() < anomaly_deadline:
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            if event.event_type == "ros.frequency" and event.data.get("topic") == topic:
                anomaly = detector.check(event)

        assert anomaly is not None, "FrequencyDetector did not react to native rate drop"
        assert anomaly.event_type == "anomaly.frequency"
        assert anomaly.data["topic"] == topic
        assert "RATE_STATUS" not in process.output_tail
        assert process.rate_bridge_counters["heartbeats_received"] >= 10
        assert process.rate_bridge_counters["failovers"] == 0
    finally:
        publish_stop.set()
        publisher_thread.join(timeout=2.0)
        try:
            process.stop()
        finally:
            publisher_node.destroy_node()
            _shutdown_rclpy()
