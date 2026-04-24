"""Unit tests for anomaly-triggered rosbag2 recording."""

from __future__ import annotations

import itertools
import time
from queue import Empty, Queue
from typing import Callable

from blackboxrs.core.config import Rosbag2RecorderConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session
from blackboxrs.recording.rosbag2 import Rosbag2Recorder


class FakeProcess:
    """Small stand-in for ``subprocess.Popen`` used by recorder tests."""

    _pid_counter = itertools.count(4000)
    registry: dict[int, "FakeProcess"] = {}

    def __init__(self, *, auto_exit_code: int | None = None) -> None:
        self.pid = next(self._pid_counter)
        self._returncode: int | None = None
        self._auto_exit_code = auto_exit_code
        self.signals: list[int] = []
        self.killed = False
        FakeProcess.registry[self.pid] = self

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if self._auto_exit_code is not None:
            self._returncode = self._auto_exit_code
            return self._returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def deliver_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self._returncode = -sig


def _wait_for_event(
    queue: Queue[BlackBoxEvent],
    predicate: Callable[[BlackBoxEvent], bool],
    *,
    timeout: float = 2.0,
) -> BlackBoxEvent:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = queue.get(timeout=0.05)
        except Empty:
            continue
        if predicate(event):
            return event
    raise AssertionError("timed out waiting for recorder event")


def _fake_killpg(pid: int, sig: int) -> None:
    FakeProcess.registry[pid].deliver_signal(sig)


class TestRosbag2Recorder:
    def test_start_reports_unavailable_when_ros2_is_missing(
        self, monkeypatch, tmp_path
    ):
        del tmp_path  # not used in this test
        bus = EventBus()
        queue = bus.subscribe(channel="rosbag_recorder")
        recorder = Rosbag2Recorder(bus, Rosbag2RecorderConfig(enabled=True), Session())

        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.shutil.which",
            lambda _: None,
        )

        recorder.start()

        event = _wait_for_event(
            queue,
            lambda ev: ev.event_type == "rosbag.recorder_unavailable",
        )
        assert event.severity == "warning"
        assert event.data["reason"] == "ros2_cli_not_found"

    def test_anomaly_starts_and_stops_recording_after_duration(
        self, monkeypatch, tmp_path
    ):
        bus = EventBus()
        queue = bus.subscribe(channel="rosbag_recorder")
        process: FakeProcess | None = None

        def fake_popen(*args, **kwargs):
            del args, kwargs
            nonlocal process
            process = FakeProcess()
            return process

        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.shutil.which",
            lambda _: "/usr/bin/ros2",
        )
        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.subprocess.Popen",
            fake_popen,
        )
        monkeypatch.setattr("blackboxrs.recording.rosbag2.os.killpg", _fake_killpg)

        recorder = Rosbag2Recorder(
            bus,
            Rosbag2RecorderConfig(
                enabled=True,
                output_dir=str(tmp_path / "bags"),
                record_duration_sec=0.05,
                cooldown_sec=0.1,
            ),
            Session(),
        )

        recorder.start()
        try:
            ready = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recorder_ready",
            )
            assert ready.data["storage_id"] == "sqlite3"

            bus.publish(
                BlackBoxEvent.anomaly_event(
                    event_type="anomaly.threshold",
                    data={
                        "detector": "threshold",
                        "metric": "cpu_percent",
                        "value": 99.0,
                        "threshold": 90.0,
                        "message": "cpu hot",
                    },
                )
            )

            started = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_started",
            )
            assert started.data["trigger_event_type"] == "anomaly.threshold"
            assert started.data["command"][-1] == "-a"
            assert started.data["command"][5:7] == ["-s", "sqlite3"]

            stopped = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_stopped",
                timeout=3.0,
            )
            assert stopped.data["reason"] == "duration_elapsed"
            assert stopped.data["suppressed_triggers"] == 0
            assert process is not None
            assert process.signals == [2]
        finally:
            recorder.stop()

    def test_trigger_storms_are_suppressed_while_active_and_during_cooldown(
        self, monkeypatch, tmp_path
    ):
        bus = EventBus()
        queue = bus.subscribe(channel="rosbag_recorder")

        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.shutil.which",
            lambda _: "/usr/bin/ros2",
        )
        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(),
        )
        monkeypatch.setattr("blackboxrs.recording.rosbag2.os.killpg", _fake_killpg)

        recorder = Rosbag2Recorder(
            bus,
            Rosbag2RecorderConfig(
                enabled=True,
                output_dir=str(tmp_path / "bags"),
                record_duration_sec=0.05,
                cooldown_sec=0.2,
            ),
            Session(),
        )

        trigger = BlackBoxEvent.anomaly_event(
            event_type="anomaly.threshold",
            data={
                "detector": "threshold",
                "metric": "cpu_percent",
                "value": 95.0,
                "threshold": 90.0,
                "message": "cpu hot",
            },
        )

        recorder.start()
        try:
            _wait_for_event(queue, lambda ev: ev.event_type == "rosbag.recorder_ready")

            bus.publish(trigger)
            _wait_for_event(queue, lambda ev: ev.event_type == "rosbag.recording_started")

            bus.publish(trigger)
            active_skip = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_skipped"
                and ev.data["reason"] == "recording_active",
            )
            assert active_skip.data["trigger_event_type"] == "anomaly.threshold"

            stopped = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_stopped",
                timeout=3.0,
            )
            assert stopped.data["suppressed_triggers"] == 1

            bus.publish(trigger)
            cooldown_skip = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_skipped"
                and ev.data["reason"] == "cooldown_active",
            )
            assert cooldown_skip.data["cooldown_remaining_sec"] > 0
        finally:
            recorder.stop()

    def test_non_zero_process_exit_is_reported_as_failure(
        self, monkeypatch, tmp_path
    ):
        bus = EventBus()
        queue = bus.subscribe(channel="rosbag_recorder")

        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.shutil.which",
            lambda _: "/usr/bin/ros2",
        )
        monkeypatch.setattr(
            "blackboxrs.recording.rosbag2.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(auto_exit_code=2),
        )

        recorder = Rosbag2Recorder(
            bus,
            Rosbag2RecorderConfig(
                enabled=True,
                output_dir=str(tmp_path / "bags"),
                record_duration_sec=1.0,
                cooldown_sec=0.1,
            ),
            Session(),
        )

        recorder.start()
        try:
            _wait_for_event(queue, lambda ev: ev.event_type == "rosbag.recorder_ready")

            bus.publish(
                BlackBoxEvent.anomaly_event(
                    event_type="anomaly.threshold",
                    data={
                        "detector": "threshold",
                        "metric": "cpu_percent",
                        "value": 99.0,
                        "threshold": 90.0,
                        "message": "cpu hot",
                    },
                )
            )

            _wait_for_event(queue, lambda ev: ev.event_type == "rosbag.recording_started")
            failed = _wait_for_event(
                queue,
                lambda ev: ev.event_type == "rosbag.recording_failed",
                timeout=3.0,
            )
            assert failed.data["reason"] == "process_exited"
            assert failed.data["returncode"] == 2
        finally:
            recorder.stop()
