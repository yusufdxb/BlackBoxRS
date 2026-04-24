"""Anomaly-triggered rosbag2 recording."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

from blackboxrs.core.config import Rosbag2RecorderConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SEC = 0.25
_STOP_WAIT_SEC = 5.0
_PROTECTED_QUEUE_FACTOR = 4
_PROTECTED_QUEUE_MIN = 4096


@dataclass
class _RecordingState:
    process: subprocess.Popen[bytes]
    started_monotonic: float
    stop_deadline: float
    output_dir: Path
    command: list[str]
    trigger_event_type: str
    trigger_metric: str | None
    suppressed_triggers: int = 0


class Rosbag2Recorder:
    """Record a bounded rosbag2 capture when an anomaly event fires."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Rosbag2RecorderConfig,
        session: Session,
    ) -> None:
        self._event_bus = event_bus
        self._config = config
        self._session = session
        self._running = False
        self._queue: Queue[BlackBoxEvent] | None = None
        self._thread: threading.Thread | None = None
        self._active: _RecordingState | None = None
        self._active_skip_reported = False
        self._cooldown_until = 0.0
        self._cooldown_skip_reported = False
        self._started_recordings = 0
        self._max_recordings_reported = False
        self._ros2_path: str | None = None

    def start(self) -> None:
        if not self._config.enabled:
            logger.info("Rosbag2 recorder is disabled by configuration")
            return

        if self._running:
            logger.warning("Rosbag2Recorder.start() called but already running")
            return

        ros2_path = self._resolve_ros2_binary()
        if ros2_path is None:
            self._publish_event(
                "rosbag.recorder_unavailable",
                severity="warning",
                data={
                    "reason": "ros2_cli_not_found",
                    "executable": self._config.executable,
                    "output_dir": os.path.expanduser(self._config.output_dir),
                },
            )
            return

        self._ros2_path = ros2_path
        queue_size = max(
            _PROTECTED_QUEUE_MIN,
            self._event_bus.default_queue_maxsize * _PROTECTED_QUEUE_FACTOR,
        )
        self._queue = self._event_bus.subscribe(
            channel="anomaly_engine",
            maxsize=queue_size,
        )
        self._running = True
        self._thread = threading.Thread(
            target=self.run,
            name="rosbag2-recorder",
            daemon=True,
        )
        self._thread.start()
        self._publish_event(
            "rosbag.recorder_ready",
            data={
                "executable": ros2_path,
                "output_dir": os.path.expanduser(self._config.output_dir),
                "record_duration_sec": self._config.record_duration_sec,
                "cooldown_sec": self._config.cooldown_sec,
                "storage_id": self._config.storage_id,
                "topics": list(self._config.topics),
                "trigger_event_types": list(self._config.trigger_event_types),
            },
        )
        logger.info("Rosbag2 recorder started")

    def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        self._stop_active_recording(reason="daemon_stopping")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._queue is not None:
            self._event_bus.unsubscribe(self._queue, channel="anomaly_engine")
            self._queue = None
        logger.info("Rosbag2 recorder stopped")

    def run(self) -> None:
        if self._queue is None:
            logger.error("run() called without an active subscription")
            return

        while self._running:
            self._poll_active_recording()
            if (
                self._cooldown_skip_reported
                and time.monotonic() >= self._cooldown_until
            ):
                self._cooldown_skip_reported = False
            try:
                event = self._queue.get(timeout=_DRAIN_TIMEOUT_SEC)
            except Empty:
                continue
            self._handle_trigger(event)

    def _handle_trigger(self, event: BlackBoxEvent) -> None:
        if not event.event_type.startswith("anomaly."):
            return
        if (
            self._config.trigger_event_types
            and event.event_type not in self._config.trigger_event_types
        ):
            return

        if self._active is not None:
            self._active.suppressed_triggers += 1
            if not self._active_skip_reported:
                self._publish_event(
                    "rosbag.recording_skipped",
                    severity="warning",
                    data={
                        "reason": "recording_active",
                        "active_output_dir": str(self._active.output_dir),
                        "trigger_event_type": event.event_type,
                    },
                )
                self._active_skip_reported = True
            return

        now = time.monotonic()
        if self._started_recordings >= self._config.max_recordings_per_run:
            if not self._max_recordings_reported:
                self._publish_event(
                    "rosbag.recording_skipped",
                    severity="warning",
                    data={
                        "reason": "max_recordings_reached",
                        "max_recordings_per_run": self._config.max_recordings_per_run,
                        "trigger_event_type": event.event_type,
                    },
                )
                self._max_recordings_reported = True
            return
        if now < self._cooldown_until:
            if not self._cooldown_skip_reported:
                self._publish_event(
                    "rosbag.recording_skipped",
                    severity="warning",
                    data={
                        "reason": "cooldown_active",
                        "cooldown_remaining_sec": round(
                            self._cooldown_until - now,
                            2,
                        ),
                        "trigger_event_type": event.event_type,
                    },
                )
                self._cooldown_skip_reported = True
            return

        ros2_path = self._resolve_ros2_binary()
        if ros2_path is None:
            self._publish_event(
                "rosbag.recorder_unavailable",
                severity="warning",
                data={
                    "reason": "ros2_cli_not_found",
                    "executable": self._config.executable,
                    "trigger_event_type": event.event_type,
                },
            )
            return

        output_dir = self._build_output_dir(event)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._cooldown_until = time.monotonic() + self._config.cooldown_sec
            self._cooldown_skip_reported = False
            self._publish_event(
                "rosbag.recording_failed",
                severity="error",
                data={
                    "reason": "output_dir_create_failed",
                    "trigger_event_type": event.event_type,
                    "error": str(exc),
                    "output_dir": str(output_dir),
                },
            )
            return
        cmd = self._build_command(ros2_path, output_dir)
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._cooldown_until = time.monotonic() + self._config.cooldown_sec
            self._cooldown_skip_reported = False
            self._publish_event(
                "rosbag.recording_failed",
                severity="error",
                data={
                    "reason": "spawn_failed",
                    "trigger_event_type": event.event_type,
                    "error": str(exc),
                    "output_dir": str(output_dir),
                    "command": cmd,
                },
            )
            return

        now = time.monotonic()
        self._active = _RecordingState(
            process=process,
            started_monotonic=now,
            stop_deadline=now + self._config.record_duration_sec,
            output_dir=output_dir,
            command=cmd,
            trigger_event_type=event.event_type,
            trigger_metric=event.data.get("metric"),
        )
        self._active_skip_reported = False
        self._cooldown_skip_reported = False
        self._started_recordings += 1
        self._publish_event(
            "rosbag.recording_started",
            data={
                "trigger_event_type": event.event_type,
                "trigger_metric": event.data.get("metric"),
                "trigger_message": event.data.get("message"),
                "output_dir": str(output_dir),
                "pid": process.pid,
                "record_duration_sec": self._config.record_duration_sec,
                "storage_id": self._config.storage_id,
                "topics": list(self._config.topics),
                "command": cmd,
            },
        )

    def _poll_active_recording(self) -> None:
        state = self._active
        if state is None:
            return

        returncode = state.process.poll()
        if returncode is not None:
            self._finish_recording(reason="process_exited", returncode=returncode)
            return
        if time.monotonic() >= state.stop_deadline:
            self._stop_active_recording(reason="duration_elapsed")

    def _stop_active_recording(self, *, reason: str) -> None:
        state = self._active
        if state is None:
            return

        process = state.process
        returncode = process.poll()
        if returncode is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                returncode = process.wait(timeout=_STOP_WAIT_SEC)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait(timeout=2.0)
            except OSError as exc:
                logger.warning("Failed to stop rosbag2 process cleanly: %s", exc)
                process.kill()
                returncode = process.wait(timeout=2.0)

        self._finish_recording(reason=reason, returncode=returncode)

    def _finish_recording(self, *, reason: str, returncode: int | None) -> None:
        state = self._active
        if state is None:
            return

        elapsed = time.monotonic() - state.started_monotonic
        if reason in {"duration_elapsed", "daemon_stopping"}:
            severity = "info"
            event_type = "rosbag.recording_stopped"
        elif returncode == 0:
            severity = "info"
            event_type = "rosbag.recording_stopped"
        else:
            severity = "error"
            event_type = "rosbag.recording_failed"

        self._publish_event(
            event_type,
            severity=severity,
            data={
                "reason": reason,
                "returncode": returncode,
                "elapsed_sec": round(elapsed, 2),
                "output_dir": str(state.output_dir),
                "command": state.command,
                "trigger_event_type": state.trigger_event_type,
                "trigger_metric": state.trigger_metric,
                "suppressed_triggers": state.suppressed_triggers,
            },
        )
        self._cooldown_until = time.monotonic() + self._config.cooldown_sec
        self._cooldown_skip_reported = False
        self._active_skip_reported = False
        self._active = None

    def _resolve_ros2_binary(self) -> str | None:
        if self._ros2_path is not None:
            return self._ros2_path

        self._ros2_path = shutil.which(self._config.executable)
        if self._ros2_path is None:
            logger.warning(
                "%s not found on PATH; rosbag2 recorder will stay inactive",
                self._config.executable,
            )
        return self._ros2_path

    def _build_command(self, ros2_path: str, output_dir: Path) -> list[str]:
        cmd = [
            ros2_path,
            "bag",
            "record",
            "-o",
            str(output_dir),
            "-s",
            self._config.storage_id,
        ]
        if self._config.topics:
            cmd.extend(self._config.topics)
        else:
            cmd.append("-a")
        return cmd

    def _build_output_dir(self, event: BlackBoxEvent) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        metric = str(event.data.get("metric", "unknown")).replace("/", "_")
        label = event.event_type.replace(".", "_")
        name = f"{timestamp}_{label}_{metric}"
        return Path(os.path.expanduser(self._config.output_dir)) / name

    def _publish_event(
        self,
        event_type: str,
        *,
        data: dict[str, object],
        severity: str = "info",
    ) -> None:
        event = BlackBoxEvent.recorder_event(
            event_type=event_type,
            severity=severity,
            data=data,
            **self._session.metadata(),
        )
        self._event_bus.publish(event)
