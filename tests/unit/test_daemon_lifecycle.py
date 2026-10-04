from __future__ import annotations

from pathlib import Path

import pytest

from blackboxrs.cli.daemon import BlackBoxDaemon
from blackboxrs.core.config import (
    AnomalyEngineConfig,
    BlackBoxConfig,
    CaptureConfig,
    RosMonitorConfig,
    SystemMonitorConfig,
)


def _native_config(tmp_path: Path, native_command: list[str]) -> BlackBoxConfig:
    return BlackBoxConfig(
        log_dir=str(tmp_path / "logs"),
        capture=CaptureConfig(
            backend="cpp",
            native_output_dir=str(tmp_path / "native"),
            native_command=native_command,
        ),
        ros_monitor=RosMonitorConfig(enabled=False),
        system_monitor=SystemMonitorConfig(enabled=False),
        anomaly_engine=AnomalyEngineConfig(enabled=False),
    )


def _isolate_pid_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pid_dir = tmp_path / "pid"
    monkeypatch.setattr(BlackBoxDaemon, "_PID_DIR", pid_dir)
    monkeypatch.setattr(BlackBoxDaemon, "_PID_FILE", pid_dir / "blackboxrs.pid")


def test_start_rolls_back_logger_when_native_command_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_pid_file(monkeypatch, tmp_path)
    calls: list[str] = []

    class FakeLoggingPipeline:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            calls.append("logging.start")

        def stop(self) -> None:
            calls.append("logging.stop")

    monkeypatch.setattr("blackboxrs.logging.LoggingPipeline", FakeLoggingPipeline)
    daemon = BlackBoxDaemon(
        _native_config(tmp_path, [str(tmp_path / "missing-native-recorder")])
    )

    with pytest.raises(FileNotFoundError):
        daemon.start()

    assert calls == ["logging.start", "logging.stop"]
    assert daemon._running is False
    assert daemon._components == []
    assert daemon._stop_event.is_set()
    assert not daemon._PID_FILE.exists()


def test_start_preserves_original_error_when_reverse_rollback_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_pid_file(monkeypatch, tmp_path)
    calls: list[str] = []
    startup_error = RuntimeError("native startup failed")

    class FakeLoggingPipeline:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            calls.append("logging.start")

        def stop(self) -> None:
            calls.append("logging.stop")
            raise LookupError("logging cleanup failed")

    class FakeAnomalyEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            calls.append("engine.start")

        def stop(self) -> None:
            calls.append("engine.stop")

    class FailingNativeCapture:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            calls.append("native.start")
            raise startup_error

        def stop(self) -> None:
            calls.append("native.stop")

    monkeypatch.setattr("blackboxrs.logging.LoggingPipeline", FakeLoggingPipeline)
    monkeypatch.setattr("blackboxrs.anomaly_engine.AnomalyEngine", FakeAnomalyEngine)
    monkeypatch.setattr("blackboxrs.recording.NativeCaptureProcess", FailingNativeCapture)
    config = _native_config(tmp_path, ["unused"])
    config.anomaly_engine.enabled = True
    daemon = BlackBoxDaemon(config)

    def fail_pid_cleanup() -> None:
        calls.append("pid.cleanup")
        raise OSError("PID cleanup failed")

    monkeypatch.setattr(daemon, "_remove_pid_file", fail_pid_cleanup)

    with pytest.raises(RuntimeError) as caught:
        daemon.start()

    assert caught.value is startup_error
    assert calls == [
        "logging.start",
        "engine.start",
        "native.start",
        "native.stop",
        "engine.stop",
        "logging.stop",
        "pid.cleanup",
    ]
    assert daemon._running is False
    assert daemon._components == []
    assert daemon._stop_event.is_set()


def test_daemon_wires_native_rate_loss_to_running_ros_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_pid_file(monkeypatch, tmp_path)
    native_instances = []
    ros_instances = []

    class FakeLoggingPipeline:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeNativeCapture:
        rate_bridge_active = True

        def __init__(self, *args, **kwargs) -> None:
            self.fallback = None
            native_instances.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def set_rate_bridge_fallback(self, callback) -> None:
            self.fallback = callback

    class FakeRosMonitor:
        def __init__(self, *args, native_frequency_bridge=False, **kwargs) -> None:
            self.native_frequency_bridge = native_frequency_bridge
            self.reasons: list[str] = []
            ros_instances.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def enable_python_frequency_fallback(self, reason: str) -> bool:
            self.reasons.append(reason)
            return True

    monkeypatch.setattr("blackboxrs.logging.LoggingPipeline", FakeLoggingPipeline)
    monkeypatch.setattr("blackboxrs.recording.NativeCaptureProcess", FakeNativeCapture)
    monkeypatch.setattr("blackboxrs.ros_monitor.RosMonitor", FakeRosMonitor)
    config = _native_config(tmp_path, ["unused"])
    config.ros_monitor.enabled = True
    daemon = BlackBoxDaemon(config)

    daemon.start()

    assert ros_instances[0].native_frequency_bridge is True
    assert native_instances[0].fallback is not None
    native_instances[0].fallback("RATE_COVERAGE_INCOMPLETE")
    assert ros_instances[0].reasons == ["RATE_COVERAGE_INCOMPLETE"]
    daemon.stop()
