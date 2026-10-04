#!/usr/bin/env python3
"""Replay a ROS bag through the installed native recorder and verify capture fidelity.

Two modes:

* default (no ``--bag``): the committed sim fixture with a fixed, hardcoded
  contract (topics, types, counts). This is what CI runs.
* ``--bag <rosbag2 dir | .db3 | .mcap>``: any recorded bag. Expectations are
  derived from the source bag itself (every non-bookkeeping topic with its type
  and message count, across every sqlite split), then the bag is replayed through
  the recorder and each topic must come back byte-identical with zero drops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
from typing import Any

import yaml
from mcap.reader import make_reader


SCHEMA_VERSION = "blackboxrs.native_bag_gate.v1"
CONTROL_TOPIC = "/blackboxrs/events"

# Expectations for the committed sim fixture. These stay hardcoded so the
# default CI invocation is a fixed contract rather than a self-fulfilling
# comparison of the recorder against whatever it happened to capture.
DEFAULT_TOPICS = {
    "/source/utlidar/robot_odom": ("nav_msgs/msg/Odometry", 433),
    "/source/utlidar/imu": ("sensor_msgs/msg/Imu", 433),
    "/source/cmd_vel": ("geometry_msgs/msg/Twist", 43),
}
DEFAULT_BAG_LABEL = "examples/bags/go2_sim_odom_imu.mcap"

# Topics that a rosbag2 recording carries as bookkeeping rather than robot
# evidence. They are never replayed into the recorder.
BOOKKEEPING_TOPICS = frozenset({"/rosout", "/parameter_events", "/events/write_split"})
ZERO_STATUS_COUNTERS = (
    "dropped",
    "dropped_bytes",
    "storage_errors",
    "clock_anomalies",
    "status_publish_failures",
    "graph_wait_faults",
    "graph_coverage_faults",
    "graph_snapshot_failures",
    "node_snapshot_failures",
    "endpoint_query_failures",
    "subscription_failures",
    "runtime_callback_faults",
    "rate_status_failures",
    "trigger_intent_lost",
    "rmw_messages_lost",
    "rmw_event_callbacks_unavailable",
    "incompatible_qos_events",
    "ambiguous_topic_types",
    "incident_manifest_errors",
    "retention_evicted_segments",
    "retention_evicted_events",
    "retention_evicted_bytes",
)
ZERO_QUALITY_COUNTERS = (
    "dropped",
    "bytes_dropped",
    "storage_errors",
    "clock_anomalies",
    "graph_wait_faults",
    "graph_coverage_faults",
    "graph_snapshot_failures",
    "node_snapshot_failures",
    "endpoint_query_failures",
    "subscription_failures",
    "runtime_callback_faults",
    "rmw_messages_lost",
    "rmw_event_callbacks_unavailable",
    "incompatible_qos_events",
    "ambiguous_topic_types",
    "retention_evicted_segments",
    "retention_evicted_events",
    "retention_evicted_bytes",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"expected JSON object: {path}")
    return value


def _message_inventory(
    paths: list[Path],
    topics: dict[str, tuple[str, int]] | frozenset[str] | set[str],
    *,
    allow_control: bool,
    reject_unexpected: bool,
    timestamps: list[int] | None = None,
) -> tuple[dict[str, str], dict[str, list[bytes]]]:
    """Read serialized payloads per topic from MCAP files."""
    wanted = set(topics)
    schemas: dict[str, str] = {}
    messages: dict[str, list[bytes]] = {topic: [] for topic in wanted}
    unexpected: set[str] = set()
    for path in paths:
        with path.open("rb") as stream:
            reader = make_reader(stream)
            for schema, channel, message in reader.iter_messages():
                topic = channel.topic
                if topic == CONTROL_TOPIC and allow_control:
                    continue
                if topic not in wanted:
                    unexpected.add(topic)
                    continue
                _require(schema is not None, f"topic {topic} has no MCAP schema in {path}")
                previous = schemas.setdefault(topic, schema.name)
                _require(
                    previous == schema.name,
                    f"topic {topic} changed schema from {previous} to {schema.name}",
                )
                messages[topic].append(bytes(message.data))
                if timestamps is not None:
                    timestamps.append(int(message.log_time))
    if reject_unexpected:
        _require(not unexpected, f"unexpected serialized topics: {sorted(unexpected)}")
    return schemas, messages


def _open_sqlite_readonly(db3: Path) -> sqlite3.Connection:
    # immutable=1 guarantees no journal/-shm/-wal side files next to the bag.
    return sqlite3.connect(f"file:{db3}?mode=ro&immutable=1", uri=True)


def _sqlite_bag_files(bag_dir: Path) -> list[Path]:
    """Return every ``.db3`` split file a rosbag2 recording directory contains.

    ``ros2 bag record`` splits a long session across several sequentially
    numbered files. ``metadata.yaml`` is authoritative; a sorted glob is the
    fallback. Reading only the first file silently truncates the source
    inventory, which would make a fidelity comparison pass against a subset
    of the recording. When both exist they must agree.
    """
    globbed = sorted(bag_dir.glob("*.db3"))
    metadata_path = bag_dir / "metadata.yaml"
    if not metadata_path.is_file():
        _require(bool(globbed), f"no .db3 files under {bag_dir}")
        return globbed
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    relative = metadata["rosbag2_bagfile_information"]["relative_file_paths"]
    root = bag_dir.resolve()
    resolved: list[Path] = []
    for entry in relative:
        candidate = (bag_dir / str(entry)).resolve()
        _require(
            candidate.is_relative_to(root),
            f"metadata.yaml bag file escapes the bag directory: {entry!r}",
        )
        _require(candidate.is_file(), f"metadata.yaml lists a missing bag file: {entry!r}")
        resolved.append(candidate)
    _require(
        {path.resolve() for path in globbed} == set(resolved),
        f"metadata.yaml bag files {[p.name for p in resolved]} disagree with "
        f"the .db3 files present {[p.name for p in globbed]}",
    )
    return resolved


def _sqlite_metadata_counts(bag_dir: Path) -> dict[str, int] | None:
    metadata_path = bag_dir / "metadata.yaml"
    if not metadata_path.is_file():
        return None
    info = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))["rosbag2_bagfile_information"]
    return {
        entry["topic_metadata"]["name"]: int(entry["message_count"])
        for entry in info.get("topics_with_message_count", [])
    }


def _sqlite_source_inventory(
    bag_dir: Path,
    wanted: set[str] | None,
    timestamps: list[int],
    topic_totals: dict[str, int],
) -> tuple[dict[str, str], dict[str, list[bytes]]]:
    """Read ``(type, ordered payloads)`` per topic from a sqlite3 rosbag2 bag.

    Only the rosbag2 sqlite schema is touched, so no ROS message packages and
    no deserialization are required. Rows are ordered by recorded timestamp (stable
    in file order) so the payload sequence matches the order ``ros2 bag play``
    publishes. With ``wanted=None`` every non-bookkeeping topic is read.
    Per-topic totals across all splits are cross-checked against metadata.yaml.
    """
    all_types: dict[str, str] = {}
    totals: dict[str, int] = {}
    files = _sqlite_bag_files(bag_dir)
    for db3 in files:
        connection = _open_sqlite_readonly(db3)
        try:
            for name, msg_type in connection.execute("SELECT name, type FROM topics"):
                previous = all_types.setdefault(name, msg_type)
                _require(
                    previous == msg_type,
                    f"topic {name} changed type from {previous} to {msg_type} in {db3.name}",
                )
            for name, count in connection.execute(
                "SELECT t.name, COUNT(m.id) FROM topics t "
                "LEFT JOIN messages m ON m.topic_id = t.id GROUP BY t.id"
            ):
                totals[name] = totals.get(name, 0) + int(count)
        finally:
            connection.close()
    topic_totals.update(totals)
    declared = _sqlite_metadata_counts(bag_dir)
    if declared is not None:
        mismatch = {
            name: (declared.get(name), totals.get(name))
            for name in sorted(set(declared) | set(totals))
            if declared.get(name, 0) != totals.get(name, 0)
        }
        _require(
            not mismatch,
            f"metadata.yaml message counts disagree with the .db3 files "
            f"(declared, found): {mismatch}",
        )
    if wanted is None:
        wanted = set(all_types) - BOOKKEEPING_TOPICS
    ordered: list[tuple[int, str, bytes]] = []
    for db3 in files:
        connection = _open_sqlite_readonly(db3)
        try:
            rows = connection.execute(
                "SELECT m.timestamp, t.name, m.data "
                "FROM messages m JOIN topics t ON m.topic_id = t.id ORDER BY m.id"
            )
            for timestamp, name, data in rows:
                if name in wanted:
                    ordered.append((int(timestamp), name, bytes(data)))
        finally:
            connection.close()
    ordered.sort(key=lambda row: row[0])
    messages: dict[str, list[bytes]] = {topic: [] for topic in wanted}
    for timestamp, name, data in ordered:
        messages[name].append(data)
        timestamps.append(timestamp)
    schemas = {name: all_types[name] for name in wanted if name in all_types}
    return schemas, messages


def _bag_storage(bag_path: Path) -> str:
    """Classify a bag path as ``mcap`` or ``sqlite3``."""
    if bag_path.is_dir():
        if sorted(bag_path.glob("*.mcap")):
            return "mcap"
        _require(
            bool(sorted(bag_path.glob("*.db3"))),
            f"no .mcap or .db3 storage files under {bag_path}",
        )
        return "sqlite3"
    suffix = bag_path.suffix.lower()
    _require(suffix in {".mcap", ".db3"}, f"unsupported bag format {suffix!r}")
    return "mcap" if suffix == ".mcap" else "sqlite3"


def _bag_source_files(bag_path: Path, storage: str) -> list[Path]:
    if storage == "sqlite3":
        return _sqlite_bag_files(bag_path if bag_path.is_dir() else bag_path.parent)
    if bag_path.is_dir():
        candidates = sorted(bag_path.glob("*.mcap"))
        _require(len(candidates) == 1, f"expected exactly one .mcap under {bag_path}")
        return candidates
    return [bag_path]


def _source_inventory(
    bag_path: Path,
    storage: str,
    timestamps: list[int],
    topic_totals: dict[str, int] | None = None,
) -> tuple[dict[str, str], dict[str, list[bytes]]]:
    """Read every non-bookkeeping topic's type and ordered payloads from a bag.

    ``topic_totals`` (optional) receives the message count of every topic in the
    bag, bookkeeping included, so the report can account for what was left out.
    """
    totals = topic_totals if topic_totals is not None else {}
    if storage == "sqlite3":
        return _sqlite_source_inventory(
            bag_path if bag_path.is_dir() else bag_path.parent, None, timestamps, totals
        )
    mcap_path = _bag_source_files(bag_path, storage)[0]
    with mcap_path.open("rb") as stream:
        summary = make_reader(stream).get_summary()
    _require(summary is not None, f"MCAP has no summary section: {mcap_path}")
    if summary.statistics is not None:
        for channel_id, count in summary.statistics.channel_message_counts.items():
            channel = summary.channels[channel_id]
            totals[channel.topic] = totals.get(channel.topic, 0) + int(count)
    wanted = {channel.topic for channel in summary.channels.values()} - BOOKKEEPING_TOPICS
    return _message_inventory(
        [mcap_path], wanted, allow_control=False, reject_unexpected=False, timestamps=timestamps
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files_record(files: list[Path]) -> list[dict[str, Any]]:
    return [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": _file_sha256(path)}
        for path in files
    ]


def _require_importable_types(topics: dict[str, str]) -> None:
    """Fail loudly, naming the topic, if a message type cannot be imported."""
    from rosidl_runtime_py.utilities import get_message

    for topic, msg_type in sorted(topics.items()):
        try:
            get_message(msg_type)
        except (ImportError, AttributeError, ValueError, ModuleNotFoundError) as error:
            raise RuntimeError(
                f"message type {msg_type!r} of source topic {topic} is not importable "
                f"({error}); install its message package or pass --exclude-topic {topic} "
                "(recorded in the report)"
            ) from error


def _bag_resource_profile(
    topics: dict[str, tuple[str, int]],
    messages: dict[str, list[bytes]],
    span_ns: int,
) -> dict[str, int | float]:
    """Size the recorder for the source bag.

    The fixed fixture parameters (64 KiB payload cap, 32 MiB retention) are
    sized for a 1 MB sim bag. A real recording has larger payloads and far
    more bytes than the pre-trigger retention window, so the recorder limits
    are derived from the source inventory. The zero-loss and zero-eviction
    checks themselves are not relaxed.
    """
    total_bytes = sum(len(payload) for topic in topics for payload in messages[topic])
    max_payload = max(len(payload) for topic in topics for payload in messages[topic])
    block = 4096
    segment_bytes = 64 * 1024 * 1024
    retention_bytes = int(total_bytes * 1.25) + 64 * 1024 * 1024
    return {
        "capture.max_payload_bytes": max(65536, math.ceil(max_payload / block) * block),
        "buffer.event_capacity": 16384,
        "buffer.payload_block_count": 16384,
        "buffer.memory_budget_bytes": 536870912,
        "storage.segment_max_bytes": segment_bytes,
        "storage.retention_max_bytes": retention_bytes,
        "storage.retention_max_segments": math.ceil(retention_bytes / segment_bytes) + 16,
        "storage.total_max_bytes": retention_bytes * 3 + segment_bytes,
        "buffer.history_seconds": math.ceil(span_ns / 1e9) + 60.0,
        "source_payload_bytes": total_bytes,
        "source_max_payload_bytes": max_payload,
    }


def _payload_sequence_digest(messages: list[bytes]) -> str:
    digest = hashlib.sha256()
    for payload in messages:
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    return digest.hexdigest()


def _write_params(
    path: Path,
    output_directory: Path,
    topics: list[str],
    overrides: dict[str, Any] | None = None,
) -> None:
    parameters: dict[str, Any] = {
        "runtime.role": "onboard",
        "runtime.observed_host": "",
        "capture.topics": list(topics),
        "capture.discover_all": False,
        "capture.exclude_topics": [
            "/rosout",
            "/parameter_events",
            "/blackbox/capture_status",
        ],
        "capture.priority_tier_0": list(topics),
        "capture.discovery_period_ms": 50,
        "capture.max_topics": 16,
        "capture.max_graph_nodes": 128,
        "capture.topic_string_bytes": 16384,
        "capture.max_payload_bytes": 65536,
        "capture.subscription_depth": 1000,
        "buffer.event_capacity": 4096,
        "buffer.control_reserve": 128,
        "buffer.payload_block_size": 4096,
        "buffer.payload_block_count": 4096,
        "buffer.memory_budget_bytes": 67108864,
        "buffer.high_watermark_ratio": 0.8,
        "storage.output_directory": str(output_directory),
        "storage.segment_max_bytes": 16777216,
        "storage.segment_max_events": 100000,
        "storage.segment_max_duration_sec": 60.0,
        "storage.chunk_size_bytes": 1048576,
        "storage.retention_max_bytes": 33554432,
        "storage.retention_max_segments": 4,
        "storage.max_incidents": 1,
        "storage.total_max_bytes": 134217728,
        "storage.max_sessions": 1,
        "storage.flush_period_ms": 100,
        "storage.failure_injection_delay_ms": 0,
        "storage.failure_injection_fail_after_bytes": -1,
        "trigger.dead_topic_timeout_sec": 30.0,
        "trigger.clock_forward_jump_sec": 1.0,
        "trigger.clock_backward_jump_sec": 0.001,
        "trigger.rate_window_sec": 5.0,
        "trigger.rate_deviation_ratio": 0.5,
        "buffer.history_seconds": 30.0,
        "buffer.post_trigger_seconds": 1.0,
        "status.publish_period_ms": 250,
        "shutdown.drain_timeout_ms": 10000,
    }
    for key, value in (overrides or {}).items():
        _require(key in parameters, f"unknown recorder parameter override {key}")
        parameters[key] = value
    document = {"/blackbox/blackbox_capture": {"ros__parameters": parameters}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _wait_for_ready(process: subprocess.Popen[bytes], log_path: Path, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        contents = log_path.read_text(encoding="utf-8", errors="replace")
        if "READY session=" in contents:
            return
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"native recorder exited before READY ({exit_code})\n{contents}")
        time.sleep(0.05)
    raise RuntimeError(
        f"native recorder did not become READY within {timeout_sec:.1f}s\n"
        + log_path.read_text(encoding="utf-8", errors="replace")
    )


def _signal_group(process: subprocess.Popen[bytes], requested_signal: signal.Signals) -> None:
    if process.poll() is None:
        os.killpg(process.pid, requested_signal)


def _wait_or_kill(process: subprocess.Popen[bytes], timeout_sec: float) -> int:
    try:
        return process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        process.wait(timeout=5)
        raise RuntimeError(f"process {process.args!r} did not stop within {timeout_sec:.1f}s")


def _final_status(log_path: Path) -> dict[str, Any]:
    for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        marker = "FINAL_STATUS "
        if marker in line:
            return json.loads(line.split(marker, 1)[1])
    raise RuntimeError(f"native recorder did not emit FINAL_STATUS\n{log_path.read_text()}")


def _validate_zero_counters(record: dict[str, Any], names: tuple[str, ...], label: str) -> None:
    nonzero = {name: record.get(name) for name in names if record.get(name) != 0}
    _require(not nonzero, f"{label} contains nonzero loss or fault counters: {nonzero}")


def _validate_segments(segments: list[Path]) -> None:
    _require(segments, "native recorder produced no finalized MCAP segment")
    for segment in segments:
        sidecar_path = segment.with_suffix(".json")
        _require(sidecar_path.is_file(), f"missing segment sidecar: {sidecar_path}")
        sidecar = _read_json(sidecar_path)
        _require(sidecar.get("schema_version") == "blackboxrs.capture_segment.v1", "bad sidecar")
        _require(sidecar.get("clean") is True, f"unclean sidecar: {sidecar_path}")
        _require(sidecar.get("recovered") is False, f"unexpected recovery: {sidecar_path}")
        _require(sidecar.get("path") == segment.name, f"sidecar path mismatch: {sidecar_path}")
        _require(sidecar.get("file_bytes") == segment.stat().st_size, "sidecar byte mismatch")
        digest = hashlib.sha256(segment.read_bytes()).hexdigest()
        _require(sidecar.get("sha256") == digest, f"sidecar checksum mismatch: {sidecar_path}")


def _plan_default(repository: Path) -> dict[str, Any]:
    """Fixed contract for the committed sim fixture (what CI runs)."""
    bag_path = (repository / DEFAULT_BAG_LABEL).resolve()
    _require(bag_path.is_file(), f"source bag does not exist: {bag_path}")
    source_schemas, source_messages = _message_inventory(
        [bag_path], set(DEFAULT_TOPICS), allow_control=False, reject_unexpected=False
    )
    for topic, (expected_type, expected_count) in DEFAULT_TOPICS.items():
        _require(source_schemas.get(topic) == expected_type, f"source type mismatch for {topic}")
        _require(
            len(source_messages[topic]) == expected_count,
            f"source count mismatch for {topic}: {len(source_messages[topic])} != {expected_count}",
        )
    return {
        "mode": "default_fixture",
        "bag_path": bag_path,
        "bag_label": DEFAULT_BAG_LABEL,
        "storage": "mcap",
        "topics": dict(DEFAULT_TOPICS),
        "messages": source_messages,
        "source_files": [bag_path],
        "param_overrides": {},
        "excluded_topics": [],
        "bookkeeping_excluded": {},
        "empty_source_topics": [],
        "playback_timeout_sec": 30.0,
        "shutdown_timeout_sec": 15.0,
    }


def _plan_source_bag(args: argparse.Namespace) -> dict[str, Any]:
    """Derive expectations from the source bag's own inventory."""
    bag_path = args.bag.resolve()
    _require(bag_path.exists(), f"source bag does not exist: {bag_path}")
    storage = _bag_storage(bag_path)
    source_files = _bag_source_files(bag_path, storage)
    timestamps: list[int] = []
    topic_totals: dict[str, int] = {}
    schemas, messages = _source_inventory(bag_path, storage, timestamps, topic_totals)
    _require(bool(timestamps), f"source bag contains no messages: {bag_path}")

    bookkeeping = {
        topic: count for topic, count in sorted(topic_totals.items()) if topic in BOOKKEEPING_TOPICS
    }
    excluded = sorted(set(args.exclude_topic))
    unknown = [topic for topic in excluded if topic not in schemas]
    _require(not unknown, f"--exclude-topic names topics not in the source bag: {unknown}")
    candidates = {t: ty for t, ty in schemas.items() if t not in excluded}
    empty = sorted(t for t in candidates if not messages[t])
    topics = {t: (ty, len(messages[t])) for t, ty in candidates.items() if messages[t]}
    _require(bool(topics), "no replayable topics remain in the source bag")
    _require_importable_types({t: ty for t, (ty, _count) in topics.items()})

    span_ns = max(timestamps) - min(timestamps)
    profile = _bag_resource_profile(topics, messages, span_ns)
    replay_sec = span_ns / 1e9 / args.playback_rate
    return {
        "mode": "source_bag",
        "bag_path": bag_path,
        "bag_label": bag_path.name,
        "storage": storage,
        "topics": topics,
        "messages": {t: messages[t] for t in topics},
        "source_files": source_files,
        "param_overrides": {
            key: value for key, value in profile.items() if not key.startswith("source_")
        },
        "resource_profile": profile,
        "excluded_topics": excluded,
        "bookkeeping_excluded": bookkeeping,
        "empty_source_topics": empty,
        "span_sec": span_ns / 1e9,
        "playback_timeout_sec": replay_sec + args.discovery_delay_sec + 60.0,
        "shutdown_timeout_sec": 60.0,
    }


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[1]
    plan = _plan_default(repository) if args.bag is None else _plan_source_bag(args)
    playback_timeout_sec = (
        args.playback_timeout_sec
        if args.playback_timeout_sec is not None
        else plan["playback_timeout_sec"]
    )
    shutdown_timeout_sec = (
        args.shutdown_timeout_sec
        if args.shutdown_timeout_sec is not None
        else plan["shutdown_timeout_sec"]
    )
    source_files: list[Path] = plan["source_files"]
    source_record = _source_files_record(source_files)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "valid": False,
        "mode": plan["mode"],
        "source_bag": plan["bag_label"],
        "source": {
            "name": plan["bag_path"].name,
            "path": plan["bag_label"] if plan["mode"] == "default_fixture" else str(plan["bag_path"]),
            "storage": plan["storage"],
            "files": source_record,
            "total_bytes": sum(entry["bytes"] for entry in source_record),
        },
        "excluded_topics": plan["excluded_topics"],
        "bookkeeping_excluded": plan["bookkeeping_excluded"],
        "empty_source_topics": plan["empty_source_topics"],
        "resource_profile": plan.get("resource_profile", {}),
        "topics": {},
    }
    try:
        _run_capture_and_compare(args, plan, report, playback_timeout_sec, shutdown_timeout_sec)
        _require(
            _source_files_record(source_files) == source_record,
            "source bag files changed during the run",
        )
        report["source"]["unchanged_after_run"] = True
        report["valid"] = True
    except RuntimeError as error:
        report["error"] = str(error)[:4000]
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _run_capture_and_compare(
    args: argparse.Namespace,
    plan: dict[str, Any],
    report: dict[str, Any],
    playback_timeout_sec: float,
    shutdown_timeout_sec: float,
) -> None:
    TOPICS: dict[str, tuple[str, int]] = plan["topics"]
    source_messages: dict[str, list[bytes]] = plan["messages"]
    environment = os.environ.copy()
    environment.setdefault("ROS_LOCALHOST_ONLY", "1")
    environment.setdefault("ROS_DOMAIN_ID", str(100 + os.getpid() % 100))
    rmw_implementation = environment.get("RMW_IMPLEMENTATION", "environment_default")
    report["rmw_implementation"] = rmw_implementation
    report["ros_domain_id"] = environment["ROS_DOMAIN_ID"]

    with tempfile.TemporaryDirectory(prefix="blackboxrs-native-bag-gate-") as temporary:
        temporary_path = Path(temporary)
        output_directory = temporary_path / "capture"
        params_path = temporary_path / "params.yaml"
        recorder_log_path = temporary_path / "recorder.log"
        playback_log_path = temporary_path / "playback.log"
        _write_params(params_path, output_directory, list(TOPICS), plan["param_overrides"])

        recorder_command = [
            "ros2",
            "run",
            "blackbox_capture_cpp",
            "blackbox_capture",
            "--ros-args",
            "--params-file",
            str(params_path),
        ]
        if plan["storage"] == "sqlite3":
            play_target = plan["bag_path"] if plan["bag_path"].is_dir() else plan["bag_path"].parent
        else:
            play_target = plan["bag_path"]
        playback_command = [
            "ros2",
            "bag",
            "play",
            str(play_target),
            "--storage",
            str(plan["storage"]),
            "--topics",
            *TOPICS,
            "--rate",
            str(args.playback_rate),
            "--delay",
            str(args.discovery_delay_sec),
            "--disable-keyboard-controls",
            "--wait-for-all-acked",
            "3000",
        ]
        recorder: subprocess.Popen[bytes] | None = None
        playback: subprocess.Popen[bytes] | None = None
        try:
            with recorder_log_path.open("wb") as recorder_log:
                recorder = subprocess.Popen(
                    recorder_command,
                    stdout=recorder_log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                )
                _wait_for_ready(recorder, recorder_log_path, args.ready_timeout_sec)
                with playback_log_path.open("wb") as playback_log:
                    playback = subprocess.Popen(
                        playback_command,
                        stdout=playback_log,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        start_new_session=True,
                    )
                    playback_exit = _wait_or_kill(playback, playback_timeout_sec)
                _require(
                    playback_exit == 0,
                    f"rosbag playback failed ({playback_exit})\n"
                    + playback_log_path.read_text(encoding="utf-8", errors="replace"),
                )
                time.sleep(args.post_playback_drain_sec)
                _signal_group(recorder, signal.SIGINT)
                recorder_exit = _wait_or_kill(recorder, shutdown_timeout_sec)
        finally:
            if playback is not None and playback.poll() is None:
                _signal_group(playback, signal.SIGKILL)
                playback.wait(timeout=5)
            if recorder is not None and recorder.poll() is None:
                _signal_group(recorder, signal.SIGKILL)
                recorder.wait(timeout=5)

        _require(
            recorder_exit == 0,
            f"native recorder failed ({recorder_exit})\n"
            + recorder_log_path.read_text(encoding="utf-8", errors="replace"),
        )
        status = _final_status(recorder_log_path)
        _require(status.get("state") == "STOPPED_CLEAN", f"bad final state: {status.get('state')}")
        _validate_zero_counters(status, ZERO_STATUS_COUNTERS, "final status")
        _require(status.get("drop_breakdown") == [], "final status has drop details")
        _require(status.get("writer_faulted") is False, "writer faulted")
        _require(status.get("writer_alive") is False, "writer still alive after final status")
        _require(status.get("accepting") is False, "recorder still accepting after final status")
        _require(status.get("queue_depth") == 0, "recorder did not drain its queue")
        _require(
            status.get("received") == status.get("admitted") == status.get("committed")
            == status.get("durable"),
            "final recorder counters do not reconcile",
        )
        _require(status.get("topic_coverage_truncated") is False, "topic coverage truncated")
        _require(status.get("node_coverage_truncated") is False, "node coverage truncated")

        pointer = _read_json(output_directory / "current_session.json")
        session_directory = (output_directory / str(pointer.get("path", ""))).resolve()
        _require(
            session_directory.is_relative_to(output_directory.resolve()),
            "current session pointer escaped the capture root",
        )
        _require(session_directory.is_dir(), "current session directory is missing")
        quality = _read_json(session_directory / "capture_quality.json")
        _require(quality.get("schema_version") == "blackboxrs.capture_quality.v1", "bad quality")
        _require(quality.get("clean") is True, "capture quality is not clean")
        _validate_zero_counters(quality, ZERO_QUALITY_COUNTERS, "capture quality")
        _require(quality.get("drop_breakdown") == [], "capture quality has drop details")
        _require(
            quality.get("received") == quality.get("admitted") == quality.get("committed")
            == quality.get("durable"),
            "capture quality counters do not reconcile",
        )

        segments_directory = session_directory / "segments"
        partials = sorted(segments_directory.glob("*.partial.mcap"))
        _require(not partials, f"partial native segments remain: {partials}")
        segments = sorted(segments_directory.glob("*.mcap"))
        _validate_segments(segments)
        captured_schemas, captured_messages = _message_inventory(
            segments, set(TOPICS), allow_control=True, reject_unexpected=True
        )

        report["capture"] = {
            "backend": status.get("backend"),
            "final_state": status.get("state"),
            "clean": quality.get("clean"),
            "segment_count": len(segments),
            "received": quality.get("received"),
            "committed": quality.get("committed"),
            "durable": quality.get("durable"),
            "dropped": quality.get("dropped"),
            "storage_errors": quality.get("storage_errors"),
        }
        topic_report: dict[str, Any] = report["topics"]
        failures: list[str] = []
        for topic, (expected_type, expected_count) in TOPICS.items():
            source_payloads = source_messages[topic]
            captured_payloads = captured_messages.get(topic, [])
            matching_prefix = 0
            for source_payload, captured_payload in zip(source_payloads, captured_payloads):
                if source_payload != captured_payload:
                    break
                matching_prefix += 1
            source_digest = _payload_sequence_digest(source_payloads)
            capture_digest = _payload_sequence_digest(captured_payloads)
            topic_report[topic] = {
                "schema_type": expected_type,
                "captured_schema_type": captured_schemas.get(topic),
                "source_count": len(source_payloads),
                "captured_count": len(captured_payloads),
                "matching_prefix": matching_prefix,
                "source_payload_sequence_sha256": source_digest,
                "captured_payload_sequence_sha256": capture_digest,
                "payload_sequence_exact": source_payloads == captured_payloads,
            }
            if captured_schemas.get(topic) != expected_type:
                failures.append(f"captured type mismatch: {topic}")
            if len(captured_payloads) != expected_count:
                failures.append(
                    f"captured count mismatch for {topic}: "
                    f"{len(captured_payloads)} != {expected_count}"
                )
            if source_payloads != captured_payloads:
                failures.append(
                    f"captured payload sequence differs from source for {topic} "
                    f"(identical prefix {matching_prefix} of {len(source_payloads)})"
                )
        report["source_messages"] = sum(len(values) for values in source_messages.values())
        report["captured_messages"] = sum(len(values) for values in captured_messages.values())
        _require(not failures, "; ".join(failures))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bag",
        type=Path,
        default=None,
        help="rosbag2 directory, .db3, or .mcap to replay with expectations derived from "
        "its inventory; omit for the fixed committed-fixture contract",
    )
    parser.add_argument(
        "--exclude-topic",
        action="append",
        default=[],
        metavar="TOPIC",
        help="source topic to leave out of replay (repeatable, recorded in the report); "
        "only valid with --bag",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--playback-rate", type=float, default=4.0)
    parser.add_argument("--discovery-delay-sec", type=float, default=3.0)
    parser.add_argument("--post-playback-drain-sec", type=float, default=0.5)
    parser.add_argument("--ready-timeout-sec", type=float, default=20.0)
    parser.add_argument(
        "--playback-timeout-sec",
        type=float,
        default=None,
        help="default: 30 for the fixture, bag duration / rate + 60 for --bag",
    )
    parser.add_argument(
        "--shutdown-timeout-sec",
        type=float,
        default=None,
        help="default: 15 for the fixture, 60 for --bag",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.bag is None and args.exclude_topic:
        parser.error("--exclude-topic requires --bag")
    result = run_gate(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
