"""Lifecycle adapter for the opt-in native ROS 2 capture process."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from fnmatch import fnmatch
from io import BufferedReader
from pathlib import Path
from typing import Callable

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
_RATE_MARKER = b"RATE_STATUS "
_STATUS_SCHEMA = "blackboxrs.capture_status.v1"
_RATE_STATUS_SCHEMA = "blackboxrs.capture_rate_status.v1"
_RATE_BATCH_TOPIC_LIMIT = 64
_RATE_BATCH_LIMIT = 64
_RATE_TOPIC_BYTES = 4096
_RATE_WINDOW_TOPIC_BYTES = 256 * 1024
_RATE_SESSION_ID_BYTES = 256
_RATE_UINT64_MAX = (1 << 64) - 1
_RATE_COVERAGE_COUNTER_FIELDS = (
    "graph_coverage_faults",
    "graph_snapshot_failures",
    "endpoint_query_failures",
    "subscription_failures",
    "ambiguous_topic_types",
)
_RATE_STATUS_PIPE_ENV = "BLACKBOXRS_RATE_STATUS_PIPE"
_DEFAULT_RATE_SUMMARY_PERIOD_MS = 1000
_RATE_HEARTBEAT_GRACE_WINDOWS = 3
_RATE_HEARTBEAT_MIN_TIMEOUT_SEC = 0.5
_ROS_MONITOR_INTERNAL_PREFIXES = ("/rosout", "/parameter_events", "/blackbox/")


class NativeCaptureProcess:
    """Start and stop ``blackbox_capture_cpp`` with daemon-owned parameters."""

    def __init__(
        self,
        config: CaptureConfig,
        runtime: RuntimeConfig,
        event_bus: EventBus | None = None,
        session: Session | None = None,
        publish_rate_events: bool = False,
        rate_topic_filters: list[str] | None = None,
        rate_summary_period_ms: int = _DEFAULT_RATE_SUMMARY_PERIOD_MS,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._event_bus = event_bus
        self._session = session
        self._publish_rate_events = publish_rate_events
        self._rate_topic_filters = tuple(rate_topic_filters or ())
        if rate_summary_period_ms <= 0:
            raise ValueError("rate_summary_period_ms must be positive")
        self._rate_summary_period_ms = rate_summary_period_ms
        self._process: subprocess.Popen[bytes] | None = None
        self._params_path: Path | None = None
        self._output_thread: threading.Thread | None = None
        self._rate_output_thread: threading.Thread | None = None
        self._rate_output_stream: BufferedReader | None = None
        self._rate_fifo_path: Path | None = None
        self._rate_fifo_directory: Path | None = None
        self._rate_fifo_keepalive_fd: int | None = None
        self._output_tail = bytearray()
        self._output_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._unexpected_exit_code: int | None = None
        self._latest_status: dict[str, object] | None = None
        self._final_status: dict[str, object] | None = None
        self._reported_health_states: set[str] = set()
        self._rate_status_lines = 0
        self._rate_events_published = 0
        self._rate_status_rejected = 0
        self._rate_coverage_faults = 0
        self._rate_heartbeats_received = 0
        self._rate_failovers = 0
        self._rate_fallback_callback_failures = 0
        self._rate_transport_started = False
        self._rate_bridge_active = False
        self._rate_bridge_failed = False
        self._rate_failover_reason: str | None = None
        self._rate_last_heartbeat: float | None = None
        self._rate_activation_event = threading.Event()
        self._rate_fallback_callback: Callable[[str], object] | None = None
        self._rate_fallback_invoked = False
        self._rate_window_key: tuple[str, int, int] | None = None
        self._rate_expected_batches = 0
        self._rate_seen_batches: set[int] = set()
        self._rate_pending_counts: dict[str, int] = {}
        self._rate_pending_topic_bytes = 0

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

    @property
    def rate_bridge_counters(self) -> dict[str, int]:
        """Return bounded native-rate bridge accounting for diagnostics."""
        with self._output_lock:
            return {
                "status_lines": self._rate_status_lines,
                "events_published": self._rate_events_published,
                "status_rejected": self._rate_status_rejected,
                "coverage_faults": self._rate_coverage_faults,
                "heartbeats_received": self._rate_heartbeats_received,
                "failovers": self._rate_failovers,
                "fallback_callback_failures": self._rate_fallback_callback_failures,
            }

    @property
    def rate_bridge_active(self) -> bool:
        """Return whether the dedicated native rate bridge is healthy."""
        with self._output_lock:
            return self._rate_bridge_active and not self._rate_bridge_failed

    def set_rate_bridge_fallback(self, callback: Callable[[str], object]) -> None:
        """Register the one-shot callback that enables Python rate tracking."""
        invoke = False
        with self._output_lock:
            self._rate_fallback_callback = callback
            if self._rate_bridge_failed and not self._rate_fallback_invoked:
                self._rate_fallback_invoked = True
                reason = self._rate_failover_reason or "native_rate_bridge_unavailable"
                invoke = True
        if invoke:
            self._invoke_rate_fallback(callback, reason)

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
            self._rate_status_lines = 0
            self._rate_events_published = 0
            self._rate_status_rejected = 0
            self._rate_coverage_faults = 0
            self._rate_heartbeats_received = 0
            self._rate_failovers = 0
            self._rate_fallback_callback_failures = 0
            self._rate_transport_started = False
            self._rate_bridge_active = False
            self._rate_bridge_failed = False
            self._rate_failover_reason = None
            self._rate_last_heartbeat = None
            self._rate_fallback_invoked = False
            self._reset_rate_window()
        self._rate_activation_event.clear()
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
                    "status.rate_summary_period_ms": (
                        self._rate_summary_period_ms if self._publish_rate_events else 0
                    ),
                }
            }
        }
        rate_read_fd: int | None = None
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
            environment = os.environ.copy()
            environment.pop("BLACKBOXRS_RATE_STATUS_FD", None)
            if self._publish_rate_events:
                self._rate_fifo_directory = Path(
                    tempfile.mkdtemp(prefix="native_rate_", dir=runtime_dir)
                )
                self._rate_fifo_path = self._rate_fifo_directory / "status.fifo"
                os.mkfifo(self._rate_fifo_path, mode=0o600)
                rate_read_fd = os.open(self._rate_fifo_path, os.O_RDONLY | os.O_NONBLOCK)
                self._rate_fifo_keepalive_fd = os.open(
                    self._rate_fifo_path, os.O_WRONLY | os.O_NONBLOCK
                )
                read_flags = fcntl.fcntl(rate_read_fd, fcntl.F_GETFL)
                fcntl.fcntl(rate_read_fd, fcntl.F_SETFL, read_flags & ~os.O_NONBLOCK)
                environment[_RATE_STATUS_PIPE_ENV] = str(self._rate_fifo_path)
            else:
                environment.pop(_RATE_STATUS_PIPE_ENV, None)
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
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
            if rate_read_fd is not None:
                self._rate_output_stream = os.fdopen(rate_read_fd, "rb", buffering=0)
                rate_read_fd = None
                with self._output_lock:
                    self._rate_transport_started = True
                self._rate_output_thread = threading.Thread(
                    target=self._drain_output,
                    args=(self._rate_output_stream, True),
                    name="blackboxrs-native-rate-output",
                    daemon=True,
                )
                self._rate_output_thread.start()
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
                    if self._publish_rate_events and not self._rate_activation_event.wait(
                        timeout=self._rate_heartbeat_timeout_sec
                    ):
                        self._trigger_rate_bridge_failover("ACTIVATION_TIMEOUT")
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
            self._watch_stop.set()
            for descriptor_to_close in (rate_read_fd,):
                if descriptor_to_close is not None:
                    try:
                        os.close(descriptor_to_close)
                    except OSError:
                        pass
            self._close_rate_fifo_keepalive()
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5.0)
                except (OSError, subprocess.SubprocessError):
                    logger.exception("Could not terminate failed native capture startup")
            self._join_output_thread()
            self._close_rate_output_stream()
            self._join_rate_output_thread()
            self._process = None
            self._cleanup_files()
            raise

    def stop(self) -> None:
        process = self._process
        self._watch_stop.set()
        if process is None:
            self._join_watch_thread()
            self._close_rate_output_stream()
            self._join_rate_output_thread()
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
            self._close_rate_output_stream()
            self._join_rate_output_thread()
            self._process = None
            self._cleanup_files()
        with self._output_lock:
            status = dict(self._final_status) if self._final_status is not None else None
        state = status.get("state") if status is not None else None
        rate_failures = status.get("rate_status_failures") if status is not None else None
        if (
            self._publish_rate_events
            and isinstance(rate_failures, int)
            and not isinstance(rate_failures, bool)
            and rate_failures > 0
        ):
            self._publish_health_event(
                "capture.native_rate_bridge_fault",
                {
                    "state": "RATE_STATUS_THREAD_FAILED",
                    "rate_status_failures": rate_failures,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                },
            )
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
                self._check_rate_bridge_timeout()
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

    @property
    def _rate_heartbeat_timeout_sec(self) -> float:
        return max(
            _RATE_HEARTBEAT_MIN_TIMEOUT_SEC,
            self._rate_summary_period_ms * _RATE_HEARTBEAT_GRACE_WINDOWS / 1000.0,
        )

    def _check_rate_bridge_timeout(self, now: float | None = None) -> bool:
        """Fail over once when an active bridge misses its heartbeat deadline."""
        current = time.monotonic() if now is None else now
        with self._output_lock:
            last_heartbeat = self._rate_last_heartbeat
            should_fail = (
                self._rate_transport_started
                and self._rate_bridge_active
                and not self._rate_bridge_failed
                and last_heartbeat is not None
                and current - last_heartbeat > self._rate_heartbeat_timeout_sec
            )
        if should_fail:
            self._trigger_rate_bridge_failover("HEARTBEAT_TIMEOUT")
        return should_fail

    def _publish_health_event(
        self,
        event_type: str,
        data: dict[str, object],
        *,
        severity: str = "error",
    ) -> None:
        if self._event_bus is None or self._session is None:
            return
        event = BlackBoxEvent.native_event(
            event_type=event_type,
            severity=severity,
            data=data,
            **self._session.metadata(),
        )
        self._event_bus.publish(event)

    def _record_rate_heartbeat(self) -> bool:
        """Record one complete summary window and activate the bridge once."""
        activated = False
        with self._output_lock:
            self._rate_heartbeats_received += 1
            if self._rate_bridge_failed:
                return False
            if self._rate_transport_started:
                self._rate_last_heartbeat = time.monotonic()
                activated = not self._rate_bridge_active
                self._rate_bridge_active = True
        if self._rate_transport_started:
            self._rate_activation_event.set()
            self._close_rate_fifo_keepalive()
        if activated:
            self._publish_health_event(
                "capture.native_rate_bridge_active",
                {
                    "state": "ACTIVE",
                    "transport": "dedicated_pipe",
                    "heartbeat_period_ms": self._rate_summary_period_ms,
                    "capture_backend": "cpp",
                    "frequency_source": "native_cpp",
                    "zero_arrival_heartbeat": True,
                    "coverage_complete": True,
                },
                severity="info",
            )
        return True

    def _trigger_rate_bridge_failover(self, reason: str) -> bool:
        """Transition the native bridge to permanent Python fallback once."""
        callback: Callable[[str], object] | None = None
        counters: dict[str, int]
        with self._output_lock:
            if not self._rate_transport_started or self._rate_bridge_failed:
                return False
            self._rate_bridge_failed = True
            self._rate_bridge_active = False
            self._rate_failover_reason = reason
            self._rate_failovers += 1
            if self._rate_fallback_callback is not None and not self._rate_fallback_invoked:
                self._rate_fallback_invoked = True
                callback = self._rate_fallback_callback
            counters = {
                "heartbeats_received": self._rate_heartbeats_received,
                "status_rejected": self._rate_status_rejected,
                "coverage_faults": self._rate_coverage_faults,
                "failovers": self._rate_failovers,
            }
        self._rate_activation_event.set()
        self._close_rate_fifo_keepalive()
        self._publish_health_event(
            "capture.native_rate_bridge_failover",
            {
                "state": "FALLBACK_REQUESTED",
                "reason": reason,
                "transport": "dedicated_pipe",
                "capture_backend": "cpp",
                "frequency_source": "python",
                "evidence_complete": False,
                **counters,
            },
        )
        if callback is not None:
            self._invoke_rate_fallback(callback, reason)
        return True

    def _invoke_rate_fallback(
        self,
        callback: Callable[[str], object],
        reason: str,
    ) -> None:
        try:
            callback(reason)
        except Exception as error:  # noqa: BLE001
            with self._output_lock:
                self._rate_fallback_callback_failures += 1
                failures = self._rate_fallback_callback_failures
            logger.exception("Could not enable Python frequency fallback")
            self._publish_health_event(
                "capture.native_rate_bridge_fallback_failed",
                {
                    "state": "FALLBACK_CALLBACK_FAILED",
                    "reason": reason,
                    "error_type": type(error).__name__,
                    "callback_failures": failures,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                },
            )

    def _join_watch_thread(self) -> None:
        thread = self._watch_thread
        if thread is None:
            return
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.warning("Native capture watchdog did not stop promptly")
        else:
            self._watch_thread = None

    def _drain_output(self, stream: BufferedReader, rate_channel: bool = False) -> None:
        """Drain stdout or the dedicated rate channel without blocking the child."""
        pending = bytearray()
        discarding_oversized_line = False
        read_chunk = getattr(stream, "read1", stream.read)
        try:
            while chunk := read_chunk(_OUTPUT_READ_BYTES):
                if not rate_channel:
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
                            if not discarding_oversized_line:
                                discarding_oversized_line = True
                                logger.warning("Native capture emitted an oversized output line")
                                if rate_channel:
                                    self._reject_rate_status("oversized rate-status line")
                        break
                    if discarding_oversized_line or newline > _OUTPUT_LINE_BYTES:
                        line = b""
                        if not discarding_oversized_line:
                            logger.warning("Native capture emitted an oversized output line")
                            if rate_channel:
                                self._reject_rate_status("oversized rate-status line")
                    else:
                        line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    discarding_oversized_line = False
                    if line:
                        self._inspect_output_line(line, trusted_rate_channel=rate_channel)
            if pending and not discarding_oversized_line:
                self._inspect_output_line(
                    bytes(pending), trusted_rate_channel=rate_channel
                )
        except (OSError, ValueError):
            logger.debug("Native capture output pipe closed during drain", exc_info=True)
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            if rate_channel and not self._watch_stop.is_set():
                self._trigger_rate_bridge_failover("RATE_PIPE_CLOSED")

    def _inspect_output_line(
        self,
        line: bytes,
        *,
        trusted_rate_channel: bool = False,
    ) -> None:
        marker = next(
            (candidate for candidate in (_HEALTH_MARKER, _FINAL_MARKER, _RATE_MARKER)
             if candidate in line),
            None,
        )
        if marker is None:
            return
        if marker == _RATE_MARKER and self._rate_transport_started and not trusted_rate_channel:
            with self._output_lock:
                self._rate_status_lines += 1
            self._reject_rate_status("rate status arrived outside dedicated transport")
            return
        position = line.find(marker)
        try:
            status = json.loads(line[position + len(marker) :].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("Native capture emitted malformed machine-readable status")
            if marker == _RATE_MARKER and self._publish_rate_events:
                with self._output_lock:
                    self._rate_status_lines += 1
                self._reject_rate_status("malformed rate-status JSON")
            return
        if marker == _RATE_MARKER:
            try:
                self._publish_rate_status(status)
            except (OverflowError, ValueError):
                self._reject_rate_status("out-of-range rate-status numeric field")
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

    def _publish_rate_status(self, status: object) -> None:
        """Adapt one bounded C++ rate summary into existing ROS events."""
        if not self._publish_rate_events:
            return
        with self._output_lock:
            self._rate_status_lines += 1
        if not isinstance(status, dict) or status.get("schema_version") != _RATE_STATUS_SCHEMA:
            self._reject_rate_status("unsupported schema")
            return
        window_start = status.get("window_start_monotonic_ns")
        window_end = status.get("window_end_monotonic_ns")
        native_session_id = status.get("session_id")
        batch_index = status.get("batch_index")
        batch_count = status.get("batch_count")
        topics = status.get("topics")
        topics_truncated = status.get("topics_truncated")
        coverage_complete = status.get("coverage_complete")
        topic_coverage_truncated = status.get("topic_coverage_truncated")
        coverage_counters = {
            field: status.get(field) for field in _RATE_COVERAGE_COUNTER_FIELDS
        }
        if (
            not isinstance(window_start, int)
            or isinstance(window_start, bool)
            or window_start < 0
            or window_start > _RATE_UINT64_MAX
            or not isinstance(window_end, int)
            or isinstance(window_end, bool)
            or window_end <= window_start
            or window_end > _RATE_UINT64_MAX
            or not isinstance(native_session_id, str)
            or not native_session_id
            or len(native_session_id.encode("utf-8", errors="replace"))
            > _RATE_SESSION_ID_BYTES
            or not isinstance(batch_index, int)
            or isinstance(batch_index, bool)
            or batch_index < 0
            or not isinstance(batch_count, int)
            or isinstance(batch_count, bool)
            or batch_count <= 0
            or batch_count > _RATE_BATCH_LIMIT
            or batch_index >= batch_count
            or not isinstance(topics, list)
            or len(topics) > _RATE_BATCH_TOPIC_LIMIT
            or not isinstance(topics_truncated, bool)
        ):
            self._reject_rate_status("invalid envelope")
            return
        coverage_fields_valid = (
            isinstance(coverage_complete, bool)
            and isinstance(topic_coverage_truncated, bool)
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= _RATE_UINT64_MAX
                for value in coverage_counters.values()
            )
        )
        if not coverage_fields_valid:
            self._reject_rate_coverage("invalid or missing runtime coverage fields")
            return
        coverage_details: dict[str, object] = {
            "coverage_complete": coverage_complete,
            "topic_coverage_truncated": topic_coverage_truncated,
            **coverage_counters,
        }
        derived_coverage_complete = (
            not topic_coverage_truncated
            and all(value == 0 for value in coverage_counters.values())
        )
        if coverage_complete != derived_coverage_complete:
            self._reject_rate_coverage(
                "inconsistent runtime coverage completeness",
                coverage_details,
            )
            return
        if topics_truncated:
            self._reject_rate_coverage(
                "rate-status batch topic coverage truncated",
                {**coverage_details, "topics_truncated": True},
            )
            return
        if not coverage_complete:
            self._reject_rate_coverage(
                "runtime topic coverage incomplete",
                coverage_details,
            )
            return

        normalized: list[tuple[str, int, int]] = []
        for item in topics:
            if not isinstance(item, dict):
                self._reject_rate_status("invalid topic entry")
                return
            topic = item.get("topic")
            message_count = item.get("message_count")
            frequency_hz = item.get("frequency_hz")
            interval_ms = item.get("interval_ms")
            try:
                topic_size = len(topic.encode("utf-8")) if isinstance(topic, str) else 0
            except UnicodeEncodeError:
                topic_size = _RATE_TOPIC_BYTES + 1
            if (
                not isinstance(topic, str)
                or not topic.startswith("/")
                or not topic
                or topic_size > _RATE_TOPIC_BYTES
                or not isinstance(message_count, int)
                or isinstance(message_count, bool)
                or message_count <= 0
                or message_count > _RATE_UINT64_MAX
                or not isinstance(frequency_hz, (int, float))
                or isinstance(frequency_hz, bool)
                or not math.isfinite(float(frequency_hz))
                or frequency_hz <= 0
                or not isinstance(interval_ms, (int, float))
                or isinstance(interval_ms, bool)
                or not math.isfinite(float(interval_ms))
                or interval_ms <= 0
            ):
                self._reject_rate_status("invalid topic entry")
                return
            if self._rate_topic_allowed(topic):
                normalized.append((topic, message_count, topic_size))

        self._accept_rate_batch(
            native_session_id=native_session_id,
            window_start=window_start,
            window_end=window_end,
            batch_index=batch_index,
            batch_count=batch_count,
            topics=normalized,
        )

    def _accept_rate_batch(
        self,
        *,
        native_session_id: str,
        window_start: int,
        window_end: int,
        batch_index: int,
        batch_count: int,
        topics: list[tuple[str, int, int]],
    ) -> None:
        key = (native_session_id, window_start, window_end)
        if self._rate_window_key is not None and self._rate_window_key != key:
            self._reject_rate_status("incomplete rate-status window")
        if self._rate_window_key is None:
            self._rate_window_key = key
            self._rate_expected_batches = batch_count
        if (
            self._rate_expected_batches != batch_count
            or batch_index in self._rate_seen_batches
        ):
            self._reject_rate_status("inconsistent or duplicate rate-status batch")
            return

        for topic, message_count, topic_size in topics:
            if topic not in self._rate_pending_counts:
                if (
                    len(self._rate_pending_counts) >= _RATE_BATCH_LIMIT * _RATE_BATCH_TOPIC_LIMIT
                    or self._rate_pending_topic_bytes + topic_size
                    > _RATE_WINDOW_TOPIC_BYTES
                ):
                    self._reject_rate_status("rate-status window exceeds bridge capacity")
                    return
                self._rate_pending_counts[topic] = 0
                self._rate_pending_topic_bytes += topic_size
            self._rate_pending_counts[topic] += message_count
        self._rate_seen_batches.add(batch_index)
        if len(self._rate_seen_batches) != self._rate_expected_batches:
            return

        if not self._record_rate_heartbeat():
            self._reset_rate_window()
            return

        elapsed_ns = window_end - window_start
        metadata = {
            **(self._session.metadata() if self._session is not None else {}),
            "capture_backend": "cpp",
            "frequency_source": "native_cpp",
            "native_capture_session_id": native_session_id,
            "window_start_monotonic_ns": window_start,
            "window_end_monotonic_ns": window_end,
            "rate_batch_count": batch_count,
            "rate_coverage_complete": True,
        }
        published = 0
        if self._event_bus is not None and self._session is not None:
            for topic, message_count in self._rate_pending_counts.items():
                frequency_hz = float(message_count) * 1.0e9 / float(elapsed_ns)
                interval_ms = float(elapsed_ns) / float(message_count) / 1.0e6
                event = BlackBoxEvent.ros_event(
                    event_type="ros.frequency",
                    severity="info",
                    data={
                        "topic": topic,
                        "frequency_hz": frequency_hz,
                        "interval_ms": interval_ms,
                    },
                    message_count=message_count,
                    **metadata,
                )
                self._event_bus.publish(event)
                published += 1
        with self._output_lock:
            self._rate_events_published += published
        self._reset_rate_window()

    def _rate_topic_allowed(self, topic: str) -> bool:
        if any(topic.startswith(prefix) for prefix in _ROS_MONITOR_INTERNAL_PREFIXES):
            return False
        if not self._rate_topic_filters:
            return True
        return any(fnmatch(topic, pattern) for pattern in self._rate_topic_filters)

    def _reset_rate_window(self) -> None:
        self._rate_window_key = None
        self._rate_expected_batches = 0
        self._rate_seen_batches.clear()
        self._rate_pending_counts.clear()
        self._rate_pending_topic_bytes = 0

    def _reject_rate_coverage(
        self,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        """Reject incomplete runtime coverage and request permanent fallback."""
        self._reset_rate_window()
        with self._output_lock:
            self._rate_status_rejected += 1
            self._rate_coverage_faults += 1
            already_reported = "RATE_COVERAGE_INCOMPLETE" in self._reported_health_states
            self._reported_health_states.add("RATE_COVERAGE_INCOMPLETE")
            rejected = self._rate_status_rejected
            coverage_faults = self._rate_coverage_faults
        logger.error("Native rate coverage is incomplete: %s", reason)
        if not already_reported:
            self._publish_health_event(
                "capture.native_rate_bridge_coverage_fault",
                {
                    "state": "RATE_COVERAGE_INCOMPLETE",
                    "reason": reason,
                    "rejected_statuses": rejected,
                    "coverage_faults": coverage_faults,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                    **(details or {}),
                },
            )
        if not self._watch_stop.is_set():
            self._trigger_rate_bridge_failover("RATE_COVERAGE_INCOMPLETE")

    def _reject_rate_status(self, reason: str) -> None:
        self._reset_rate_window()
        with self._output_lock:
            self._rate_status_rejected += 1
            already_reported = "RATE_STATUS_REJECTED" in self._reported_health_states
            self._reported_health_states.add("RATE_STATUS_REJECTED")
            rejected = self._rate_status_rejected
        logger.warning("Native capture emitted rejected rate status: %s", reason)
        if not already_reported:
            self._publish_health_event(
                "capture.native_rate_bridge_fault",
                {
                    "state": "RATE_STATUS_REJECTED",
                    "reason": reason,
                    "rejected_statuses": rejected,
                    "capture_backend": "cpp",
                    "evidence_complete": False,
                },
            )
        if not self._watch_stop.is_set():
            self._trigger_rate_bridge_failover("RATE_STATUS_REJECTED")

    def _join_output_thread(self) -> None:
        thread = self._output_thread
        if thread is None:
            return
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.warning("Native capture output drain thread did not stop promptly")
        else:
            self._output_thread = None

    def _close_rate_output_stream(self) -> None:
        stream = self._rate_output_stream
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _close_rate_fifo_keepalive(self) -> None:
        with self._output_lock:
            descriptor = self._rate_fifo_keepalive_fd
            if descriptor is None:
                return
            self._rate_fifo_keepalive_fd = None
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _join_rate_output_thread(self) -> None:
        thread = self._rate_output_thread
        if thread is None:
            self._rate_output_stream = None
            return
        thread.join(timeout=1.0)
        if thread.is_alive():
            logger.warning("Native capture rate-status drain thread did not stop promptly")
        else:
            self._rate_output_thread = None
            self._rate_output_stream = None
            if self._rate_window_key is not None:
                self._reject_rate_status("rate output ended during rate-status window")

    def _diagnostic_suffix(self) -> str:
        tail = self.output_tail.strip()
        if not tail:
            return ""
        return f"\nNative capture output tail:\n{tail}"

    def _cleanup_files(self) -> None:
        self._close_rate_fifo_keepalive()
        if self._rate_fifo_path is not None:
            try:
                self._rate_fifo_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("Could not remove native rate-status FIFO", exc_info=True)
            self._rate_fifo_path = None
        if self._rate_fifo_directory is not None:
            try:
                self._rate_fifo_directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("Could not remove native rate-status directory", exc_info=True)
            self._rate_fifo_directory = None
        if self._params_path is not None:
            try:
                self._params_path.unlink()
            except FileNotFoundError:
                pass
            self._params_path = None
