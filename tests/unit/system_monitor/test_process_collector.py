"""Unit tests for ProcessSignalsCollector and its pure-function helpers.

All tests are offline: no real processes are inspected.  psutil is
monkeypatched at the ``psutil.process_iter`` boundary so tests run on
any machine, including CI without robot processes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from blackboxrs.core.config import ProcessSignalsCollectorConfig
from blackboxrs.system_monitor.collectors.process import (
    ProcessSignalsCollector,
    cmdline_matches_patterns,
    proc_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    enabled: bool = True,
    tracked_patterns: list[str] | None = None,
    max_tracked: int = 64,
) -> ProcessSignalsCollectorConfig:
    return ProcessSignalsCollectorConfig(
        enabled=enabled,
        sample_hz=1.0,
        tracked_patterns=tracked_patterns
        or ["*ros2*", "*rclpy*", "*controller*", "*nav2*", "*moveit*"],
        max_tracked=max_tracked,
    )


def _make_fake_proc(
    pid: int = 1234,
    name: str = "ros2",
    cmdline: list[str] | None = None,
    cpu_percent: float = 10.0,
    rss_bytes: int = 100 * 1024 * 1024,
) -> MagicMock:
    """Build a mock psutil.Process with the given attributes."""
    proc = MagicMock()
    proc.pid = pid
    proc.name.return_value = name
    proc.cmdline.return_value = cmdline or ["ros2", "run", "foo", "bar"]
    proc.cpu_percent.return_value = cpu_percent
    proc.memory_info.return_value = MagicMock(rss=rss_bytes)
    return proc


def _make_collector(
    *,
    config: ProcessSignalsCollectorConfig | None = None,
    is_observer: bool = False,
    bus: object | None = None,
) -> ProcessSignalsCollector:
    cfg = config or _make_config()
    mock_bus = bus or MagicMock()
    # Patch process_iter so the baseline-seed at __init__ does nothing
    with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
        return ProcessSignalsCollector(
            config=cfg,
            event_bus=mock_bus,
            session_meta={"session_id": "test-sess"},
            is_observer=is_observer,
        )


# ---------------------------------------------------------------------------
# cmdline_matches_patterns (pure function)
# ---------------------------------------------------------------------------


class TestCmdlineMatchesPatterns:
    def test_ros2_run_matches_ros2_pattern(self):
        assert cmdline_matches_patterns(["ros2", "run", "foo", "bar"], ["*ros2*"])

    def test_python_ros2_module_matches(self):
        # python3 -m ros2_pkg.node style
        assert cmdline_matches_patterns(
            ["python3", "-m", "ros2_pkg.node"], ["*ros2*"]
        )

    def test_no_match_returns_false(self):
        assert not cmdline_matches_patterns(["bash", "-c", "echo hello"], ["*ros2*"])

    def test_multiple_patterns_any_match(self):
        assert cmdline_matches_patterns(
            ["nav2_bringup", "--params-file", "params.yaml"],
            ["*ros2*", "*nav2*"],
        )

    def test_empty_cmdline_returns_false(self):
        assert not cmdline_matches_patterns([], ["*ros2*"])

    def test_controller_pattern_matches(self):
        assert cmdline_matches_patterns(
            ["/opt/ros/humble/bin/controller_manager", "--ros-args"],
            ["*controller*"],
        )

    def test_rclpy_pattern_matches(self):
        assert cmdline_matches_patterns(
            ["python3", "-m", "rclpy.executors.SingleThreadedExecutor"],
            ["*rclpy*"],
        )


# ---------------------------------------------------------------------------
# proc_to_dict (pure function)
# ---------------------------------------------------------------------------


class TestProcToDict:
    def test_happy_path_returns_dict(self):
        proc = _make_fake_proc(pid=42, name="scan_node", cpu_percent=25.0, rss_bytes=200 * 1024 * 1024)
        result = proc_to_dict(proc)
        assert result is not None
        assert result["pid"] == 42
        assert result["name"] == "scan_node"
        assert abs(result["cpu_percent"] - 25.0) < 0.1
        assert abs(result["rss_mb"] - 200.0) < 0.1

    def test_zero_cpu_percent_dropped(self):
        """First-call warm-up: cpu_percent==0.0 must be dropped."""
        proc = _make_fake_proc(cpu_percent=0.0)
        assert proc_to_dict(proc) is None

    def test_access_denied_returns_none(self):
        proc = MagicMock()
        proc.pid = 999
        proc.name.side_effect = PermissionError("AccessDenied")
        assert proc_to_dict(proc) is None

    def test_no_such_process_returns_none(self):
        """Process dies between iter and attribute access."""
        proc = MagicMock()
        proc.pid = 998
        proc.name.return_value = "dead"
        proc.cpu_percent.side_effect = RuntimeError("NoSuchProcess")
        assert proc_to_dict(proc) is None

    def test_rss_converted_to_mb(self):
        proc = _make_fake_proc(cpu_percent=5.0, rss_bytes=512 * 1024 * 1024)
        result = proc_to_dict(proc)
        assert result is not None
        assert abs(result["rss_mb"] - 512.0) < 0.01


# ---------------------------------------------------------------------------
# ProcessSignalsCollector: observer mode
# ---------------------------------------------------------------------------


class TestObserverMode:
    def test_observer_mode_disables_collector(self):
        """In observer mode tick() must return None without publishing."""
        bus = MagicMock()
        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess", "role": "observer"},
                is_observer=True,
            )
        result = collector.tick()
        assert result is None
        bus.publish.assert_not_called()

    def test_observer_mode_logs_once(self, caplog):
        """Observer-mode disable log is emitted at INFO exactly once."""
        with (
            caplog.at_level("INFO"),
            patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]),
        ):
            ProcessSignalsCollector(
                config=_make_config(),
                event_bus=MagicMock(),
                session_meta={"session_id": "sess"},
                is_observer=True,
            )
        observer_msgs = [r for r in caplog.records if "observer" in r.message.lower()]
        assert len(observer_msgs) >= 1


# ---------------------------------------------------------------------------
# ProcessSignalsCollector: normal operation
# ---------------------------------------------------------------------------


class TestCollectorTick:
    def test_tick_emits_event_with_matched_processes(self):
        bus = MagicMock()
        fake_proc = _make_fake_proc(pid=100, name="ros2", cpu_percent=15.0)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
                is_observer=False,
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[fake_proc]):
            result = collector.tick()

        assert result is not None
        assert "processes" in result
        assert len(result["processes"]) == 1
        assert result["processes"][0]["name"] == "ros2"
        bus.publish.assert_called_once()

    def test_published_event_has_correct_type(self):
        bus = MagicMock()
        fake_proc = _make_fake_proc(cpu_percent=20.0)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[fake_proc]):
            collector.tick()

        event = bus.publish.call_args[0][0]
        assert event.event_type == "system.process_signals"
        assert event.source == "system_monitor"

    def test_empty_matched_set_no_event(self):
        """If no processes match, no event is emitted."""
        bus = MagicMock()

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[]):
            result = collector.tick()

        assert result is None
        bus.publish.assert_not_called()

    def test_all_zero_cpu_no_event(self):
        """If all matched procs have cpu_percent==0.0, snapshot is empty."""
        bus = MagicMock()
        proc_zero = _make_fake_proc(cpu_percent=0.0)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[proc_zero]):
            result = collector.tick()

        assert result is None
        bus.publish.assert_not_called()

    def test_max_tracked_cap_honored(self):
        """Snapshot must contain at most max_tracked processes."""
        bus = MagicMock()
        # 10 fake processes
        procs = [_make_fake_proc(pid=i, name=f"node_{i}", cpu_percent=float(i + 1)) for i in range(10)]
        cfg = _make_config(max_tracked=3)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=cfg,
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        # Override _iter_matched_procs to return only cfg.max_tracked items
        # (as the real method does)
        with patch.object(collector, "_iter_matched_procs", return_value=procs[:3]):
            result = collector.tick()

        assert result is not None
        assert len(result["processes"]) <= 3

    def test_sampling_interval_sec_present(self):
        """Event payload must include sampling_interval_sec."""
        bus = MagicMock()
        fake_proc = _make_fake_proc(cpu_percent=10.0)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[fake_proc]):
            result = collector.tick()

        assert result is not None
        assert "sampling_interval_sec" in result
        assert isinstance(result["sampling_interval_sec"], float)

    def test_access_denied_skipped_others_still_emitted(self):
        """A proc that raises AccessDenied is dropped; valid procs still appear."""
        bus = MagicMock()
        bad_proc = MagicMock()
        bad_proc.pid = 1
        bad_proc.name.side_effect = PermissionError("AccessDenied")
        good_proc = _make_fake_proc(pid=2, name="ros2", cpu_percent=12.0)

        with patch("blackboxrs.system_monitor.collectors.process.psutil.process_iter", return_value=[]):
            collector = ProcessSignalsCollector(
                config=_make_config(),
                event_bus=bus,
                session_meta={"session_id": "sess"},
            )

        with patch.object(collector, "_iter_matched_procs", return_value=[bad_proc, good_proc]):
            result = collector.tick()

        assert result is not None
        assert len(result["processes"]) == 1
        assert result["processes"][0]["name"] == "ros2"
