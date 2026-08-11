from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from blackboxrs.cli.daemon import _native_rate_bridge_has_full_coverage
from blackboxrs.core.config import BlackBoxConfig, CaptureConfig, RuntimeConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session
from blackboxrs.recording.native import resolve_current_native_session
from blackboxrs.recording.native_process import NativeCaptureProcess


class _FakeProcess:
    pid = 4242

    def __init__(self, output: bytes = b"") -> None:
        self.returncode = None
        self.stdout = io.BytesIO(output)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class _AvailableChunkStream:
    """Pipe stand-in whose blocking read path must never be selected."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter([*chunks, b""])
        self.closed = False

    def read1(self, _size: int) -> bytes:
        return next(self._chunks)

    def read(self, _size: int) -> bytes:
        raise AssertionError("blocking buffered read was used")

    def close(self) -> None:
        self.closed = True


def test_native_process_uses_installed_executable_and_daemon_parameters(
    tmp_path: Path, monkeypatch
):
    launched: dict[str, object] = {}
    fake = _FakeProcess()

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        pointer = tmp_path / "current_session.json"
        session = tmp_path / "capture_ready"
        session.mkdir()
        pointer.write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: signals.append((pid, sig)))
    config = CaptureConfig(backend="cpp", topics=["/imu/data"], native_output_dir=str(tmp_path))
    process = NativeCaptureProcess(config, RuntimeConfig(), publish_rate_events=True)

    process.start()

    command = launched["command"]
    assert command[:4] == ["ros2", "run", "blackbox_capture_cpp", "blackbox_capture"]
    assert launched["kwargs"]["stdout"] is subprocess.PIPE
    params_path = Path(command[-1])
    params = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    node_params = params["/blackbox/blackbox_capture"]["ros__parameters"]
    assert node_params["capture.topics"] == ["/imu/data"]
    assert node_params["capture.discover_all"] is False
    assert node_params["status.rate_summary_period_ms"] == 1000
    assert resolve_current_native_session(tmp_path) == tmp_path / "capture_ready"

    process.stop()

    assert signals
    assert not params_path.exists()
    assert not (tmp_path / "native_capture.log").exists()


def test_current_session_resolver_rejects_path_traversal(tmp_path: Path):
    (tmp_path / "current_session.json").write_text(
        json.dumps(
            {
                "schema_version": "blackboxrs.current_capture.v1",
                "session_id": "bad",
                "path": "../capture_bad",
            }
        ),
        encoding="utf-8",
    )

    assert resolve_current_native_session(tmp_path) is None


def test_native_process_rejects_malformed_ready_pointer(tmp_path: Path, monkeypatch):
    fake = _FakeProcess()

    def fake_popen(command, **kwargs):
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "path": "../capture_escape",
                }
            ),
            encoding="utf-8",
        )
        return fake

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: signals.append((pid, sig)))
    config = CaptureConfig(
        backend="cpp",
        native_output_dir=str(tmp_path),
        native_startup_timeout_sec=0.01,
    )
    process = NativeCaptureProcess(config, RuntimeConfig())

    with pytest.raises(RuntimeError, match="did not publish READY"):
        process.start()

    assert signals
    assert not list((tmp_path / "runtime").glob("native_capture_*.yaml"))


def test_native_process_drains_output_into_bounded_tail(tmp_path: Path, monkeypatch):
    marker = b"final native diagnostic"
    fake = _FakeProcess(b"x" * (70 * 1024) + marker)

    def fake_popen(command, **kwargs):
        session = tmp_path / "capture_ready"
        session.mkdir()
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
    )

    process.start()
    process.stop()

    tail = process.output_tail.encode()
    assert len(tail) <= 64 * 1024
    assert tail.endswith(marker)
    assert not (tmp_path / "native_capture.log").exists()


def test_native_process_includes_bounded_tail_in_startup_error(tmp_path: Path, monkeypatch):
    fake = _FakeProcess(b"fatal native configuration\n")
    fake.returncode = 2
    monkeypatch.setattr("subprocess.Popen", lambda command, **kwargs: fake)

    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
    )

    with pytest.raises(RuntimeError, match="fatal native configuration"):
        process.start()

    assert process.output_tail == "fatal native configuration\n"
    assert not list((tmp_path / "runtime").glob("native_capture_*.yaml"))


def test_native_process_reports_post_ready_child_exit(tmp_path: Path, monkeypatch):
    fake = _FakeProcess()

    def fake_popen(command, **kwargs):
        session = tmp_path / "capture_ready"
        session.mkdir()
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
    )

    process.start()
    fake.returncode = 7
    deadline = time.monotonic() + 1.0
    while process.unexpected_exit_code is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert process.unexpected_exit_code == 7
    event = events.get(timeout=1.0)
    assert event.source == "native_capture"
    assert event.event_type == "capture.native_process_exit"
    assert event.severity == "error"
    assert event.data["evidence_complete"] is False
    process.stop()


def test_native_process_surfaces_machine_readable_storage_fault(tmp_path: Path, monkeypatch):
    status = {
        "schema_version": "blackboxrs.capture_status.v1",
        "state": "STORAGE_FAULT",
        "storage_errors": 1,
        "dropped": 7,
        "dropped_bytes": 4096,
    }
    fake = _FakeProcess(f"HEALTH_STATUS {json.dumps(status)}\n".encode())

    def fake_popen(command, **kwargs):
        session = tmp_path / "capture_ready"
        session.mkdir()
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
    )

    process.start()
    event = events.get(timeout=1.0)

    assert event.event_type == "capture.native_health_fault"
    assert event.data["state"] == "STORAGE_FAULT"
    assert event.data["dropped"] == 7
    assert process.latest_status == status
    process.stop()


def test_native_process_requires_authoritative_clean_final_status(
    tmp_path: Path, monkeypatch
):
    fake = _FakeProcess()

    def fake_popen(command, **kwargs):
        session = tmp_path / "capture_ready"
        session.mkdir()
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
    )

    process.start()
    process.stop()

    event = events.get(timeout=1.0)
    assert event.event_type == "capture.native_shutdown_incomplete"
    assert event.data["state"] == "FINAL_STATUS_UNAVAILABLE"
    assert event.data["evidence_complete"] is False


def test_native_process_discards_oversized_unterminated_status_line(
    tmp_path: Path, monkeypatch
):
    fake = _FakeProcess(b"HEALTH_STATUS " + b"x" * (70 * 1024))

    def fake_popen(command, **kwargs):
        session = tmp_path / "capture_ready"
        session.mkdir()
        (tmp_path / "current_session.json").write_text(
            json.dumps(
                {
                    "schema_version": "blackboxrs.current_capture.v1",
                    "session_id": "ready",
                    "path": session.name,
                }
            ),
            encoding="utf-8",
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
    )

    process.start()
    process.stop()

    assert process.latest_status is None
    assert len(process.output_tail.encode()) <= 64 * 1024


def test_native_process_bridges_batched_cpp_rates_into_ros_events(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1_000_000_000,
        "window_end_monotonic_ns": 2_000_000_000,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        "topics": [
            {
                "topic": "/imu/data",
                "message_count": 400,
                "frequency_hz": 400.0,
                "interval_ms": 2.5,
            },
            {
                "topic": "/joint_states",
                "message_count": 100,
                "frequency_hz": 100.0,
                "interval_ms": 10.0,
            },
        ],
    }

    process._inspect_output_line(f"RATE_STATUS {json.dumps(status)}".encode())

    first = events.get_nowait()
    second = events.get_nowait()
    assert [first.data["topic"], second.data["topic"]] == [
        "/imu/data",
        "/joint_states",
    ]
    assert first.source == "ros_monitor"
    assert first.event_type == "ros.frequency"
    assert first.data["frequency_hz"] == 400.0
    assert first.data["interval_ms"] == 2.5
    assert first.metadata["capture_backend"] == "cpp"
    assert first.metadata["frequency_source"] == "native_cpp"
    assert first.metadata["message_count"] == 400
    assert first.metadata["native_capture_session_id"] == "native"
    assert process.rate_bridge_counters == {
        "status_lines": 1,
        "events_published": 2,
        "status_rejected": 0,
    }


def test_native_process_consumes_available_rate_line_without_full_buffer(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        "topics": [
            {
                "topic": "/imu/data",
                "message_count": 100,
                "frequency_hz": 100.0,
                "interval_ms": 10.0,
            }
        ],
    }
    stream = _AvailableChunkStream(
        [f"RATE_STATUS {json.dumps(status)}\n".encode()]
    )

    process._drain_output(stream)  # type: ignore[arg-type]

    event = events.get_nowait()
    assert event.data["topic"] == "/imu/data"
    assert stream.closed is True


def test_native_process_assembles_batches_and_aggregates_type_churn(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
        rate_topic_filters=["/imu/*"],
    )

    def batch(index: int, topics: list[dict[str, object]]) -> bytes:
        return (
            "RATE_STATUS "
            + json.dumps(
                {
                    "schema_version": "blackboxrs.capture_rate_status.v1",
                    "session_id": "native",
                    "window_start_monotonic_ns": 1_000_000_000,
                    "window_end_monotonic_ns": 2_000_000_000,
                    "batch_index": index,
                    "batch_count": 2,
                    "topics_truncated": False,
                    "topics": topics,
                }
            )
        ).encode()

    process._inspect_output_line(
        batch(
            0,
            [
                {
                    "topic": "/imu/data",
                    "message_count": 40,
                    "frequency_hz": 40.0,
                    "interval_ms": 25.0,
                }
            ],
        )
    )
    assert events.empty()
    process._inspect_output_line(
        batch(
            1,
            [
                {
                    "topic": "/imu/data",
                    "message_count": 60,
                    "frequency_hz": 60.0,
                    "interval_ms": 1000.0 / 60.0,
                },
                {
                    "topic": "/joint_states",
                    "message_count": 50,
                    "frequency_hz": 50.0,
                    "interval_ms": 20.0,
                },
                {
                    "topic": "/blackbox/internal",
                    "message_count": 10,
                    "frequency_hz": 10.0,
                    "interval_ms": 100.0,
                },
            ],
        )
    )

    event = events.get_nowait()
    assert events.empty()
    assert event.data == {
        "topic": "/imu/data",
        "frequency_hz": 100.0,
        "interval_ms": 10.0,
    }
    assert event.metadata["message_count"] == 100
    assert event.metadata["rate_batch_count"] == 2
    assert process.rate_bridge_counters == {
        "status_lines": 2,
        "events_published": 1,
        "status_rejected": 0,
    }


def test_native_process_rejects_malformed_rate_status_atomically(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "window_start_monotonic_ns": 10,
        "window_end_monotonic_ns": 20,
        "topics_truncated": False,
        "topics": [
            {
                "topic": "relative_topic",
                "message_count": 1,
                "frequency_hz": 1.0,
                "interval_ms": 1000.0,
            }
        ],
    }

    process._inspect_output_line(f"RATE_STATUS {json.dumps(status)}".encode())

    fault = events.get_nowait()
    assert fault.source == "native_capture"
    assert fault.event_type == "capture.native_rate_bridge_fault"
    assert fault.data["state"] == "RATE_STATUS_REJECTED"
    assert events.empty()
    assert process.rate_bridge_counters == {
        "status_lines": 1,
        "events_published": 0,
        "status_rejected": 1,
    }


@pytest.mark.parametrize(
    ("topics", "filters", "expected"),
    [
        ([], [], True),
        (["/imu/data"], [], False),
        (["/imu/data", "/joint_states"], ["/imu/data"], True),
        (["/imu/data"], ["/imu/*"], False),
        (["/imu/data"], ["/joint_states"], False),
    ],
)
def test_native_rate_bridge_requires_full_monitor_coverage(
    topics: list[str], filters: list[str], expected: bool
):
    config = BlackBoxConfig.default()
    config.capture.backend = "cpp"
    config.capture.topics = topics
    config.ros_monitor.topic_filters = filters

    assert _native_rate_bridge_has_full_coverage(config) is expected
