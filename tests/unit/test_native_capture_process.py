from __future__ import annotations

import io
import json
import os
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
        self.child_fds: list[int] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        for descriptor in self.child_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.child_fds.clear()
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


def _healthy_rate_coverage() -> dict[str, object]:
    return {
        "coverage_complete": True,
        "topic_coverage_truncated": False,
        "graph_coverage_faults": 0,
        "graph_snapshot_failures": 0,
        "endpoint_query_failures": 0,
        "subscription_failures": 0,
        "ambiguous_topic_types": 0,
    }


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
        child_rate_fd = os.open(
            kwargs["env"]["BLACKBOXRS_RATE_STATUS_PIPE"],
            os.O_WRONLY | os.O_NONBLOCK,
        )
        fake.child_fds.append(child_rate_fd)
        heartbeat = {
            "schema_version": "blackboxrs.capture_rate_status.v1",
            "session_id": "ready",
            "window_start_monotonic_ns": 1,
            "window_end_monotonic_ns": 1_000_000_001,
            "batch_index": 0,
            "batch_count": 1,
            "topics_truncated": False,
            **_healthy_rate_coverage(),
            "topics": [],
        }
        os.write(child_rate_fd, f"RATE_STATUS {json.dumps(heartbeat)}\n".encode())
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
    rate_pipe = Path(launched["kwargs"]["env"]["BLACKBOXRS_RATE_STATUS_PIPE"])
    assert rate_pipe.name == "status.fifo"
    assert process.rate_bridge_active is True
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
        **_healthy_rate_coverage(),
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
    assert first.metadata["rate_coverage_complete"] is True
    assert first.metadata["message_count"] == 400
    assert first.metadata["native_capture_session_id"] == "native"
    assert process.rate_bridge_counters == {
        "status_lines": 1,
        "events_published": 2,
        "status_rejected": 0,
        "coverage_faults": 0,
        "heartbeats_received": 1,
        "failovers": 0,
        "fallback_callback_failures": 0,
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
        **_healthy_rate_coverage(),
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


def _assert_bad_numeric_rate_line_is_bounded(
    tmp_path: Path,
    *,
    digit_count: int,
    expected_reason: str,
) -> None:
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    bad_line = (
        "RATE_STATUS {"
        '"schema_version":"blackboxrs.capture_rate_status.v1",'
        '"session_id":"native",'
        '"window_start_monotonic_ns":1,'
        '"window_end_monotonic_ns":1000000001,'
        '"batch_index":0,"batch_count":1,"topics_truncated":false,'
        '"coverage_complete":true,"topic_coverage_truncated":false,'
        '"graph_coverage_faults":0,"graph_snapshot_failures":0,'
        '"endpoint_query_failures":0,"subscription_failures":0,'
        '"ambiguous_topic_types":0,'
        '"topics":[{"topic":"/bad","message_count":'
        + ("9" * digit_count)
        + ',"frequency_hz":1.0,"interval_ms":1.0}]}\n'
    ).encode()
    valid_status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 2_000_000_000,
        "window_end_monotonic_ns": 3_000_000_000,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [
            {
                "topic": "/good",
                "message_count": 10,
                "frequency_hz": 10.0,
                "interval_ms": 100.0,
            }
        ],
    }
    good_line = f"RATE_STATUS {json.dumps(valid_status)}\n".encode()
    stream = _AvailableChunkStream([bad_line, good_line])

    process._drain_output(stream)  # type: ignore[arg-type]

    fault = events.get_nowait()
    frequency = events.get_nowait()
    assert events.empty()
    assert fault.event_type == "capture.native_rate_bridge_fault"
    assert fault.data["state"] == "RATE_STATUS_REJECTED"
    assert fault.data["reason"] == expected_reason
    assert frequency.event_type == "ros.frequency"
    assert frequency.data["topic"] == "/good"
    assert frequency.data["frequency_hz"] == 10.0
    assert process.rate_bridge_counters == {
        "status_lines": 2,
        "events_published": 1,
        "status_rejected": 1,
        "coverage_faults": 0,
        "heartbeats_received": 1,
        "failovers": 0,
        "fallback_callback_failures": 0,
    }
    assert stream.closed is True


def test_native_process_rejects_overflowing_rate_count_and_keeps_draining(
    tmp_path: Path,
):
    _assert_bad_numeric_rate_line_is_bounded(
        tmp_path,
        digit_count=4000,
        expected_reason="invalid topic entry",
    )


def test_native_process_rejects_json_digit_limit_and_keeps_draining(tmp_path: Path):
    _assert_bad_numeric_rate_line_is_bounded(
        tmp_path,
        digit_count=5000,
        expected_reason="malformed rate-status JSON",
    )


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
                    **_healthy_rate_coverage(),
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
        "coverage_faults": 0,
        "heartbeats_received": 1,
        "failovers": 0,
        "fallback_callback_failures": 0,
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
        **_healthy_rate_coverage(),
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
        "coverage_faults": 0,
        "heartbeats_received": 0,
        "failovers": 0,
        "fallback_callback_failures": 0,
    }


def test_native_process_empty_rate_window_is_heartbeat_not_liveness(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    heartbeat = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [],
    }

    process._inspect_output_line(
        f"RATE_STATUS {json.dumps(heartbeat)}".encode(),
        trusted_rate_channel=True,
    )

    assert events.empty()
    assert process.rate_bridge_counters["heartbeats_received"] == 1
    assert process.rate_bridge_counters["events_published"] == 0


@pytest.mark.parametrize(
    "fault_field",
    [
        "topic_coverage_truncated",
        "graph_coverage_faults",
        "graph_snapshot_failures",
        "endpoint_query_failures",
        "subscription_failures",
        "ambiguous_topic_types",
    ],
)
def test_native_process_runtime_coverage_fault_fails_over_once(
    tmp_path: Path,
    fault_field: str,
):
    event_bus = EventBus()
    events = event_bus.subscribe()
    fallback_reasons: list[str] = []
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    process.set_rate_bridge_fallback(fallback_reasons.append)
    process._rate_transport_started = True
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [],
    }
    status["coverage_complete"] = False
    status[fault_field] = True if fault_field == "topic_coverage_truncated" else 1

    process._inspect_output_line(
        f"RATE_STATUS {json.dumps(status)}".encode(),
        trusted_rate_channel=True,
    )

    coverage_fault = events.get_nowait()
    failover = events.get_nowait()
    assert events.empty()
    assert coverage_fault.event_type == "capture.native_rate_bridge_coverage_fault"
    assert coverage_fault.data["state"] == "RATE_COVERAGE_INCOMPLETE"
    assert coverage_fault.data[fault_field] == status[fault_field]
    assert failover.event_type == "capture.native_rate_bridge_failover"
    assert failover.data["reason"] == "RATE_COVERAGE_INCOMPLETE"
    assert fallback_reasons == ["RATE_COVERAGE_INCOMPLETE"]
    assert process.rate_bridge_active is False
    assert process.rate_bridge_counters["coverage_faults"] == 1
    assert process.rate_bridge_counters["status_rejected"] == 1
    assert process.rate_bridge_counters["events_published"] == 0


@pytest.mark.parametrize(
    "invalid_mutation",
    [
        ("missing", "coverage_complete"),
        ("missing", "endpoint_query_failures"),
        ("value", "subscription_failures"),
        ("inconsistent", "ambiguous_topic_types"),
    ],
)
def test_native_process_requires_bounded_consistent_coverage_fields(
    tmp_path: Path,
    invalid_mutation: tuple[str, str],
):
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    process._rate_transport_started = True
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [],
    }
    mutation, field = invalid_mutation
    if mutation == "missing":
        status.pop(field)
    elif mutation == "value":
        status[field] = (1 << 64)
    else:
        status[field] = 1

    process._inspect_output_line(
        f"RATE_STATUS {json.dumps(status)}".encode(),
        trusted_rate_channel=True,
    )

    fault = events.get_nowait()
    failover = events.get_nowait()
    assert fault.event_type == "capture.native_rate_bridge_coverage_fault"
    assert failover.data["reason"] == "RATE_COVERAGE_INCOMPLETE"
    assert process.rate_bridge_counters["coverage_faults"] == 1
    assert process.rate_bridge_counters["heartbeats_received"] == 0


def test_native_process_runtime_heartbeat_loss_fails_over_once(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    fallback_reasons: list[str] = []
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
        rate_summary_period_ms=10,
    )
    process.set_rate_bridge_fallback(fallback_reasons.append)
    process._rate_transport_started = True
    heartbeat = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 10_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [],
    }
    process._inspect_output_line(
        f"RATE_STATUS {json.dumps(heartbeat)}".encode(),
        trusted_rate_channel=True,
    )
    activated = events.get_nowait()
    assert activated.event_type == "capture.native_rate_bridge_active"
    assert process.rate_bridge_active is True
    assert process._rate_last_heartbeat is not None

    timed_out = process._check_rate_bridge_timeout(
        process._rate_last_heartbeat + process._rate_heartbeat_timeout_sec + 0.01
    )

    assert timed_out is True
    assert process.rate_bridge_active is False
    assert fallback_reasons == ["HEARTBEAT_TIMEOUT"]
    failover = events.get_nowait()
    assert failover.event_type == "capture.native_rate_bridge_failover"
    assert failover.data["reason"] == "HEARTBEAT_TIMEOUT"
    assert process._check_rate_bridge_timeout(time.monotonic() + 10.0) is False
    assert fallback_reasons == ["HEARTBEAT_TIMEOUT"]
    assert process.rate_bridge_counters["failovers"] == 1


def test_native_process_rate_rejection_fails_over_and_keeps_draining(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    fallback_reasons: list[str] = []
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    process.set_rate_bridge_fallback(fallback_reasons.append)
    process._rate_transport_started = True
    valid = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [
            {
                "topic": "/still_draining",
                "message_count": 1,
                "frequency_hz": 1.0,
                "interval_ms": 1000.0,
            }
        ],
    }
    stream = _AvailableChunkStream(
        [b"RATE_STATUS {not-json}\n", f"RATE_STATUS {json.dumps(valid)}\n".encode()]
    )

    process._drain_output(stream, rate_channel=True)  # type: ignore[arg-type]

    fault = events.get_nowait()
    failover = events.get_nowait()
    assert events.empty()
    assert fault.event_type == "capture.native_rate_bridge_fault"
    assert failover.event_type == "capture.native_rate_bridge_failover"
    assert fallback_reasons == ["RATE_STATUS_REJECTED"]
    assert process.rate_bridge_counters["status_lines"] == 2
    assert process.rate_bridge_counters["status_rejected"] == 1
    assert process.rate_bridge_counters["events_published"] == 0
    assert stream.closed is True


def test_native_process_rejects_rate_status_from_stdout_when_pipe_expected(
    tmp_path: Path,
):
    event_bus = EventBus()
    events = event_bus.subscribe()
    fallback_reasons: list[str] = []
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    process.set_rate_bridge_fallback(fallback_reasons.append)
    process._rate_transport_started = True
    status = {
        "schema_version": "blackboxrs.capture_rate_status.v1",
        "session_id": "native",
        "window_start_monotonic_ns": 1,
        "window_end_monotonic_ns": 1_000_000_001,
        "batch_index": 0,
        "batch_count": 1,
        "topics_truncated": False,
        **_healthy_rate_coverage(),
        "topics": [],
    }

    process._inspect_output_line(f"RATE_STATUS {json.dumps(status)}".encode())

    fault = events.get_nowait()
    failover = events.get_nowait()
    assert events.empty()
    assert fault.data["reason"] == "rate status arrived outside dedicated transport"
    assert failover.event_type == "capture.native_rate_bridge_failover"
    assert fallback_reasons == ["RATE_STATUS_REJECTED"]
    assert process.rate_bridge_active is False
    assert process.rate_bridge_counters["status_rejected"] == 1


def test_native_process_counts_fallback_callback_failure(tmp_path: Path):
    event_bus = EventBus()
    events = event_bus.subscribe()
    calls = 0

    def fail_fallback(_reason: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("fallback unavailable")

    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
    )
    process._rate_transport_started = True
    process.set_rate_bridge_fallback(fail_fallback)

    assert process._trigger_rate_bridge_failover("RATE_PIPE_CLOSED") is True
    assert process._trigger_rate_bridge_failover("RATE_PIPE_CLOSED") is False

    assert calls == 1
    assert events.get_nowait().event_type == "capture.native_rate_bridge_failover"
    assert events.get_nowait().event_type == "capture.native_rate_bridge_fallback_failed"
    assert process.rate_bridge_counters["fallback_callback_failures"] == 1


def test_native_process_bounds_initial_activation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        fake.child_fds.append(
            os.open(
                kwargs["env"]["BLACKBOXRS_RATE_STATUS_PIPE"],
                os.O_WRONLY | os.O_NONBLOCK,
            )
        )
        return fake

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    monkeypatch.setattr(
        "blackboxrs.recording.native_process._RATE_HEARTBEAT_MIN_TIMEOUT_SEC", 0.02
    )
    event_bus = EventBus()
    events = event_bus.subscribe()
    process = NativeCaptureProcess(
        CaptureConfig(backend="cpp", native_output_dir=str(tmp_path)),
        RuntimeConfig(),
        event_bus,
        Session(),
        publish_rate_events=True,
        rate_summary_period_ms=10,
    )

    started_at = time.monotonic()
    process.start()
    elapsed = time.monotonic() - started_at
    fallback_reasons: list[str] = []
    process.set_rate_bridge_fallback(fallback_reasons.append)

    assert elapsed < 0.5
    assert process.rate_bridge_active is False
    assert fallback_reasons == ["ACTIVATION_TIMEOUT"]
    event = events.get_nowait()
    assert event.event_type == "capture.native_rate_bridge_failover"
    assert event.data["reason"] == "ACTIVATION_TIMEOUT"
    process.stop()


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
