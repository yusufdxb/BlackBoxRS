from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from blackboxrs.core.config import CaptureConfig, RuntimeConfig
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
    process = NativeCaptureProcess(config, RuntimeConfig())

    process.start()

    command = launched["command"]
    assert command[:4] == ["ros2", "run", "blackbox_capture_cpp", "blackbox_capture"]
    assert launched["kwargs"]["stdout"] is subprocess.PIPE
    params_path = Path(command[-1])
    params = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    node_params = params["/blackbox/blackbox_capture"]["ros__parameters"]
    assert node_params["capture.topics"] == ["/imu/data"]
    assert node_params["capture.discover_all"] is False
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
