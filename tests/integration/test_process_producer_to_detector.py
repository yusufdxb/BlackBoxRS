"""Integration test: ProcessSignalsCollector -> EventBus -> ProcessSignalsDetector.

Injects synthetic process snapshots through the full producer-to-detector
pipeline and asserts the detector fires correctly.  All psutil calls are
monkeypatched; no real robot processes are required.
"""

from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

from blackboxrs.anomaly_engine.detectors.process_signals import ProcessSignalsDetector
from blackboxrs.core.config import ProcessSignalsCollectorConfig, ProcessSignalsConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.system_monitor.collectors.process import ProcessSignalsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_collector_config(
    tracked_patterns: list[str] | None = None,
    max_tracked: int = 64,
) -> ProcessSignalsCollectorConfig:
    return ProcessSignalsCollectorConfig(
        enabled=True,
        sample_hz=1.0,
        tracked_patterns=tracked_patterns or ["*ros2*", "*rclpy*", "*controller*", "*nav2*", "*moveit*"],
        max_tracked=max_tracked,
    )


def _make_fake_proc(
    pid: int,
    name: str,
    cmdline: list[str],
    cpu_percent: float,
    rss_bytes: int = 200 * 1024 * 1024,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.name.return_value = name
    proc.cmdline.return_value = cmdline
    proc.cpu_percent.return_value = cpu_percent
    proc.memory_info.return_value = MagicMock(rss=rss_bytes)
    return proc


def _run_pipeline(
    fake_procs: list[MagicMock],
    cpu_threshold: float = 90.0,
    rss_threshold: float = 1024.0,
) -> BlackBoxEvent | None:
    """Drive one producer tick through the bus and return the detector output."""
    bus = EventBus()
    sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

    with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
        collector = ProcessSignalsCollector(
            config=_make_collector_config(),
            event_bus=bus,
            session_meta={"session_id": "integ-sess"},
            is_observer=False,
        )

    with patch.object(collector, "_iter_matched_procs", return_value=fake_procs):
        collector.tick()

    bus.unsubscribe(sub)

    try:
        event = sub.get_nowait()
    except queue.Empty:
        return None

    detector = ProcessSignalsDetector(
        ProcessSignalsConfig(cpu_percent=cpu_threshold, rss_mb=rss_threshold, min_consecutive_samples=1)
    )
    return detector.check(event)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProcessProducerToDetector:
    """End-to-end: producer tick -> bus -> detector check."""

    def test_cpu_spike_fires_anomaly(self):
        """A process at 95% CPU with a 90% threshold must fire."""
        procs = [
            _make_fake_proc(1234, "scan_node", ["ros2", "run", "scan", "scan_node"], cpu_percent=95.0)
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0)
        assert anomaly is not None
        assert anomaly.event_type == "anomaly.process_signals"

    def test_anomaly_carries_process_name_fingerprint(self):
        """Anomaly data must contain process_name (not pid) as fingerprint."""
        procs = [
            _make_fake_proc(1234, "scan_node", ["ros2", "run", "scan", "scan_node"], cpu_percent=95.0)
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0)
        assert anomaly is not None
        assert anomaly.data.get("process_name") == "scan_node"

    def test_process_name_not_pid_is_fingerprint_key(self):
        """Two anomalies from the same named process (different pids) must
        produce the same fingerprint data key."""
        procs_tick1 = [
            _make_fake_proc(1000, "controller_node", ["/opt/ros/controller_manager"], cpu_percent=95.0)
        ]
        procs_tick2 = [
            _make_fake_proc(2000, "controller_node", ["/opt/ros/controller_manager"], cpu_percent=95.0)
        ]
        a1 = _run_pipeline(procs_tick1, cpu_threshold=90.0)
        a2 = _run_pipeline(procs_tick2, cpu_threshold=90.0)
        assert a1 is not None and a2 is not None
        assert a1.data["process_name"] == a2.data["process_name"] == "controller_node"

    def test_below_threshold_no_anomaly(self):
        """A process at 50% CPU with a 90% threshold must not fire."""
        procs = [
            _make_fake_proc(1234, "scan_node", ["ros2", "run", "scan", "scan_node"], cpu_percent=50.0)
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0)
        assert anomaly is None

    def test_rss_spike_fires_anomaly(self):
        """A process with high RSS but low CPU fires an RSS anomaly."""
        procs = [
            _make_fake_proc(
                1234,
                "planner_node",
                ["ros2", "run", "nav2", "planner_node"],
                cpu_percent=5.0,
                rss_bytes=3 * 1024 * 1024 * 1024,  # 3 GB
            )
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0, rss_threshold=1024.0)
        assert anomaly is not None
        assert anomaly.data.get("metric", "").startswith("process:rss_mb")

    def test_empty_proc_set_no_event_reaches_detector(self):
        """No matched processes means no event on the bus."""
        bus = EventBus()
        sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_collector_config(),
                event_bus=bus,
                session_meta={"session_id": "integ-sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[]):
            result = collector.tick()

        bus.unsubscribe(sub)
        assert result is None
        with __import__("pytest").raises(queue.Empty):
            sub.get_nowait()

    def test_observer_mode_no_event_emitted(self):
        """Observer-mode collector must not put anything on the bus."""
        bus = EventBus()
        sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_collector_config(),
                event_bus=bus,
                session_meta={"session_id": "integ-sess"},
                is_observer=True,
            )
        collector.tick()

        bus.unsubscribe(sub)
        with __import__("pytest").raises(queue.Empty):
            sub.get_nowait()

    def test_anomaly_event_source_is_anomaly_engine(self):
        """The anomaly event emitted by the detector must have the
        correct source tag so the engine loop skips it (feedback loop
        prevention)."""
        procs = [
            _make_fake_proc(1234, "scan_node", ["ros2", "run", "scan", "scan_node"], cpu_percent=95.0)
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0)
        assert anomaly is not None
        assert anomaly.source == "anomaly_engine"

    def test_all_processes_in_anomaly_data(self):
        """anomaly.data['all_processes'] must carry the full snapshot."""
        procs = [
            _make_fake_proc(1, "scan_node", ["ros2", "run", "scan", "scan_node"], cpu_percent=95.0),
            _make_fake_proc(2, "controller_node", ["/opt/ros/controller"], cpu_percent=10.0),
        ]
        anomaly = _run_pipeline(procs, cpu_threshold=90.0)
        assert anomaly is not None
        all_procs = anomaly.data.get("all_processes", [])
        assert len(all_procs) == 2
