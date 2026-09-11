#!/usr/bin/env python3
"""Run a proof-bounded BlackBoxRS capture benchmark.

The supervisor records only values it can observe. Unsupported backend metrics
are written as JSON null and explicitly qualified instead of being estimated
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

#: Backend selector to the value written into ``capture_backend``.
#: Python capture is deliberately absent: it records semantic telemetry rather
#: than the full serialized workload and is not a throughput-equivalent backend.
BACKEND_LABELS = {"native": "cpp", "rosbag2": "rosbag2"}

# Native recorder settings the other backends are matched against. These mirror
# the values written by ``_write_generated_params`` and the native recorder
# defaults, and every one of them is restated in the artifact comparison block
# so a reviewer never has to trust this constant table on its own.
NATIVE_SUBSCRIPTION_DEPTH = 1000
NATIVE_CHUNK_SIZE_BYTES = 1_048_576
NATIVE_SEGMENT_MAX_BYTES = 67_108_864
NATIVE_SEGMENT_MAX_DURATION_SEC = 5.0
NATIVE_PAYLOAD_ARENA_BYTES = 4096 * 4096
NATIVE_DISCOVERY_PERIOD_MS = 100
ROSBAG2_DISCOVERY_POLL_MS = 100
ROSBAG2_MAX_CACHE_BYTES = NATIVE_PAYLOAD_ARENA_BYTES

#: Largest start-to-end drift of the CLOCK_REALTIME minus CLOCK_MONOTONIC offset
#: that still allows a rosbag2 ingest-latency percentile to be published. The
#: offset is sampled during the run and interpolated, so this is a guard against
#: a clock step rather than the correction itself.
CLOCK_OFFSET_DRIFT_LIMIT_NS = 2_000_000


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


def _rosbag2_process_pid(root_pid: int) -> int:
    """Return the process that hosts the rosbag2 recorder node.

    ``ros2 bag record`` loads the C++ recorder into the CLI process itself on
    Humble, so the root process is normally the correct accounting boundary.
    A deeper descendant is preferred only if one actually exists.
    """
    for pid in reversed(_descendant_pids(root_pid)):
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"bag" in command_line and b"record" in command_line:
            return pid
    return root_pid


class ClockOffsetSampler:
    """Sample CLOCK_REALTIME minus CLOCK_MONOTONIC while a run is in flight.

    rosbag2 stamps every message with a realtime receive timestamp while the
    benchmark publisher embeds a steady timestamp, so the two live in different
    epochs. The offset between them is measured here rather than assumed, and
    the recorded drift lets the artifact refuse a latency percentile when the
    system clock stepped during the run.
    """

    def __init__(self, period_sec: float = 0.25) -> None:
        self.period_sec = period_sec
        self.samples: list[tuple[int, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture-clock-offset-sampler")

    @staticmethod
    def _pair() -> tuple[int, int]:
        realtime = time.clock_gettime_ns(time.CLOCK_REALTIME)
        monotonic = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        return realtime, realtime - monotonic

    def start(self) -> None:
        self.samples.append(self._pair())
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.period_sec * 4.0))
        self.samples.append(self._pair())

    def _run(self) -> None:
        while not self._stop.wait(self.period_sec):
            self.samples.append(self._pair())

    @property
    def drift_ns(self) -> int | None:
        if len(self.samples) < 2:
            return None
        offsets = [offset for _, offset in self.samples]
        return max(offsets) - min(offsets)

    def offset_at(self, realtime_ns: int) -> int | None:
        """Piecewise-linear offset at *realtime_ns*, clamped to the sampled span."""
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        if len(ordered) == 1 or realtime_ns <= ordered[0][0]:
            return ordered[0][1]
        if realtime_ns >= ordered[-1][0]:
            return ordered[-1][1]
        for index in range(1, len(ordered)):
            left_time, left_offset = ordered[index - 1]
            right_time, right_offset = ordered[index]
            if realtime_ns <= right_time:
                if right_time == left_time:
                    return left_offset
                span = (realtime_ns - left_time) / (right_time - left_time)
                return int(round(left_offset + span * (right_offset - left_offset)))
        return ordered[-1][1]

    def summary(self) -> dict[str, Any]:
        offsets = [offset for _, offset in self.samples]
        return {
            "sample_count": len(self.samples),
            "min_offset_ns": min(offsets) if offsets else None,
            "max_offset_ns": max(offsets) if offsets else None,
            "drift_ns": self.drift_ns,
            "drift_limit_ns": CLOCK_OFFSET_DRIFT_LIMIT_NS,
        }


class ProcessSampler:
    def __init__(
        self,
        pid: int,
        output_root: Path,
        period_sec: float,
        status_collector: RosStatusCollector | None = None,
        pid_selector: Any = _capture_process_pid,
        closed_segment_glob: str = "capture_*/segments/*.mcap",
        partial_suffix: str = ".partial.mcap",
    ) -> None:
        self.pid = pid
        self.output_root = output_root
        self.period_sec = period_sec
        self.status_collector = status_collector
        self.pid_selector = pid_selector
        self.closed_segment_glob = closed_segment_glob
        self.partial_suffix = partial_suffix
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
            sampled_pid = self.pid_selector(self.pid)
            snapshot = _proc_snapshot(sampled_pid)
            if snapshot is None:
                return
            rss_mb, ticks, write_bytes = snapshot
            cpu_percent: float | None = None
            if previous_time is not None and previous_ticks is not None and now > previous_time:
                cpu_percent = (
                    100.0 * ((ticks - previous_ticks) / clock_ticks) / (now - previous_time)
                )
            mcap_paths = list(self.output_root.glob(self.closed_segment_glob))
            segment_count = sum(
                1 for path in mcap_paths if not path.name.endswith(self.partial_suffix)
            )
            partial_count = sum(1 for path in mcap_paths if path.name.endswith(self.partial_suffix))
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


def _capture_exit_is_acceptable(exit_code: int | None, expect_storage_fault: bool) -> bool:
    allowed = {0, 130}
    if expect_storage_fault:
        allowed.add(2)
    return exit_code in allowed


def _requires_publisher_delivery_reconciliation(
    *,
    expect_storage_fault: bool,
    workload_matched: bool,
    retention_evicted_segments: int | float | None,
    sent: int | float | None,
    serialized_committed: int | float | None,
    serialized_dropped: int | float | None,
) -> bool:
    return (
        not expect_storage_fault
        and workload_matched
        and retention_evicted_segments == 0
        and all(
            isinstance(value, int)
            for value in (sent, serialized_committed, serialized_dropped)
        )
    )


def _serialized_session_totals(
    *,
    backend: str,
    serialized_retained: int | None,
    serialized_retained_bytes: int | None,
    retention_evicted_segments: int | float | None,
    partial_segment_count: int,
    clean_process_close: bool,
) -> tuple[int | None, int | None, bool]:
    """Return reconstructable session totals and whether their scope is complete."""
    scope_complete = (
        retention_evicted_segments == 0
        and partial_segment_count == 0
        and (backend == "native" or clean_process_close)
    )
    if not scope_complete:
        return None, None, False
    return serialized_retained, serialized_retained_bytes, True


def _machine() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "hostname_recorded": False,
        "gpu_recorded": False,
    }


def _dimension(status: str, native: Any, backend: Any, note: str | None = None) -> dict[str, Any]:
    """One matched-semantics dimension.

    ``status`` is one of ``reference`` (this run is the native baseline),
    ``matched``, ``approximated``, ``unmatched``, or ``not_applicable``. An
    unmatched dimension is recorded as unmatched; it is never silently reported
    as if it had been controlled.
    """
    return {"status": status, "native": native, "backend": backend, "note": note}


def _shared_workload_dimensions(args: argparse.Namespace, status: str) -> dict[str, Any]:
    """Dimensions the supervisor controls identically for every backend."""
    return {
        "topics": _dimension(status, args.topics, args.topics),
        "message_type": _dimension(
            status, "std_msgs/msg/ByteMultiArray", "std_msgs/msg/ByteMultiArray"
        ),
        "payload_bytes": _dimension(status, args.payload_bytes, args.payload_bytes),
        "aggregate_rate_hz": _dimension(status, args.rate, args.rate),
        "duration_sec": _dimension(status, args.duration_sec, args.duration_sec),
        "discovery_warmup_sec": _dimension(
            status, args.discovery_warmup_sec, args.discovery_warmup_sec
        ),
        "publisher": _dimension(
            status,
            "blackbox_capture_bench publisher",
            "blackbox_capture_bench publisher",
            "the same publisher binary is launched with the same arguments for every backend",
        ),
        "publisher_qos": _dimension(
            status, f"{args.qos}/depth {args.qos_depth}", f"{args.qos}/depth {args.qos_depth}"
        ),
        "shutdown_signal": _dimension(
            status, "SIGINT to the process group", "SIGINT to the process group"
        ),
    }


def _native_comparison(args: argparse.Namespace) -> dict[str, Any]:
    dimensions = _shared_workload_dimensions(args, "reference")
    dimensions.update(
        {
            "subscription_reliability": _dimension("reference", args.qos, args.qos),
            "subscription_durability": _dimension("reference", "volatile", "volatile"),
            "subscription_history_depth": _dimension(
                "reference", NATIVE_SUBSCRIPTION_DEPTH, NATIVE_SUBSCRIPTION_DEPTH
            ),
            "message_deserialization": _dimension(
                "reference", "none (generic serialized)", "none (generic serialized)"
            ),
            "storage_plugin": _dimension("reference", "mcap", "mcap"),
            "storage_chunking": _dimension(
                "reference", "enabled, chunk CRC on", "enabled, chunk CRC on"
            ),
            "storage_chunk_size_bytes": _dimension(
                "reference", NATIVE_CHUNK_SIZE_BYTES, NATIVE_CHUNK_SIZE_BYTES
            ),
            "compression": _dimension("reference", "none", "none"),
            "segment_split_bytes": _dimension(
                "reference", NATIVE_SEGMENT_MAX_BYTES, NATIVE_SEGMENT_MAX_BYTES
            ),
            "segment_split_duration_sec": _dimension(
                "reference", NATIVE_SEGMENT_MAX_DURATION_SEC, NATIVE_SEGMENT_MAX_DURATION_SEC
            ),
            "write_buffering": _dimension(
                "reference",
                f"4096-event ring plus a {NATIVE_PAYLOAD_ARENA_BYTES} byte payload arena drained by a writer thread",
                f"4096-event ring plus a {NATIVE_PAYLOAD_ARENA_BYTES} byte payload arena drained by a writer thread",
            ),
            "durability_sync_policy": _dimension(
                "reference",
                "fsync on segment close and on the segment directory",
                "fsync on segment close and on the segment directory",
            ),
            "topic_discovery_period_ms": _dimension(
                "reference", NATIVE_DISCOVERY_PERIOD_MS, NATIVE_DISCOVERY_PERIOD_MS
            ),
            "process_accounting_boundary": _dimension(
                "reference", "native C++ recorder process", "native C++ recorder process"
            ),
            "ingest_timestamp_clock": _dimension(
                "reference",
                "CLOCK_MONOTONIC callback timestamp",
                "CLOCK_MONOTONIC callback timestamp",
            ),
        }
    )
    return _comparison_block(
        backend="native",
        scope="full_payload_recording",
        content_equivalent=True,
        content_note=(
            "This is the native reference run. Every retained message is the full serialized "
            "payload of a benchmark topic."
        ),
        dimensions=dimensions,
    )


def _rosbag2_comparison(args: argparse.Namespace) -> dict[str, Any]:
    dimensions = _shared_workload_dimensions(args, "matched")
    dimensions.update(
        {
            "subscription_reliability": _dimension("matched", args.qos, args.qos),
            "subscription_durability": _dimension("matched", "volatile", "volatile"),
            "subscription_history_depth": _dimension(
                "matched", NATIVE_SUBSCRIPTION_DEPTH, NATIVE_SUBSCRIPTION_DEPTH
            ),
            "message_deserialization": _dimension(
                "matched", "none (generic serialized)", "none (generic serialized)"
            ),
            "storage_plugin": _dimension("matched", "mcap", "mcap"),
            "storage_chunking": _dimension(
                "matched", "enabled, chunk CRC on", "enabled, chunk CRC on"
            ),
            "storage_chunk_size_bytes": _dimension(
                "matched", NATIVE_CHUNK_SIZE_BYTES, NATIVE_CHUNK_SIZE_BYTES
            ),
            "compression": _dimension("matched", "none", "none"),
            "segment_split_bytes": _dimension(
                "matched", NATIVE_SEGMENT_MAX_BYTES, NATIVE_SEGMENT_MAX_BYTES
            ),
            "segment_split_duration_sec": _dimension(
                "matched", NATIVE_SEGMENT_MAX_DURATION_SEC, NATIVE_SEGMENT_MAX_DURATION_SEC
            ),
            "write_buffering": _dimension(
                "approximated",
                f"4096-event ring plus {NATIVE_PAYLOAD_ARENA_BYTES} payload-arena bytes",
                f"{ROSBAG2_MAX_CACHE_BYTES} cache bytes with rosbag2 double buffering",
                "the byte cache and native event-plus-payload bounds are not equivalent",
            ),
            "durability_sync_policy": _dimension(
                "unmatched",
                "periodic flush plus fsync on segment close and directory update",
                "rosbag2 MCAP close/finalization; no equivalent fsync contract was observed",
            ),
            "topic_discovery_period_ms": _dimension(
                "matched", NATIVE_DISCOVERY_PERIOD_MS, ROSBAG2_DISCOVERY_POLL_MS
            ),
            "control_chronology": _dimension(
                "unmatched",
                "graph, trigger, clock, status, and drop events share the chronology",
                "workload topics only",
            ),
            "loss_accounting": _dimension(
                "unmatched",
                "callback-level received and reasoned drop counters",
                "no authoritative callback-received or drop ledger in the stock recorder",
            ),
            "process_accounting_boundary": _dimension(
                "matched",
                "native recorder process",
                "rosbag2 recorder process selected below the ros2 CLI wrapper when present",
            ),
            "ingest_timestamp_clock": _dimension(
                "approximated",
                "CLOCK_MONOTONIC callback timestamp",
                "realtime receive timestamp corrected with sampled realtime-minus-monotonic offset",
                "latency is suppressed if clock-offset drift exceeds the configured limit",
            ),
        }
    )
    return _comparison_block(
        backend="rosbag2",
        scope="serialized_workload_topics_only",
        content_equivalent=True,
        content_note=(
            "The configured benchmark topics carry the same full serialized payloads. Native "
            "control chronology is excluded from workload counts and storage equivalence."
        ),
        dimensions=dimensions,
        counter_reconciled=False,
        reconciliation_note=(
            "A clean rosbag2 file provides a retained-record count, but stock rosbag2 does not "
            "expose callback-received or reasoned-drop counters. Missing records remain unexplained."
        ),
    )


def _comparison_block(
    *,
    backend: str,
    scope: str,
    content_equivalent: bool,
    content_note: str,
    dimensions: dict[str, Any],
    counter_reconciled: bool | None = None,
    reconciliation_note: str | None = None,
) -> dict[str, Any]:
    statuses = [value["status"] for value in dimensions.values()]
    return {
        "backend": backend,
        "reference_backend": "native",
        "comparable_scope": scope,
        "content_equivalent_to_native": content_equivalent,
        "content_equivalence_note": content_note,
        "counter_reconciled": counter_reconciled,
        "reconciliation_note": reconciliation_note,
        "matched_count": statuses.count("matched"),
        "approximated_count": statuses.count("approximated"),
        "unmatched_count": statuses.count("unmatched"),
        "not_applicable_count": statuses.count("not_applicable"),
        "dimensions": dimensions,
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
        f"    capture.subscription_depth: {NATIVE_SUBSCRIPTION_DEPTH}\n"
        f"    capture.discovery_period_ms: {NATIVE_DISCOVERY_PERIOD_MS}\n"
        f'    storage.output_directory: "{output_root}"\n'
        f"    storage.failure_injection_delay_ms: {args.slow_writer_ms}\n"
        f"    storage.failure_injection_fail_after_bytes: {args.fail_after_bytes}\n"
        "    buffer.event_capacity: 4096\n"
        "    buffer.control_reserve: 64\n"
        "    buffer.payload_block_size: 4096\n"
        "    buffer.payload_block_count: 4096\n"
        f"    storage.chunk_size_bytes: {NATIVE_CHUNK_SIZE_BYTES}\n"
        f"    storage.segment_max_bytes: {NATIVE_SEGMENT_MAX_BYTES}\n"
        f"    storage.segment_max_duration_sec: {NATIVE_SEGMENT_MAX_DURATION_SEC}\n"
        "    status.publish_period_ms: 250\n",
        encoding="utf-8",
    )


def _write_rosbag2_configs(qos_path: Path, storage_path: Path, args: argparse.Namespace) -> None:
    topics = [f"{args.topic_prefix}{index}" for index in range(args.topics)]
    qos_lines: list[str] = []
    for topic in topics:
        qos_lines.extend(
            [
                f'"{topic}":',
                "  history: keep_last",
                f"  depth: {NATIVE_SUBSCRIPTION_DEPTH}",
                f"  reliability: {args.qos}",
                "  durability: volatile",
            ]
        )
    qos_path.write_text("\n".join(qos_lines) + "\n", encoding="utf-8")
    storage_path.write_text(
        "noChunking: false\n"
        "noChunkCRC: false\n"
        "enableDataCRC: true\n"
        "noMessageIndex: false\n"
        "noSummary: false\n"
        "noSummaryCRC: false\n"
        f"chunkSize: {NATIVE_CHUNK_SIZE_BYTES}\n"
        'compression: "None"\n',
        encoding="utf-8",
    )


def _rosbag2_command(
    args: argparse.Namespace,
    output_root: Path,
    qos_path: Path,
    storage_path: Path,
) -> list[str]:
    topics = [f"{args.topic_prefix}{index}" for index in range(args.topics)]
    return _command(
        args.rosbag2_command,
        {},
        [
            "--storage",
            "mcap",
            "--output",
            str(output_root),
            "--polling-interval",
            str(ROSBAG2_DISCOVERY_POLL_MS),
            "--max-bag-size",
            str(NATIVE_SEGMENT_MAX_BYTES),
            "--max-bag-duration",
            f"{NATIVE_SEGMENT_MAX_DURATION_SEC:g}",
            "--max-cache-size",
            str(ROSBAG2_MAX_CACHE_BYTES),
            "--qos-profile-overrides-path",
            str(qos_path),
            "--storage-config-file",
            str(storage_path),
            *topics,
        ],
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


def _benchmark_marker_fields(
    serialized: bytes, channel_topic: str, topic_prefix: str
) -> tuple[int, int, int] | None:
    """Decode the load publisher marker from a serialized ByteMultiArray.

    The publisher uses the default empty MultiArrayLayout. For CDR1 this makes
    the byte sequence begin at offset 16: encapsulation, empty dimension count,
    zero data offset, then the sequence length. Requiring this layout avoids a
    coincidental ``BBRSBEN1`` byte sequence elsewhere in malformed data.
    """
    suffix = channel_topic.removeprefix(topic_prefix)
    if not channel_topic.startswith(topic_prefix) or not suffix.isdecimal():
        return None
    if len(serialized) < 16:
        return None
    encapsulation = int.from_bytes(serialized[0:2], byteorder="big", signed=False)
    if encapsulation not in (0, 1):
        return None
    byteorder = "little" if encapsulation == 1 else "big"
    dimension_count = int.from_bytes(serialized[4:8], byteorder=byteorder, signed=False)
    data_offset = int.from_bytes(serialized[8:12], byteorder=byteorder, signed=False)
    payload_size = int.from_bytes(serialized[12:16], byteorder=byteorder, signed=False)
    payload_end = 16 + payload_size
    if (
        dimension_count != 0
        or data_offset != 0
        or payload_size < 32
        or payload_end > len(serialized)
    ):
        return None
    payload = serialized[16:payload_end]
    if payload[:8] != BENCHMARK_MARKER:
        return None
    sequence = int.from_bytes(payload[8:16], byteorder="little", signed=False)
    publisher_ns = int.from_bytes(payload[16:24], byteorder="little", signed=False)
    marker_topic_id = int.from_bytes(payload[24:28], byteorder="little", signed=False)
    if marker_topic_id != int(suffix):
        return None
    return sequence, publisher_ns, marker_topic_id


def _inspect_committed_messages(
    segments: list[Path],
    topic_prefix: str,
    clock_offsets: ClockOffsetSampler | None = None,
) -> tuple[int | None, int | None, list[float], int, int, str | None]:
    """Count retained benchmark payloads and rebuild the ingest-latency sample.

    ``clock_offsets`` is supplied when the recorder stamps messages with a
    realtime clock (rosbag2) rather than the steady clock the publisher marker
    carries. The offset is measured during the run and interpolated per message;
    it is never assumed. Messages whose corrected latency is negative are
    counted separately because a negative ingest latency proves the conversion
    is wrong, and the caller must invalidate the percentile rather than publish
    it.
    """
    try:
        from mcap.exceptions import McapError
        from mcap.reader import make_reader
    except ImportError:
        return None, None, [], 0, 0, "optional mcap package is not installed"
    count = 0
    byte_count = 0
    latency_population = 0
    negative_latency_count = 0
    latency_sample: list[float] = []
    seen_sequences: set[int] = set()
    random_source = random.Random(0)
    try:
        for segment in segments:
            with segment.open("rb") as stream:
                for _, channel, message in make_reader(stream).iter_messages():
                    if not channel.topic.startswith(topic_prefix):
                        continue
                    count += 1
                    byte_count += len(message.data)
                    marker = _benchmark_marker_fields(message.data, channel.topic, topic_prefix)
                    if marker is None:
                        continue
                    sequence, publisher_ns, _ = marker
                    if sequence in seen_sequences:
                        continue
                    seen_sequences.add(sequence)
                    recorder_ns: int | None = message.log_time
                    if clock_offsets is not None:
                        offset_ns = clock_offsets.offset_at(message.log_time)
                        recorder_ns = None if offset_ns is None else message.log_time - offset_ns
                    if recorder_ns is None:
                        continue
                    delta_ns = recorder_ns - publisher_ns
                    if delta_ns < 0:
                        negative_latency_count += 1
                        continue
                    latency_us = delta_ns / 1_000.0
                    latency_population += 1
                    if len(latency_sample) < LATENCY_SAMPLE_CAP:
                        latency_sample.append(latency_us)
                    else:
                        replacement = random_source.randrange(latency_population)
                        if replacement < LATENCY_SAMPLE_CAP:
                            latency_sample[replacement] = latency_us
    except (OSError, McapError, RuntimeError, ValueError) as error:
        return None, None, [], 0, 0, f"MCAP committed-message inspection failed: {error}"
    return count, byte_count, latency_sample, latency_population, negative_latency_count, None


def _publisher_command(
    args: argparse.Namespace, run_id: str, publisher_result_path: Path
) -> list[str]:
    """Build the load-generator command.

    Every backend drives the identical publisher invocation. Nothing in this
    function may depend on the backend under test.
    """
    return _command(
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/native_capture_benchmark.json")
    )
    parser.add_argument("--scenario", choices=[*SCENARIOS, "custom"], default="A")
    parser.add_argument("--backend", choices=sorted(BACKEND_LABELS), default="native")
    parser.add_argument(
        "--capture-command", default="ros2 run blackbox_capture_cpp blackbox_capture"
    )
    parser.add_argument("--rosbag2-command", default="ros2 bag record")
    parser.add_argument("--publisher-command", default="ros2 run blackbox_capture_bench publisher")
    parser.add_argument("--recorder-params", type=Path)
    parser.add_argument("--capture-output-dir", type=Path)
    parser.add_argument("--keep-work-directory", action="store_true")
    parser.add_argument("--include-samples", action="store_true")
    parser.add_argument(
        "--ready-pattern",
        default=None,
        help="Override the backend-specific liveness marker searched in the capture log",
    )
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
    if args.topics <= 0 or args.rate <= 0 or args.payload_bytes < 32 or args.duration_sec <= 0:
        raise ValueError(
            "topics, rate, and duration must be positive, and payload bytes must be at least 32 "
            "for the benchmark sequence and timestamp marker"
        )
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
    if args.backend == "rosbag2" and args.recorder_params is not None:
        raise ValueError("--recorder-params is native-only")
    if args.backend == "rosbag2" and (
        args.slow_writer_ms != 0 or args.fail_after_bytes != -1 or args.expect_storage_fault
    ):
        raise ValueError("native writer failure injection is unavailable for the rosbag2 backend")

    run_id = args.run_id or uuid.uuid4().hex[:12]
    repo = Path(__file__).resolve().parents[1]
    work_dir = Path(tempfile.mkdtemp(prefix=f"blackboxrs-{args.backend}-bench-"))
    output_root = (
        args.capture_output_dir.resolve() if args.capture_output_dir else work_dir / "capture"
    )
    params_path: Path | None = None
    qos_path: Path | None = None
    storage_config_path: Path | None = None
    if args.backend == "native":
        output_root.mkdir(parents=True, exist_ok=True)
        params_path = (
            args.recorder_params.resolve() if args.recorder_params else work_dir / "recorder.yaml"
        )
        if args.recorder_params is None:
            _write_generated_params(params_path, args, output_root)
    else:
        if output_root.exists():
            raise ValueError(f"rosbag2 output path already exists: {output_root}")
        output_root.parent.mkdir(parents=True, exist_ok=True)
        qos_path = work_dir / "rosbag2_qos.yaml"
        storage_config_path = work_dir / "rosbag2_mcap.yaml"
        _write_rosbag2_configs(qos_path, storage_config_path, args)

    publisher_result_path = work_dir / "publisher.json"
    capture_log_path = work_dir / "capture.log"
    publisher_log_path = work_dir / "publisher.log"
    if args.backend == "native":
        assert params_path is not None
        capture_command = _command(
            args.capture_command,
            {"params": str(params_path), "output": str(output_root)},
            ["--ros-args", "--params-file", str(params_path)],
        )
    else:
        assert qos_path is not None and storage_config_path is not None
        capture_command = _rosbag2_command(args, output_root, qos_path, storage_config_path)
    publisher_command = _publisher_command(args, run_id, publisher_result_path)

    errors: list[str] = []
    warnings: list[str] = []
    capture_exit_code: int | None = None
    publisher_exit_code: int | None = None
    startup_latency_ms: float | None = None
    shutdown_drain_ms: float | None = None
    forced_shutdown = False
    sampler: ProcessSampler | None = None
    status_collector = RosStatusCollector() if args.backend == "native" else None
    clock_offsets = ClockOffsetSampler() if args.backend == "rosbag2" else None
    ready_pattern = args.ready_pattern or ("READY" if args.backend == "native" else "Recording...")

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
            if status_collector is not None:
                status_collector.start()
            startup_latency_ms = _wait_ready(
                capture_process,
                capture_log_path,
                ready_pattern,
                args.startup_timeout_sec,
                capture_started,
            )
            if startup_latency_ms is None:
                errors.append(
                    f"capture did not emit the configured liveness marker {ready_pattern!r} "
                    "before timeout"
                )

            if capture_process.poll() is None and startup_latency_ms is not None:
                if args.backend == "native":
                    pid_selector = _capture_process_pid
                    segment_glob = "capture_*/segments/*.mcap"
                else:
                    pid_selector = _rosbag2_process_pid
                    segment_glob = "*.mcap"
                sampler = ProcessSampler(
                    capture_process.pid,
                    output_root,
                    args.sample_period_sec,
                    status_collector,
                    pid_selector=pid_selector,
                    closed_segment_glob=segment_glob,
                )
                sampler.start()
                if clock_offsets is not None:
                    clock_offsets.start()
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
            capture_exit_code, shutdown_drain_ms, forced_shutdown = _stop_process(
                capture_process, args.shutdown_timeout_sec
            )
            if clock_offsets is not None and clock_offsets.samples:
                clock_offsets.stop()
            if sampler is not None:
                sampler.stop()
            if status_collector is not None:
                status_collector.stop()

    publisher_result = _read_json(publisher_result_path)
    if publisher_result is None:
        errors.append("publisher did not produce a valid result JSON document")
    status: dict[str, Any] | None = None
    status_source: str | None = None
    session: dict[str, Any] | None = None
    capture_quality: dict[str, Any] | None = None
    sidecars: list[dict[str, Any]] = []
    segments: list[Path] = []
    partial_segments: list[Path] = []
    if args.backend == "native":
        assert status_collector is not None
        status, status_source = _select_status(
            status_collector.latest(), _find_latest_status(capture_log_path)
        )
        if not status_collector.available:
            warnings.append(
                "ROS status subscriber unavailable; final status was searched in the capture "
                "log: " + (status_collector.error or "unknown reason")
            )
        if args.recorder_params is not None:
            warnings.append(
                "external recorder parameters were not inspected for extra or missing topics; "
                "workload matching requires an independent configuration review"
            )
        if status is None:
            errors.append("capture log did not contain a blackboxrs.capture_status.v1 document")
        elif status.get("state") not in {
            "STOPPED_CLEAN",
            "STOPPED_INCOMPLETE",
            "INVARIANT_FAULT",
        }:
            errors.append("capture did not provide an authoritative terminal status")
        session_id = (
            status.get("session_id")
            if status and isinstance(status.get("session_id"), str)
            else None
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
    else:
        segments = sorted(output_root.glob("*.mcap")) if output_root.is_dir() else []
        warnings.extend(
            [
                "stock rosbag2 does not expose callback-received or reasoned-drop counters; "
                "missing published records cannot be attributed",
                "rosbag2 close/finalization was observed, but an fsync-equivalent durability "
                "contract was not measured",
            ]
        )
    if not segments and not (args.expect_storage_fault and partial_segments):
        errors.append("no closed MCAP segment was found")
    if not _capture_exit_is_acceptable(capture_exit_code, args.expect_storage_fault):
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
        negative_ingest_latency_count,
        mcap_count_error,
    ) = _inspect_committed_messages(
        segments,
        args.topic_prefix,
        clock_offsets=clock_offsets if args.backend == "rosbag2" else None,
    )
    if mcap_count_error:
        errors.append(mcap_count_error)
    if negative_ingest_latency_count:
        errors.append(
            f"{negative_ingest_latency_count} retained benchmark messages had negative ingest "
            "latency after clock-domain correction"
        )
    if (
        isinstance(serialized_retained, int)
        and ingest_latency_population + negative_ingest_latency_count != serialized_retained
    ):
        errors.append(
            "one or more retained benchmark messages had an invalid or duplicate publication marker"
        )
    final_sidecar = sidecars[-1] if sidecars else None
    sent = _first_number(publisher_result, ("sent",))
    actual_duration = _first_number(publisher_result, ("actual_duration_sec",))
    measurement_duration = (
        float(actual_duration)
        if isinstance(actual_duration, (int, float)) and actual_duration > 0
        else args.duration_sec
    )
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
    if args.backend == "rosbag2":
        retention_evicted_segments = 0
        retention_evicted_bytes = 0
    clean_process_close = capture_exit_code in (0, 130) and not forced_shutdown
    (
        serialized_committed,
        serialized_committed_bytes,
        serialized_scope_complete,
    ) = _serialized_session_totals(
        backend=args.backend,
        serialized_retained=serialized_retained,
        serialized_retained_bytes=serialized_retained_bytes,
        retention_evicted_segments=retention_evicted_segments,
        partial_segment_count=len(partial_segments),
        clean_process_close=clean_process_close,
    )
    if isinstance(retention_evicted_segments, (int, float)) and retention_evicted_segments > 0:
        warnings.append(
            "rolling retention evicted finalized segments; payload-only session totals cannot be "
            "reconstructed from the retained MCAP files"
        )
    if partial_segments:
        warnings.append(
            "a partial segment remains; finalized-file payload counts are retained, but "
            "payload-only session totals cannot be reconstructed"
        )
    clock_offset_drift = clock_offsets.drift_ns if clock_offsets is not None else 0
    clock_alignment_valid = args.backend == "native" or (
        clock_offset_drift is not None and clock_offset_drift <= CLOCK_OFFSET_DRIFT_LIMIT_NS
    )
    ingest_latency_valid = (
        not args.cross_host
        and serialized_scope_complete
        and clock_alignment_valid
        and isinstance(serialized_retained, int)
        and serialized_retained > 0
        and negative_ingest_latency_count == 0
        and ingest_latency_population == serialized_retained
    )
    if mcap_count_error:
        latency_reason = mcap_count_error
    elif args.cross_host:
        latency_reason = (
            "publisher and recorder steady clocks are not in a shared host clock domain"
        )
    elif not clock_alignment_valid:
        latency_reason = "realtime-minus-monotonic clock-offset drift exceeded the configured limit"
    elif retention_evicted_segments is None:
        latency_reason = "rolling-retention scope is unknown"
    elif retention_evicted_segments != 0:
        latency_reason = (
            "rolling eviction leaves only a retained subset of session ingest latencies"
        )
    elif negative_ingest_latency_count:
        latency_reason = "one or more corrected receive timestamps preceded their publisher markers"
    elif ingest_latency_population != serialized_retained:
        latency_reason = (
            "one or more retained benchmark payloads had an invalid or duplicate publication marker"
        )
    elif args.backend == "rosbag2":
        latency_reason = (
            "publisher steady timestamp to rosbag2 realtime receive timestamp, corrected using "
            "the sampled realtime-minus-monotonic offset"
        )
    else:
        latency_reason = (
            "publisher batch timestamp to native recorder callback steady timestamp; the current "
            "publisher reuses one pre-publish timestamp within each catch-up batch"
        )
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
    terminal_state = status.get("state") if status else None
    workload_matched = args.backend == "rosbag2" or args.recorder_params is None
    if args.backend == "native":
        if received is None or admitted is None or committed is None or dropped is None:
            errors.append("recorder status omitted one or more required reconciliation counters")
        if all(isinstance(value, (int, float)) for value in (received, committed, dropped)):
            if received != committed + dropped:
                errors.append(
                    "recorder received count does not reconcile with committed plus dropped"
                )
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
            errors.append(
                "final capture quality retained bytes do not match finalized segment files"
            )
        if status is not None and terminal_state == "STOPPED_CLEAN" and capture_quality is None:
            errors.append("clean terminal status has no final capture quality record")
        if args.expect_storage_fault:
            if not isinstance(storage_errors, (int, float)) or storage_errors <= 0:
                errors.append("expected storage fault did not increment storage_errors")
            if terminal_state == "STOPPED_CLEAN":
                errors.append("expected storage fault ended with a clean terminal state")
        elif terminal_state != "STOPPED_CLEAN":
            errors.append("ordinary benchmark did not end in STOPPED_CLEAN")
        if _requires_publisher_delivery_reconciliation(
            expect_storage_fault=args.expect_storage_fault,
            workload_matched=workload_matched,
            retention_evicted_segments=retention_evicted_segments,
            sent=sent,
            serialized_committed=serialized_committed,
            serialized_dropped=serialized_dropped,
        ):
            if sent != serialized_committed + serialized_dropped:
                errors.append(
                    "publisher sent count does not reconcile with serialized committed plus "
                    "recorder-accounted serialized drops; DDS or pre-callback loss is unexplained"
                )
        else:
            if args.expect_storage_fault:
                warnings.append(
                    "publisher-to-recorder DDS delivery is not a validity condition for an "
                    "intentional fail-stop storage experiment; callback-received recorder "
                    "accounting remains required"
                )
            else:
                warnings.append(
                    "serialized publisher-to-recorder delivery could not be reconciled because "
                    "the capture workload was external, rolling retention evicted records, or "
                    "drop details were unavailable"
                )
    elif isinstance(sent, int) and isinstance(serialized_retained, int):
        if sent != serialized_retained:
            errors.append(
                "publisher calls do not match rosbag2 retained records; stock rosbag2 cannot "
                "attribute the missing records"
            )
    else:
        errors.append("rosbag2 retained workload count could not be inspected")

    queue_capacity = _first_number(status, ("queue_capacity",), ("queue", "capacity"))
    queue_peak = _first_number(status, ("queue_peak",), ("queue", "peak_depth"))
    sidecar_peak_percent = _first_number(final_sidecar, ("peak_queue_utilization",))
    if queue_capacity and queue_peak is not None:
        queue_peak_utilization = queue_peak / queue_capacity
    elif sidecar_peak_percent is not None:
        queue_peak_utilization = sidecar_peak_percent / 100.0
    else:
        queue_peak_utilization = None

    if args.backend == "native":
        comparison = _native_comparison(args)
        comparison["counter_reconciled"] = not any(
            "reconcile" in error or "unexplained" in error for error in errors
        )
        comparison["reconciliation_note"] = (
            "Native aggregate callback counters and the retained workload count were checked "
            "against publisher calls where rolling retention and external parameters permitted."
        )
        measurement_limitations = [
            "The current load publisher reuses one steady timestamp for every message in a "
            "catch-up batch, so ingest latency includes publication-loop delay after that marker.",
            "Startup latency ends at the backend log liveness marker; complete subscription "
            "matching is checked separately before measurement.",
            "CPU and RSS cover the recorder process only; publisher resource use is not sampled.",
            "Write-service latency and trigger-to-durable-flush latency are not instrumented.",
        ]
        resource_process_selection = "native recorder descendant of ros2 run wrapper"
        latency_clock_domain = "cross_host_unknown" if args.cross_host else "local_steady"
    else:
        comparison = _rosbag2_comparison(args)
        measurement_limitations = [
            "Stock rosbag2 exposes retained records but no authoritative callback-received or "
            "reasoned-drop counters.",
            "The rosbag2 cache and native bounded event-ring plus payload arena are only an "
            "approximate buffering match.",
            "rosbag2 ingest latency uses a sampled realtime-minus-monotonic clock correction.",
            "Startup latency ends at the backend log liveness marker; complete subscription "
            "matching is checked separately before measurement.",
            "CPU and RSS cover the recorder process only; publisher resource use is not sampled.",
            "Write-service latency, durable flush latency, and trigger latency are unsupported.",
        ]
        resource_process_selection = "rosbag2 recorder process below ros2 CLI wrapper when present"
        latency_clock_domain = (
            "cross_host_unknown" if args.cross_host else "local_realtime_corrected_to_steady"
        )

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
            "actual_duration_sec": actual_duration,
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
        "capture_backend": BACKEND_LABELS[args.backend],
        "comparison": comparison,
        "measurement_limitations": measurement_limitations,
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
            "publish_calls": sent,
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
            "negative_ingest_latencies": negative_ingest_latency_count,
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
                "negative_count": negative_ingest_latency_count,
                "valid": ingest_latency_valid,
                "reason": latency_reason,
            },
            "write": {
                "p50": None,
                "p95": None,
                "p99": None,
                "supported": False,
                "reason": "the selected recorder does not expose write-service samples",
            },
            "trigger_to_flush": {
                "p50": None,
                "p95": None,
                "p99": None,
                "supported": False,
                "reason": "this workload does not issue and durably acknowledge a trigger",
            },
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
            "bytes_per_second": storage_bytes / measurement_duration
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
            "capture_closed_cleanly": clean_process_close
            and (args.backend == "rosbag2" or terminal_state == "STOPPED_CLEAN"),
            "final_status_state": status.get("state") if status else None,
            "status_source": status_source,
        },
        "recovery": session.get("recovery") if session else None,
        "provenance": {
            "capture_command": capture_command,
            "publisher_command": publisher_command,
            "generated_recorder_params": args.backend == "native" and args.recorder_params is None,
            "generated_rosbag2_qos": args.backend == "rosbag2",
            "generated_rosbag2_storage_config": args.backend == "rosbag2",
            "work_directory_retained": args.keep_work_directory,
            "resource_process_selection": resource_process_selection,
            "failure_injection": {
                "writer_delay_ms": args.slow_writer_ms,
                "fail_after_bytes": args.fail_after_bytes,
                "expect_storage_fault": args.expect_storage_fault,
            },
            "latency_clock_domain": latency_clock_domain,
            "clock_offset": clock_offsets.summary() if clock_offsets is not None else None,
            "publisher_marker_semantics": (
                "BBRSBEN1, global publish-call sequence, steady timestamp, and topic index in "
                "the first 32 ByteMultiArray payload bytes; one timestamp is currently reused "
                "within each catch-up batch"
            ),
        },
    }
    if args.include_samples:
        artifact["resource_samples"] = samples
    _atomic_json(args.output.resolve(), artifact)
    if args.keep_work_directory:
        retained = args.output.resolve().parent / f"{args.backend}_capture_work_{run_id}"
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
