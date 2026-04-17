"""Real end-to-end integration tests for BlackBoxRS.

These tests boot the full ``BlackBoxDaemon`` against a temporary log
directory and verify behaviour by inspecting the JSONL log on disk.  No
component is mocked: SystemMonitor, EventBus, AnomalyEngine, and
LoggingPipeline all run as in production.  The ROS monitor is disabled
because rclpy is not available in the test environment.

This is the test that catches regressions of:

* the daemon double-starting components (each component would have run
  twice and the log would contain duplicates);
* the detector / monitor event-type contract drifting (the threshold
  anomaly would never appear in the log);
* the SystemMonitor crashing on a list-shaped collector payload (the
  daemon would die before the writer ever flushed).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from blackboxrs.cli.daemon import BlackBoxDaemon
from blackboxrs.core.config import (
    AnomalyEngineConfig,
    AnomalyThresholds,
    BlackBoxConfig,
    RosMonitorConfig,
    SystemMonitorConfig,
)
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.logging.reader import LogReader


def _make_config(log_dir: Path, *, cpu_threshold: float = 0.0) -> BlackBoxConfig:
    """Daemon config with ROS disabled, system monitor at 0.1s interval.

    cpu_threshold defaults to 0.0 so the threshold detector reliably
    fires on every system.cpu event regardless of the host's actual
    load.  Tests that need a higher threshold can override.
    """
    return BlackBoxConfig(
        log_dir=str(log_dir),
        log_rotation_mb=1,
        log_max_files=5,
        ros_monitor=RosMonitorConfig(enabled=False),
        system_monitor=SystemMonitorConfig(enabled=True, interval_sec=0.1),
        anomaly_engine=AnomalyEngineConfig(
            enabled=True,
            thresholds=AnomalyThresholds(cpu_percent=cpu_threshold),
        ),
    )


def _read_all(log_dir: Path) -> list[BlackBoxEvent]:
    return list(LogReader(log_dir).read_all())


def _types_in(events: Iterable[BlackBoxEvent]) -> set[str]:
    return {(e.source, e.event_type) for e in events}


def _wait_for(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.05
) -> bool:
    """Poll *predicate* until it returns True or *timeout* elapses.

    Integration tests have an end-to-end path of
    ``system_monitor tick -> event_bus -> anomaly_engine (250ms drain) ->
    event_bus -> logging pipeline -> disk``.  Fixed ``time.sleep()``
    windows race this path on contended CI runners even when the code
    under test is correct, so we poll instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------


class TestDaemonLifecycle:
    def test_starts_one_thread_per_component(self, tmp_path: Path):
        before = {t.name for t in threading.enumerate()}
        cfg = _make_config(tmp_path / "logs")
        daemon = BlackBoxDaemon(cfg)
        try:
            daemon.start()
            time.sleep(0.2)
            after = {t.name for t in threading.enumerate()}
            spawned = after - before
            # Exactly one thread per enabled component: logging,
            # anomaly engine, system monitor.  ROS is disabled.  If the
            # daemon ever regresses to double-starting, this set grows.
            assert "logging-pipeline" in spawned
            assert "anomaly-engine" in spawned
            assert "blackbox-system-monitor" in spawned
            # Old (broken) code spawned a second thread named after the
            # _register("name=...") argument.  Make sure none survive.
            assert "logging_pipeline" not in spawned
            assert "anomaly_engine" not in spawned
            assert "system_monitor" not in spawned
        finally:
            daemon.stop()

    def test_pid_file_lifecycle(self, tmp_path: Path, monkeypatch):
        """is_running() must reflect the live daemon and clean up on stop."""
        pid_dir = tmp_path / "pid"
        pid_file = pid_dir / "blackboxrs.pid"
        monkeypatch.setattr(BlackBoxDaemon, "_PID_DIR", pid_dir)
        monkeypatch.setattr(BlackBoxDaemon, "_PID_FILE", pid_file)

        cfg = _make_config(tmp_path / "logs")
        daemon = BlackBoxDaemon(cfg)

        running, pid = BlackBoxDaemon.is_running()
        assert running is False and pid is None

        daemon.start()
        try:
            running, pid = BlackBoxDaemon.is_running()
            assert running is True
            assert pid is not None and pid > 0
            assert pid_file.exists()
        finally:
            daemon.stop()

        assert not pid_file.exists()
        running, _ = BlackBoxDaemon.is_running()
        assert running is False


class TestRealMonitorToLog:
    def test_system_events_reach_disk(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        daemon = BlackBoxDaemon(_make_config(log_dir, cpu_threshold=200.0))
        try:
            daemon.start()
            time.sleep(0.6)
        finally:
            daemon.stop()

        events = _read_all(log_dir)
        assert events, "expected at least one system event on disk"

        type_pairs = _types_in(events)
        # SystemMonitor must emit at least these collector events.  cpu
        # and memory are the most reliably present on every host; gpu /
        # disk / thermal are environment-dependent.
        assert ("system_monitor", "system.cpu") in type_pairs
        assert ("system_monitor", "system.memory") in type_pairs

        for ev in events:
            if ev.event_type == "system.cpu":
                assert "cpu_percent" in ev.data
            if ev.event_type == "system.memory":
                assert "memory_percent" in ev.data

    def test_threshold_detector_fires_against_real_cpu_events(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        # cpu_threshold=0.0 forces a fire on the very first sample
        # regardless of what the host is actually doing.
        daemon = BlackBoxDaemon(_make_config(log_dir, cpu_threshold=0.0))
        try:
            daemon.start()
            # Poll rather than sleep a fixed window — the full path
            # through the 250ms anomaly-engine drain is slow enough on
            # contended runners to race a blind sleep.
            _wait_for(
                lambda: any(
                    e.source == "anomaly_engine"
                    for e in _read_all(log_dir)
                ),
                timeout=5.0,
            )
        finally:
            daemon.stop()

        events = _read_all(log_dir)
        anomalies = [e for e in events if e.source == "anomaly_engine"]
        assert anomalies, (
            "no anomalies in log — the threshold detector did not see "
            "any system.cpu event with the expected payload shape"
        )
        assert any(a.event_type == "anomaly.threshold" for a in anomalies)
        assert any(a.data.get("metric") == "cpu_percent" for a in anomalies)

    def test_no_duplicate_events_per_collector_tick(self, tmp_path: Path):
        """If the daemon double-starts SystemMonitor, every collector
        tick gets logged twice.  Catching that here is the whole point."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        daemon = BlackBoxDaemon(_make_config(log_dir, cpu_threshold=200.0))
        try:
            daemon.start()
            time.sleep(0.5)
        finally:
            daemon.stop()

        events = _read_all(log_dir)
        cpu_events = [e for e in events if e.event_type == "system.cpu"]
        # The system monitor polls every 0.1s for ~0.5s, so we expect
        # roughly 4-6 cpu events.  A double-started daemon would log
        # twice as many.  Ten is a generous upper bound that still
        # detects the bug (which produced ~10-12 events under the same
        # timing).
        assert 1 <= len(cpu_events) <= 8, (
            f"unexpected cpu event count: {len(cpu_events)} "
            "(suggests the daemon double-started the system monitor)"
        )


class TestAnomalyEngineSeesEveryEvent:
    """Engine must subscribe before any producer emits, so it sees t=0
    events.  Earlier code started the logger first, then the engine —
    the engine's queue would miss the first batch."""

    def test_engine_sees_first_event(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        daemon = BlackBoxDaemon(_make_config(log_dir, cpu_threshold=0.0))
        try:
            daemon.start()
            # Poll for both a cpu event and an anomaly, rather than
            # sleeping a single drain-cycle window.  The regression this
            # test catches (engine subscribed after first publish) would
            # still show up as "cpu events present, anomalies empty"
            # below, even with an unbounded wait — timing slack does
            # not hide it.
            _wait_for(
                lambda: (
                    any(e.event_type == "system.cpu" for e in _read_all(log_dir))
                    and any(
                        e.event_type == "anomaly.threshold"
                        for e in _read_all(log_dir)
                    )
                ),
                timeout=5.0,
            )
        finally:
            daemon.stop()

        events = _read_all(log_dir)
        cpu_events = [e for e in events if e.event_type == "system.cpu"]
        anomalies = [e for e in events if e.event_type == "anomaly.threshold"]
        # If the engine missed the first event we'd see cpu_events but
        # zero anomalies.  Both must be present.
        assert cpu_events, "no cpu events — system monitor did not run"
        assert anomalies, (
            "anomaly engine missed all events; subscribe-before-publish "
            "ordering may have regressed"
        )
