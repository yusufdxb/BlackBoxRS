#!/usr/bin/env python3
"""Run a proof-bounded BlackBoxRS native capture benchmark.

The supervisor records only values it can observe. Missing recorder metrics are
written as JSON null and make the artifact invalid instead of being estimated
from publisher counts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCENARIOS: dict[str, dict[str, Any]] = {
    "A": {"topics": 1, "rate": 100.0, "payload_bytes": 64},
    "B": {"topics": 10, "rate": 1_000.0, "payload_bytes": 1_024},
    "C": {"topics": 10, "rate": 10_000.0, "payload_bytes": 256},
    "D": {"topics": 1, "rate": 30.0, "payload_bytes": 1_048_576},
    "E": {
        "topics": 10,
        "rate": 1_000.0,
        "payload_bytes": 1_024,
        "burst_every_sec": 5.0,
        "burst_duration_ms": 500.0,
        "burst_multiplier": 10.0,
    },
    "F": {
        "topics": 10,
        "rate": 1_000.0,
        "payload_bytes": 256,
        "churn_every_sec": 2.0,
        "churn_down_ms": 250.0,
    },
    "G": {
        "topics": 10,
        "rate": 1_000.0,
        "payload_bytes": 1_024,
        "slow_writer_ms": 5,
    },
}

STATUS_SCHEMA = "blackboxrs.capture_status.v1"
RESULT_SCHEMA = "blackboxrs.capture_benchmark.v1"
BENCHMARK_MARKER = b"BBRSBEN1"
LATENCY_SAMPLE_CAP = 100_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _git_sha(repo: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dirty(repo: Path) -> bool | None:
    try:
        output = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(output.strip())


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": _quantile(values, 0.50),
        "p95": _quantile(values, 0.95),
        "p99": _quantile(values, 0.99),
        "max": max(values) if values else None,
    }


def _proc_snapshot(pid: int) -> tuple[float, int, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        status_lines = Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
        io_lines = Path(f"/proc/{pid}/io").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rss_kib = 0
    for line in status_lines:
        if line.startswith("VmRSS:"):
            rss_kib = int(line.split()[1])
            break
    write_bytes = 0
    for line in io_lines:
        if line.startswith("write_bytes:"):
            write_bytes = int(line.split()[1])
            break
    ticks = int(fields[13]) + int(fields[14])
    return rss_kib / 1024.0, ticks, write_bytes


def _descendant_pids(root_pid: int) -> list[int]:
    descendants: list[int] = []
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        try:
            children = [
                int(value)
                for value in Path(f"/proc/{parent}/task/{parent}/children")
                .read_text(encoding="utf-8")
                .split()
            ]
        except (OSError, ValueError):
            continue
        descendants.extend(children)
        pending.extend(children)
    return descendants


def _capture_process_pid(root_pid: int) -> int:
    candidates = [root_pid, *_descendant_pids(root_pid)]
    for pid in reversed(candidates):
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"blackbox_capture_cpp/blackbox_capture" in command_line:
            return pid
    return root_pid


class ProcessSampler:
    def __init__(
        self,
        pid: int,
        output_root: Path,
        period_sec: float,
        status_collector: RosStatusCollector | None = None,
    ) -> None:
        self.pid = pid
        self.output_root = output_root
        self.period_sec = period_sec
        self.status_collector = status_collector
        self.samples: list[dict[str, float | int | None]] = []
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture-resource-sampler")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.period_sec * 2.0))

    def _run(self) -> None:
        previous_time: float | None = None
        previous_ticks: int | None = None
        clock_ticks = os.sysconf("SC_CLK_TCK")
        while not self._stop.is_set():
            now = time.monotonic()
            sampled_pid = _capture_process_pid(self.pid)
            snapshot = _proc_snapshot(sampled_pid)
            if snapshot is None:
                return
            rss_mb, ticks, write_bytes = snapshot
            cpu_percent: float | None = None
            if previous_time is not None and previous_ticks is not None and now > previous_time:
                cpu_percent = (
                    100.0 * ((ticks - previous_ticks) / clock_ticks) / (now - previous_time)
                )
            mcap_paths = list(self.output_root.glob("capture_*/segments/*.mcap"))
            segment_count = sum(1 for path in mcap_paths if not path.name.endswith(".partial.mcap"))
            partial_count = sum(1 for path in mcap_paths if path.name.endswith(".partial.mcap"))
            sample: dict[str, float | int | None] = {
                "t_sec": now - self._started,
                "rss_mb": rss_mb,
                "cpu_percent": cpu_percent,
                "process_write_bytes": write_bytes,
                "closed_segments": segment_count,
                "partial_segments": partial_count,
                "sampled_pid": sampled_pid,
            }
            status = self.status_collector.latest() if self.status_collector else None
            sample.update(
                {
                    "queue_depth": _first_number(status, ("queue_depth",), ("queue", "depth")),
                    "queue_capacity": _first_number(
                        status, ("queue_capacity",), ("queue", "capacity")
                    ),
                    "received": _first_number(status, ("received",), ("counters", "received")),
                    "admitted": _first_number(status, ("admitted",), ("counters", "admitted")),
                    "committed": _first_number(status, ("committed",), ("counters", "committed")),
                    "dropped": _first_number(status, ("dropped",), ("counters", "dropped")),
                    "storage_errors": _first_number(status, ("storage_errors",)),
                    "clock_anomalies": _first_number(status, ("clock_anomalies",)),
                    "rolling_segments": _first_number(status, ("rolling_segments",)),
                    "rolling_segment_bytes": _first_number(status, ("rolling_segment_bytes",)),
                    "retention_evicted_segments": _first_number(
                        status, ("retention_evicted_segments",)
                    ),
                    "retention_evicted_events": _first_number(
                        status, ("retention_evicted_events",)
                    ),
                    "retention_evicted_bytes": _first_number(status, ("retention_evicted_bytes",)),
                    "incident_manifest_errors": _first_number(
                        status, ("incident_manifest_errors",)
                    ),
                }
            )
            self.samples.append(sample)
            previous_time = now
            previous_ticks = ticks
            self._stop.wait(self.period_sec)


class RosStatusCollector:
    """Collect status without making rclpy a non-ROS script dependency."""

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self._latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._context: Any = None
        self._node: Any = None
        self._subscription: Any = None
        self._executor: Any = None

    def start(self) -> None:
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import QoSPresetProfiles
            from rclpy.signals import SignalHandlerOptions
            from std_msgs.msg import String

            self._rclpy = rclpy
            self._context = Context()
            rclpy.init(
                args=None,
                context=self._context,
                signal_handler_options=SignalHandlerOptions.NO,
            )
            self._node = Node("blackbox_capture_benchmark_status", context=self._context)
            self._executor = SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)

            def callback(message: Any) -> None:
                try:
                    value = json.loads(message.data)
                except (AttributeError, json.JSONDecodeError):
                    return
                if isinstance(value, dict) and value.get("schema_version") == STATUS_SCHEMA:
                    with self._lock:
                        self._latest = value

            self._subscription = self._node.create_subscription(
                String,
                "/blackbox/capture_status",
                callback,
                QoSPresetProfiles.SENSOR_DATA.value,
            )
            self.available = True
            self._thread = threading.Thread(target=self._spin, name="capture-status-collector")
            self._thread.start()
        except (ImportError, RuntimeError, ValueError) as error:
            self.error = str(error)
            self.stop()

    def _spin(self) -> None:
        while not self._stop.is_set() and self._context.ok():
            self._executor.spin_once(timeout_sec=0.1)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._node is not None:
            if self._executor is not None:
                self._executor.remove_node(self._node)
            self._node.destroy_node()
            self._node = None
            self._subscription = None
        if self._executor is not None:
            self._executor.shutdown()
            self._executor = None
        if self._context is not None and self._context.ok():
            self._context.shutdown()


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            results.append(value)
    return results


def _find_latest_status(capture_log: Path) -> dict[str, Any] | None:
    try:
        text = capture_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    statuses = [
        value
        for value in _extract_json_objects(text)
        if value.get("schema_version") == STATUS_SCHEMA
    ]
    return statuses[-1] if statuses else None


def _select_status(
    topic_status: dict[str, Any] | None, log_status: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    stopped_states = {"STOPPED_CLEAN", "STOPPED_INCOMPLETE", "INVARIANT_FAULT"}
    if log_status is not None and log_status.get("state") in stopped_states:
        return log_status, "capture_log"
    if topic_status is not None and topic_status.get("state") in stopped_states:
        return topic_status, "status_topic"
    if topic_status is not None:
        return topic_status, "status_topic_periodic"
    if log_status is not None:
        return log_status, "capture_log_periodic"
    return None, None


def _first_number(source: dict[str, Any] | None, *paths: tuple[str, ...]) -> int | float | None:
    if source is None:
        return None
    for path in paths:
        value: Any = source
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def _coalesce(*values: int | float | None) -> int | float | None:
    return next((value for value in values if value is not None), None)


def _machine() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "hostname_recorded": False,
        "gpu_recorded": False,
    }


def _write_generated_params(path: Path, args: argparse.Namespace, output_root: Path) -> None:
    topics = [f"{args.topic_prefix}{index}" for index in range(args.topics)]
    topic_lines = "\n".join(f'      - "{topic}"' for topic in topics)
    path.write_text(
        "/**:\n"
        "  ros__parameters:\n"
        "    capture.topics:\n"
        f"{topic_lines}\n"
        "    capture.discover_all: false\n"
        f'    storage.output_directory: "{output_root}"\n'
        f"    storage.failure_injection_delay_ms: {args.slow_writer_ms}\n"
        f"    storage.failure_injection_fail_after_bytes: {args.fail_after_bytes}\n"
        "    buffer.event_capacity: 4096\n"
        "    buffer.control_reserve: 64\n"
        "    buffer.payload_block_size: 4096\n"
        "    buffer.payload_block_count: 4096\n"
        "    storage.segment_max_bytes: 67108864\n"
        "    storage.segment_max_duration_sec: 5.0\n"
        "    status.publish_period_ms: 250\n",
        encoding="utf-8",
    )


def _command(base: str, replacements: dict[str, str], suffix: list[str]) -> list[str]:
    rendered = base
    used_placeholder = False
    for key, value in replacements.items():
        marker = "{" + key + "}"
        if marker in rendered:
            used_placeholder = True
            rendered = rendered.replace(marker, shlex.quote(value))
    command = shlex.split(rendered)
    return command if used_placeholder else command + suffix


def _wait_ready(
    process: subprocess.Popen[bytes],
    log_path: Path,
    pattern: str,
    timeout: float,
    started: float,
) -> float | None:
    deadline = started + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        try:
            if pattern in log_path.read_text(encoding="utf-8", errors="replace"):
                return (time.monotonic() - started) * 1000.0
        except OSError:
            pass
        time.sleep(0.05)
    return None


def _stop_process(
    process: subprocess.Popen[bytes], timeout: float
) -> tuple[int | None, float, bool]:
    started = time.monotonic()
    forced = False
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2.0)
    return process.returncode, (time.monotonic() - started) * 1000.0, forced


def _apply_scenario(args: argparse.Namespace) -> None:
    if args.scenario == "custom":
        return
    for key, value in SCENARIOS[args.scenario].items():
        setattr(args, key, value)


def _session_metadata(
    output_root: Path, session_id: str | None
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[Path],
    list[Path],
]:
    if session_id is None or re.fullmatch(r"[A-Za-z0-9_.-]+", session_id) is None:
        return None, None, [], [], []
    session_path = output_root / f"capture_{session_id}" / "session.json"
    if not session_path.is_file():
        return None, None, [], [], []
    quality = _read_json(session_path.parent / "capture_quality.json")
    sidecar_paths = sorted((session_path.parent / "segments").glob("*.json"))
    sidecars = [value for path in sidecar_paths if (value := _read_json(path)) is not None]
    segments = sorted(
        path
        for path in (session_path.parent / "segments").glob("*.mcap")
        if not path.name.endswith(".partial.mcap")
    )
    partials = sorted((session_path.parent / "segments").glob("*.partial.mcap"))
    return _read_json(session_path), quality, sidecars, segments, partials


def _inspect_committed_messages(
    segments: list[Path], topic_prefix: str
) -> tuple[int | None, int | None, list[float], int, str | None]:
    try:
        from mcap.exceptions import McapError
        from mcap.reader import make_reader
    except ImportError:
        return None, None, [], 0, "optional mcap package is not installed"
    count = 0
    byte_count = 0
    latency_population = 0
    latency_sample: list[float] = []
    random_source = random.Random(0)
    try:
        for segment in segments:
            with segment.open("rb") as stream:
                for _, channel, message in make_reader(stream).iter_messages():
                    if channel.topic.startswith(topic_prefix):
                        count += 1
                        byte_count += len(message.data)
                        marker_offset = message.data.find(BENCHMARK_MARKER)
                        timestamp_offset = marker_offset + 16
                        if marker_offset >= 0 and len(message.data) >= timestamp_offset + 8:
                            publisher_ns = int.from_bytes(
                                message.data[timestamp_offset : timestamp_offset + 8],
                                byteorder="little",
                                signed=False,
                            )
                            if publisher_ns <= message.log_time:
                                latency_us = (message.log_time - publisher_ns) / 1_000.0
                                latency_population += 1
                                if len(latency_sample) < LATENCY_SAMPLE_CAP:
                                    latency_sample.append(latency_us)
                                else:
                                    replacement = random_source.randrange(latency_population)
                                    if replacement < LATENCY_SAMPLE_CAP:
                                        latency_sample[replacement] = latency_us
    except (OSError, McapError, RuntimeError, ValueError) as error:
        return None, None, [], 0, f"MCAP committed-message inspection failed: {error}"
    return count, byte_count, latency_sample, latency_population, None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/native_capture_benchmark.json")
    )
    parser.add_argument("--scenario", choices=[*SCENARIOS, "custom"], default="A")
    parser.add_argument(
        "--capture-command", default="ros2 run blackbox_capture_cpp blackbox_capture"
    )
    parser.add_argument("--publisher-command", default="ros2 run blackbox_capture_bench publisher")
    parser.add_argument("--recorder-params", type=Path)
    parser.add_argument("--capture-output-dir", type=Path)
    parser.add_argument("--keep-work-directory", action="store_true")
    parser.add_argument("--include-samples", action="store_true")
    parser.add_argument("--ready-pattern", default="READY")
    parser.add_argument("--startup-timeout-sec", type=float, default=15.0)
    parser.add_argument("--shutdown-timeout-sec", type=float, default=10.0)
    parser.add_argument("--sample-period-sec", type=float, default=1.0)
    parser.add_argument("--topics", type=int, default=1)
    parser.add_argument("--rate", type=float, default=100.0, help="Aggregate messages per second")
    parser.add_argument("--payload-bytes", type=int, default=64)
    parser.add_argument("--duration-sec", type=float, default=10.0)
    parser.add_argument("--discovery-warmup-sec", type=float, default=1.0)
    parser.add_argument("--qos", choices=["best_effort", "reliable"], default="best_effort")
    parser.add_argument("--qos-depth", type=int, default=10)
    parser.add_argument("--topic-prefix", default="/blackbox_bench/topic")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--burst-every-sec", type=float, default=0.0)
    parser.add_argument("--burst-duration-ms", type=float, default=0.0)
    parser.add_argument("--burst-multiplier", type=float, default=1.0)
    parser.add_argument("--churn-every-sec", type=float, default=0.0)
    parser.add_argument("--churn-down-ms", type=float, default=0.0)
    parser.add_argument("--slow-writer-ms", type=int, default=0)
    parser.add_argument("--fail-after-bytes", type=int, default=-1)
    parser.add_argument("--expect-storage-fault", action="store_true")
    parser.add_argument(
        "--cross-host",
        action="store_true",
        help="Disable steady-clock ingest latency because publisher and recorder use different hosts",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    _apply_scenario(args)
    if args.topics <= 0 or args.rate <= 0 or args.payload_bytes <= 0 or args.duration_sec <= 0:
        raise ValueError("topics, rate, payload bytes, and duration must be positive")
    if (
        args.sample_period_sec <= 0
        or args.startup_timeout_sec <= 0
        or args.shutdown_timeout_sec <= 0
    ):
        raise ValueError("sampling and timeout values must be positive")
    if args.slow_writer_ms < 0 or args.fail_after_bytes < -1:
        raise ValueError(
            "failure injection delay must be non-negative and fail-after must be -1 or greater"
        )
    if args.recorder_params is not None and args.capture_output_dir is None:
        raise ValueError("--recorder-params requires the matching --capture-output-dir")
    if args.recorder_params is not None and (
        args.slow_writer_ms != 0 or args.fail_after_bytes != -1
    ):
        raise ValueError(
            "failure injection options cannot override an external recorder parameter file"
        )

    run_id = args.run_id or uuid.uuid4().hex[:12]
    repo = Path(__file__).resolve().parents[1]
    work_dir = Path(tempfile.mkdtemp(prefix="blackboxrs-native-bench-"))
    output_root = (
        args.capture_output_dir.resolve() if args.capture_output_dir else work_dir / "capture"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    params_path = (
        args.recorder_params.resolve() if args.recorder_params else work_dir / "recorder.yaml"
    )
    if args.recorder_params is None:
        _write_generated_params(params_path, args, output_root)

    publisher_result_path = work_dir / "publisher.json"
    capture_log_path = work_dir / "capture.log"
    publisher_log_path = work_dir / "publisher.log"
    capture_command = _command(
        args.capture_command,
        {"params": str(params_path), "output": str(output_root)},
        ["--ros-args", "--params-file", str(params_path)],
    )
    publisher_command = _command(
        args.publisher_command,
        {"result": str(publisher_result_path)},
        [
            "--topics",
            str(args.topics),
            "--aggregate-rate",
            str(args.rate),
            "--payload-bytes",
            str(args.payload_bytes),
            "--duration-sec",
            str(args.duration_sec),
            "--discovery-warmup-sec",
            str(args.discovery_warmup_sec),
            "--qos",
            args.qos,
            "--qos-depth",
            str(args.qos_depth),
            "--topic-prefix",
            args.topic_prefix,
            "--run-id",
            run_id,
            "--result-json",
            str(publisher_result_path),
            "--burst-every-sec",
            str(args.burst_every_sec),
            "--burst-duration-ms",
            str(args.burst_duration_ms),
            "--burst-multiplier",
            str(args.burst_multiplier),
            "--churn-every-sec",
            str(args.churn_every_sec),
            "--churn-down-ms",
            str(args.churn_down_ms),
        ],
    )

    errors: list[str] = []
    warnings: list[str] = []
    capture_exit_code: int | None = None
    publisher_exit_code: int | None = None
    startup_latency_ms: float | None = None
    shutdown_drain_ms: float | None = None
    forced_shutdown = False
    sampler: ProcessSampler | None = None
    status_collector = RosStatusCollector()

    with capture_log_path.open("wb") as capture_log:
        capture_started = time.monotonic()
        try:
            capture_process = subprocess.Popen(
                capture_command,
                cwd=repo,
                stdout=capture_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            errors.append(f"capture launch failed: {error}")
            capture_process = None

        if capture_process is not None:
            status_collector.start()
            startup_latency_ms = _wait_ready(
                capture_process,
                capture_log_path,
                args.ready_pattern,
                args.startup_timeout_sec,
                capture_started,
            )
            if startup_latency_ms is None:
                errors.append("capture did not emit the configured READY marker before timeout")

            if capture_process.poll() is None and startup_latency_ms is not None:
                sampler = ProcessSampler(
                    capture_process.pid, output_root, args.sample_period_sec, status_collector
                )
                sampler.start()
                with publisher_log_path.open("wb") as publisher_log:
                    try:
                        completed = subprocess.run(
                            publisher_command,
                            cwd=repo,
                            stdout=publisher_log,
                            stderr=subprocess.STDOUT,
                            timeout=args.duration_sec + args.discovery_warmup_sec + 30.0,
                            check=False,
                        )
                        publisher_exit_code = completed.returncode
                    except (OSError, subprocess.TimeoutExpired) as error:
                        errors.append(f"publisher execution failed: {error}")
            if sampler is not None:
                sampler.stop()
            capture_exit_code, shutdown_drain_ms, forced_shutdown = _stop_process(
                capture_process, args.shutdown_timeout_sec
            )
            status_collector.stop()

    publisher_result = _read_json(publisher_result_path)
    if publisher_result is None:
        errors.append("publisher did not produce a valid result JSON document")
    status, status_source = _select_status(
        status_collector.latest(), _find_latest_status(capture_log_path)
    )
    if not status_collector.available:
        warnings.append(
            "ROS status subscriber unavailable; final status was searched in the capture log: "
            + (status_collector.error or "unknown reason")
        )
    if args.recorder_params is not None:
        warnings.append(
            "external recorder parameters were not inspected for extra or missing topics; "
            "workload matching requires an independent configuration review"
        )
    if status is None:
        errors.append("capture log did not contain a blackboxrs.capture_status.v1 document")
    elif status.get("state") not in {"STOPPED_CLEAN", "STOPPED_INCOMPLETE", "INVARIANT_FAULT"}:
        errors.append("capture did not provide an authoritative terminal status")
    session_id = (
        status.get("session_id") if status and isinstance(status.get("session_id"), str) else None
    )
    session, capture_quality, sidecars, segments, partial_segments = _session_metadata(
        output_root, session_id
    )
    if session is None:
        errors.append("no native session.json was found")
    if capture_quality is None:
        warnings.append("no final capture_quality.json was found")
    elif capture_quality.get("schema_version") != "blackboxrs.capture_quality.v1":
        errors.append("final capture quality has an unsupported schema version")
    elif capture_quality.get("session_id") != session_id:
        errors.append("final capture quality session ID does not match final status")
    if not segments and not (args.expect_storage_fault and partial_segments):
        errors.append("no closed MCAP segment was found")
    if capture_exit_code not in (0, 130):
        errors.append(f"capture exited with code {capture_exit_code}")
    if publisher_exit_code != 0:
        errors.append(f"publisher exited with code {publisher_exit_code}")
    matched_topics = _first_number(publisher_result, ("matched_topics_before_measurement",))
    if matched_topics != args.topics:
        errors.append(
            "publisher did not match every configured recorder subscription before measurement"
        )
    if forced_shutdown:
        errors.append("capture exceeded the shutdown deadline and was terminated")

    samples = sampler.samples if sampler else []
    rss = [float(sample["rss_mb"]) for sample in samples]
    cpu = [
        float(sample["cpu_percent"]) for sample in samples if sample.get("cpu_percent") is not None
    ]
    retained_storage_bytes = sum(path.stat().st_size for path in segments)
    partial_storage_bytes = sum(path.stat().st_size for path in partial_segments)
    (
        serialized_retained,
        serialized_retained_bytes,
        ingest_latency_sample,
        ingest_latency_population,
        mcap_count_error,
    ) = _inspect_committed_messages(segments, args.topic_prefix)
    if mcap_count_error:
        warnings.append(mcap_count_error)
    final_sidecar = sidecars[-1] if sidecars else None
    sent = _first_number(publisher_result, ("sent",))
    received = _coalesce(
        _first_number(capture_quality, ("received",)),
        _first_number(final_sidecar, ("received",), ("counts", "received")),
        _first_number(status, ("received",), ("counters", "received")),
    )
    admitted = _coalesce(
        _first_number(capture_quality, ("admitted",)),
        _first_number(final_sidecar, ("admitted",), ("counts", "admitted")),
        _first_number(status, ("admitted",), ("counters", "admitted")),
    )
    committed = _coalesce(
        _first_number(capture_quality, ("committed",)),
        _first_number(final_sidecar, ("committed",), ("counts", "committed")),
        _first_number(status, ("committed",), ("counters", "committed")),
    )
    durable_observed = _coalesce(
        _first_number(capture_quality, ("durable",)),
        _first_number(status, ("durable",), ("counters", "durable")),
    )
    final_status_clean = (
        status is not None
        and status.get("state") == "STOPPED_CLEAN"
        and (capture_quality is None or capture_quality.get("clean") is True)
    )
    durable = durable_observed if final_status_clean else None
    if durable_observed is not None and not final_status_clean:
        warnings.append(
            "only a periodic durable lower bound was observed; no STOPPED_CLEAN status made the "
            "final durable count authoritative"
        )
    dropped = _coalesce(
        _first_number(capture_quality, ("dropped",)),
        _first_number(final_sidecar, ("dropped",), ("counts", "dropped")),
        _first_number(status, ("dropped",), ("counters", "dropped")),
    )
    dropped_bytes = _coalesce(
        _first_number(capture_quality, ("bytes_dropped",)),
        _first_number(final_sidecar, ("bytes_dropped",), ("bytes", "dropped")),
        _first_number(status, ("dropped_bytes",), ("counters", "dropped_bytes")),
    )
    drop_breakdown = status.get("drop_breakdown") if status else None
    serialized_dropped = None
    if isinstance(drop_breakdown, list):
        serialized_dropped = sum(
            int(entry.get("count", 0))
            for entry in drop_breakdown
            if isinstance(entry, dict)
            and isinstance(entry.get("topic"), str)
            and entry["topic"].startswith(args.topic_prefix)
            and isinstance(entry.get("count"), int)
        )

    retention_evicted_segments = _coalesce(
        _first_number(capture_quality, ("retention_evicted_segments",)),
        _first_number(status, ("retention_evicted_segments",)),
    )
    retention_evicted_bytes = _coalesce(
        _first_number(capture_quality, ("retention_evicted_bytes",)),
        _first_number(status, ("retention_evicted_bytes",)),
    )
    serialized_session_reconstructable = (
        retention_evicted_segments == 0 and not partial_segments
    )
    serialized_committed = (
        serialized_retained if serialized_session_reconstructable else None
    )
    serialized_committed_bytes = (
        serialized_retained_bytes if serialized_session_reconstructable else None
    )
    if isinstance(retention_evicted_segments, (int, float)) and retention_evicted_segments > 0:
        warnings.append(
            "rolling retention evicted finalized segments; payload-only session totals cannot be "
            "reconstructed from the retained MCAP files"
        )
    ingest_latency_valid = (
        not args.cross_host
        and retention_evicted_segments == 0
        and isinstance(serialized_retained, int)
        and serialized_retained > 0
        and ingest_latency_population == serialized_retained
    )
    if mcap_count_error:
        latency_reason = mcap_count_error
    elif args.cross_host:
        latency_reason = (
            "publisher and recorder steady clocks are not in a shared host clock domain"
        )
    elif retention_evicted_segments is None:
        latency_reason = "rolling-retention scope is unknown"
    elif retention_evicted_segments != 0:
        latency_reason = (
            "rolling eviction leaves only a retained subset of session ingest latencies"
        )
    elif ingest_latency_population != serialized_retained:
        latency_reason = "one or more retained benchmark payloads lacked a valid timestamp marker"
    else:
        latency_reason = "publisher steady timestamp to recorder callback steady timestamp"
    reported_ingest_latencies = ingest_latency_sample if ingest_latency_valid else []
    storage_bytes = (
        retained_storage_bytes + retention_evicted_bytes
        if isinstance(retention_evicted_bytes, int)
        else None
    )
    storage_errors = _coalesce(
        _first_number(capture_quality, ("storage_errors",)),
        _first_number(final_sidecar, ("storage_errors",)),
        _first_number(status, ("storage_errors",)),
    )

    if sent == 0:
        errors.append("publisher sent zero messages")
    if received is None or admitted is None or committed is None or dropped is None:
        errors.append("recorder status omitted one or more required reconciliation counters")
    if all(isinstance(value, (int, float)) for value in (received, committed, dropped)):
        if received != committed + dropped:
            errors.append("recorder received count does not reconcile with committed plus dropped")
    if (
        isinstance(admitted, (int, float))
        and isinstance(committed, (int, float))
        and committed > admitted
    ):
        errors.append("recorder reported committed greater than admitted")
    if (
        isinstance(durable_observed, (int, float))
        and isinstance(committed, (int, float))
        and durable_observed > committed
    ):
        errors.append("recorder reported durable greater than committed")
    quality_retained_bytes = _first_number(capture_quality, ("retained_bytes",))
    if (
        isinstance(quality_retained_bytes, (int, float))
        and quality_retained_bytes != retained_storage_bytes
    ):
        errors.append("final capture quality retained bytes do not match finalized segment files")
    if status is not None and status.get("state") == "STOPPED_CLEAN" and capture_quality is None:
        errors.append("clean terminal status has no final capture quality record")
    terminal_state = status.get("state") if status else None
    if args.expect_storage_fault:
        if not isinstance(storage_errors, (int, float)) or storage_errors <= 0:
            errors.append("expected storage fault did not increment storage_errors")
        if terminal_state == "STOPPED_CLEAN":
            errors.append("expected storage fault ended with a clean terminal state")
    elif terminal_state != "STOPPED_CLEAN":
        errors.append("ordinary benchmark did not end in STOPPED_CLEAN")
    workload_matched = args.recorder_params is None
    if (
        workload_matched
        and retention_evicted_segments == 0
        and all(
            isinstance(value, int) for value in (sent, serialized_committed, serialized_dropped)
        )
    ):
        if sent != serialized_committed + serialized_dropped:
            errors.append(
                "publisher sent count does not reconcile with serialized committed plus "
                "recorder-accounted serialized drops; DDS or pre-callback loss is unexplained"
            )
    else:
        warnings.append(
            "serialized publisher-to-recorder delivery could not be reconciled; one or more "
            "conditions applied: external workload, rolling retention, a partial segment, or "
            "unavailable drop details"
        )

    queue_capacity = _first_number(status, ("queue_capacity",), ("queue", "capacity"))
    queue_peak = _first_number(status, ("queue_peak",), ("queue", "peak_depth"))
    sidecar_peak_percent = _first_number(final_sidecar, ("peak_queue_utilization",))
    if queue_capacity and queue_peak is not None:
        queue_peak_utilization = queue_peak / queue_capacity
    elif sidecar_peak_percent is not None:
        queue_peak_utilization = sidecar_peak_percent / 100.0
    else:
        queue_peak_utilization = None

    artifact: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "generated_at": _utc_now(),
        "git_sha": _git_sha(repo),
        "git_dirty": _git_dirty(repo),
        "machine": _machine(),
        "build": {
            "ros_distro": os.environ.get("ROS_DISTRO"),
            "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION"),
            "compiler": os.environ.get("CXX"),
            "build_type": os.environ.get("CMAKE_BUILD_TYPE"),
        },
        "scenario": {
            "name": args.scenario,
            "run_id": run_id,
            "topics": args.topics,
            "aggregate_rate_hz": args.rate,
            "payload_bytes": args.payload_bytes,
            "duration_sec": args.duration_sec,
            "qos": args.qos,
            "qos_depth": args.qos_depth,
            "burst_every_sec": args.burst_every_sec,
            "burst_duration_ms": args.burst_duration_ms,
            "burst_multiplier": args.burst_multiplier,
            "churn_every_sec": args.churn_every_sec,
            "churn_down_ms": args.churn_down_ms,
            "writer_delay_injection_ms": args.slow_writer_ms,
            "fail_after_bytes": args.fail_after_bytes,
            "expect_storage_fault": args.expect_storage_fault,
            "shared_steady_clock_domain": not args.cross_host,
        },
        "capture_backend": "cpp",
        "validity": {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "publisher_and_capture_workload_matched": workload_matched,
            "publisher_to_callback_latency_valid": ingest_latency_valid,
            "latency_reason": latency_reason,
        },
        "counters": {
            "sent": sent,
            "received": received,
            "admitted": admitted,
            "committed": committed,
            "durable": durable,
            "durable_observed_lower_bound": durable_observed,
            "dropped": dropped,
            "serialized_dropped": serialized_dropped,
            "dropped_bytes": dropped_bytes,
            "serialized_committed": serialized_committed,
            "serialized_committed_bytes": serialized_committed_bytes,
            "serialized_retained": serialized_retained,
            "serialized_retained_bytes": serialized_retained_bytes,
        },
        "capture_quality": capture_quality,
        "drop_breakdown": drop_breakdown,
        "latency_us": {
            "ingest": {
                "p50": _quantile(reported_ingest_latencies, 0.50),
                "p95": _quantile(reported_ingest_latencies, 0.95),
                "p99": _quantile(reported_ingest_latencies, 0.99),
                "population": ingest_latency_population,
                "sample_size": len(reported_ingest_latencies),
                "sample_cap": LATENCY_SAMPLE_CAP,
                "valid": ingest_latency_valid,
            },
            "write": {"p50": None, "p95": None, "p99": None},
            "trigger_to_flush": {"p50": None, "p95": None, "p99": None},
        },
        "resources": {"rss_mb": _distribution(rss), "cpu_percent": _distribution(cpu)},
        "queue": {
            "depth": _first_number(status, ("queue_depth",), ("queue", "depth")),
            "capacity": queue_capacity,
            "peak_depth": queue_peak,
            "peak_utilization": queue_peak_utilization,
        },
        "storage": {
            "bytes": storage_bytes,
            "bytes_per_second": storage_bytes / args.duration_sec
            if storage_bytes is not None
            else None,
            "retained_bytes": retained_storage_bytes,
            "partial_bytes": partial_storage_bytes,
            "retention_evicted_bytes": retention_evicted_bytes,
            "retention_evicted_segments": retention_evicted_segments,
            "segments": len(segments),
            "partial_segments": len(partial_segments),
            "sidecars": len(sidecars),
            "errors": storage_errors,
        },
        "lifecycle": {
            "startup_latency_ms": startup_latency_ms,
            "shutdown_drain_ms": shutdown_drain_ms,
            "capture_exit_code": capture_exit_code,
            "publisher_exit_code": publisher_exit_code,
            "forced_shutdown": forced_shutdown,
            "final_status_state": status.get("state") if status else None,
            "status_source": status_source,
        },
        "recovery": session.get("recovery") if session else None,
        "provenance": {
            "capture_command": capture_command,
            "publisher_command": publisher_command,
            "generated_recorder_params": args.recorder_params is None,
            "work_directory_retained": args.keep_work_directory,
            "resource_process_selection": "native recorder descendant of ros2 run wrapper",
            "failure_injection": {
                "writer_delay_ms": args.slow_writer_ms,
                "fail_after_bytes": args.fail_after_bytes,
                "expect_storage_fault": args.expect_storage_fault,
            },
            "latency_clock_domain": "cross_host_unknown" if args.cross_host else "local_steady",
        },
    }
    if args.include_samples:
        artifact["resource_samples"] = samples
    _atomic_json(args.output.resolve(), artifact)
    if args.keep_work_directory:
        retained = args.output.resolve().parent / f"native_capture_work_{run_id}"
        if retained.exists():
            raise FileExistsError(f"retained work directory already exists: {retained}")
        work_dir.rename(retained)
        artifact["provenance"]["retained_work_directory"] = str(retained)
        _atomic_json(args.output.resolve(), artifact)
    else:
        shutil.rmtree(work_dir)
    return artifact


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        artifact = run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(args.output), "valid": artifact["validity"]["valid"]}))
    return 0 if artifact["validity"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
