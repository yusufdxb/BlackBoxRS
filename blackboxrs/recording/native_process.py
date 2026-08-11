"""Lifecycle adapter for the opt-in native ROS 2 capture process."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from io import BufferedReader
from pathlib import Path

import yaml

from blackboxrs.core.config import CaptureConfig, RuntimeConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session
from blackboxrs.recording.native import resolve_current_native_session


logger = logging.getLogger(__name__)

_OUTPUT_READ_BYTES = 4096
_OUTPUT_TAIL_BYTES = 64 * 1024
_OUTPUT_LINE_BYTES = 64 * 1024
_HEALTH_MARKER = b"HEALTH_STATUS "
_FINAL_MARKER = b"FINAL_STATUS "
_STATUS_SCHEMA = "blackboxrs.capture_status.v1"


class NativeCaptureProcess:
    """Start and stop ``blackbox_capture_cpp`` with daemon-owned parameters."""

    def __init__(
        self,
        config: CaptureConfig,
        runtime: RuntimeConfig,
        event_bus: EventBus | None = None,
        session: Session | None = None,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._event_bus = event_bus
        self._session = session
        self._process: subprocess.Popen[bytes] | None = None
        self._params_path: Path | None = None
        self._output_thread: threading.Thread | None = None
        self._output_tail = bytearray()
        self._output_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._unexpected_exit_code: int | None = None
        self._latest_status: dict[str, object] | None = None
        self._final_status: dict[str, object] | None = None
        self._reported_health_states: set[str] = set()

    @property
    def output_tail(self) -> str:
        """Return the bounded recent subprocess output for diagnostics."""
        with self._output_lock:
            output = bytes(self._output_tail)
        return output.decode("utf-8", errors="replace")

    @property
    def unexpected_exit_code(self) -> int | None:
        """Return the post-READY child exit code observed by the watchdog."""
        return self._unexpected_exit_code

    @property
    def latest_status(self) -> dict[str, object] | None:
        """Return the latest machine-readable native health or final status."""
        with self._output_lock:
            return dict(self._latest_status) if self._latest_status is not None else None

    def start(self) -> None:
        if self._process is not None:
            return
        with self._output_lock:
            self._output_tail.clear()
        self._watch_stop.clear()
        self._unexpected_exit_code = None
        with self._output_lock:
            self._latest_status = None
            self._final_status = None
            self._reported_health_states.clear()
        output = Path(self._config.native_output_dir).expanduser()
        runtime_dir = output / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix="native_capture_", suffix=".yaml", dir=runtime_dir
        )
        self._params_path = Path(raw_path)
        current_pointer = output / "current_session.json"
        try:
            current_pointer.unlink()
        except FileNotFoundError:
            pass
        parameters = {
            "/blackbox/blackbox_capture": {
                "ros__parameters": {
                    "runtime.role": self._runtime.role,
                    "runtime.observed_host": self._runtime.observed_host,
                    "capture.topics": list(self._config.topics),
                    "capture.discover_all": not self._config.topics,
                    "storage.output_directory": str(output),
                }
            }
        }
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(parameters, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            command = [
                *self._config.native_command,
                "--ros-args",
                "--params-file",
                str(self._params_path),
            ]
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if self._process.stdout is None:
                raise RuntimeError("native capture stdout pipe was not created")
            self._output_thread = threading.Thread(
                target=self._drain_output,
                args=(self._process.stdout,),
                name="blackboxrs-native-output",
                daemon=True,
            )
            self._output_thread.start()
            deadline = time.monotonic() + self._config.native_startup_timeout_sec
            while time.monotonic() < deadline:
                return_code = self._process.poll()
                if return_code is not None:
                    self._join_output_thread()
                    raise RuntimeError(
                        f"native capture exited before READY with code {return_code}"
                        f"{self._diagnostic_suffix()}"
                    )
                if resolve_current_native_session(output) is not None:
                    self._watch_thread = threading.Thread(
                        target=self._watch_process,
                        args=(self._process,),
                        name="blackboxrs-native-watchdog",
                        daemon=True,
                    )
                    self._watch_thread.start()
                    return
                time.sleep(0.05)
            os.killpg(self._process.pid, signal.SIGKILL)
            self._process.wait(timeout=5.0)
            self._join_output_thread()
            raise RuntimeError(
                "native capture did not publish READY before startup timeout"
                f"{self._diagnostic_suffix()}"
            )
        except Exception:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5.0)
                except (OSError, subprocess.SubprocessError):
                    logger.exception("Could not terminate failed native capture startup")
            self._join_output_thread()
            self._process = None
            self._cleanup_files()
            raise

    def stop(self) -> None:
        process = self._process
        self._watch_stop.set()
        if process is None:
            self._join_watch_thread()
            self._cleanup_files()
            return
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=self._config.native_shutdown_timeout_sec)
                except subprocess.TimeoutExpired:
                    logger.error("Native capture exceeded shutdown timeout; forcing termination")
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5.0)
            elif process.returncode not in (0, 130):
                logger.error("Native capture exited early with code %s", process.returncode)
        finally:
            self._join_watch_thread()
            self._join_output_thread()
            self._process = None
            self._cleanup_files()
        with self._output_lock:
            status = dict(self._final_status) if self._final_status is not None else None
        state = status.get("state") if status is not None else None
        if process.returncode not in (0, 130) or state != "STOPPED_CLEAN":
            self._publish_health_event(
                "capture.native_shutdown_incomplete",
                {
                    "state": state or "FINAL_STATUS_UNAVAILABLE",
                    "exit_code": process.returncode,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                },
            )

    def _watch_process(self, process: subprocess.Popen[bytes]) -> None:
        """Report a native child that exits after READY but before shutdown."""
        while not self._watch_stop.wait(0.1):
            return_code = process.poll()
            if return_code is None:
                continue
            self._unexpected_exit_code = return_code
            logger.error("Native capture exited unexpectedly with code %s", return_code)
            self._publish_health_event(
                "capture.native_process_exit",
                {
                    "state": "PROCESS_EXITED",
                    "exit_code": return_code,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                },
            )
            return

    def _publish_health_event(self, event_type: str, data: dict[str, object]) -> None:
        if self._event_bus is None or self._session is None:
            return
        event = BlackBoxEvent.native_event(
            event_type=event_type,
            severity="error",
            data=data,
            **self._session.metadata(),
        )
        self._event_bus.publish(event)

    def _join_watch_thread(self) -> None:
        thread = self._watch_thread
        if thread is None:
            return
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.warning("Native capture watchdog did not stop promptly")
        else:
            self._watch_thread = None

    def _drain_output(self, stream: BufferedReader) -> None:
        """Drain child output continuously while retaining a fixed-size tail."""
        pending = bytearray()
        discarding_oversized_line = False
        try:
            while chunk := stream.read(_OUTPUT_READ_BYTES):
                with self._output_lock:
                    self._output_tail.extend(chunk)
                    overflow = len(self._output_tail) - _OUTPUT_TAIL_BYTES
                    if overflow > 0:
                        del self._output_tail[:overflow]
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        if len(pending) > _OUTPUT_LINE_BYTES:
                            pending.clear()
                            discarding_oversized_line = True
                            logger.warning("Native capture emitted an oversized output line")
                        break
                    if discarding_oversized_line or newline > _OUTPUT_LINE_BYTES:
                        line = b""
                        if not discarding_oversized_line:
                            logger.warning("Native capture emitted an oversized output line")
                    else:
                        line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    discarding_oversized_line = False
                    if line:
                        self._inspect_output_line(line)
            if pending and not discarding_oversized_line:
                self._inspect_output_line(bytes(pending))
        except (OSError, ValueError):
            logger.debug("Native capture output pipe closed during drain", exc_info=True)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _inspect_output_line(self, line: bytes) -> None:
        marker = _HEALTH_MARKER if _HEALTH_MARKER in line else _FINAL_MARKER
        position = line.find(marker)
        if position < 0:
            return
        try:
            status = json.loads(line[position + len(marker) :].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Native capture emitted malformed machine-readable status")
            return
        if not isinstance(status, dict) or status.get("schema_version") != _STATUS_SCHEMA:
            logger.warning("Native capture emitted an unsupported machine-readable status")
            return
        state = status.get("state")
        with self._output_lock:
            self._latest_status = dict(status)
            if marker == _FINAL_MARKER:
                self._final_status = dict(status)
            already_reported = isinstance(state, str) and state in self._reported_health_states
            if isinstance(state, str) and state in {"STORAGE_FAULT", "INVARIANT_FAULT"}:
                self._reported_health_states.add(state)
        if state in {"STORAGE_FAULT", "INVARIANT_FAULT"} and not already_reported:
            self._publish_health_event(
                "capture.native_health_fault",
                {
                    "state": state,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                    "storage_errors": status.get("storage_errors"),
                    "dropped": status.get("dropped"),
                    "dropped_bytes": status.get("dropped_bytes"),
                },
            )

    def _join_output_thread(self) -> None:
        thread = self._output_thread
        if thread is None:
            return
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.warning("Native capture output drain thread did not stop promptly")
        else:
            self._output_thread = None

    def _diagnostic_suffix(self) -> str:
        tail = self.output_tail.strip()
        if not tail:
            return ""
        return f"\nNative capture output tail:\n{tail}"

    def _cleanup_files(self) -> None:
        if self._params_path is not None:
            try:
                self._params_path.unlink()
            except FileNotFoundError:
                pass
            self._params_path = None
