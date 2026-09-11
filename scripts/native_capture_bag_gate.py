#!/usr/bin/env python3
"""Replay the committed ROS bag through the installed native recorder and verify fidelity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any

import yaml
from mcap.reader import make_reader


SCHEMA_VERSION = "blackboxrs.native_bag_gate.v1"
CONTROL_TOPIC = "/blackboxrs/events"
TOPICS = {
    "/source/utlidar/robot_odom": ("nav_msgs/msg/Odometry", 433),
    "/source/utlidar/imu": ("sensor_msgs/msg/Imu", 433),
    "/source/cmd_vel": ("geometry_msgs/msg/Twist", 43),
}
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
    paths: list[Path], *, allow_control: bool, reject_unexpected: bool
) -> tuple[dict[str, str], dict[str, list[bytes]]]:
    schemas: dict[str, str] = {}
    messages = {topic: [] for topic in TOPICS}
    unexpected: set[str] = set()
    for path in paths:
        with path.open("rb") as stream:
            reader = make_reader(stream)
            for schema, channel, message in reader.iter_messages():
                topic = channel.topic
                if topic == CONTROL_TOPIC and allow_control:
                    continue
                if topic not in TOPICS:
                    unexpected.add(topic)
                    continue
                _require(schema is not None, f"topic {topic} has no MCAP schema in {path}")
                previous = schemas.setdefault(topic, schema.name)
                _require(
                    previous == schema.name,
                    f"topic {topic} changed schema from {previous} to {schema.name}",
                )
                messages[topic].append(bytes(message.data))
    if reject_unexpected:
        _require(not unexpected, f"unexpected serialized topics: {sorted(unexpected)}")
    return schemas, messages


def _payload_sequence_digest(messages: list[bytes]) -> str:
    digest = hashlib.sha256()
    for payload in messages:
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    return digest.hexdigest()


def _write_params(path: Path, output_directory: Path) -> None:
    parameters: dict[str, Any] = {
        "runtime.role": "onboard",
        "runtime.observed_host": "",
        "capture.topics": list(TOPICS),
        "capture.discover_all": False,
        "capture.exclude_topics": [
            "/rosout",
            "/parameter_events",
            "/blackbox/capture_status",
        ],
        "capture.priority_tier_0": list(TOPICS),
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


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    bag_path = args.bag.resolve()
    _require(bag_path.is_file(), f"source bag does not exist: {bag_path}")
    source_schemas, source_messages = _message_inventory(
        [bag_path], allow_control=False, reject_unexpected=False
    )
    for topic, (expected_type, expected_count) in TOPICS.items():
        _require(source_schemas.get(topic) == expected_type, f"source type mismatch for {topic}")
        _require(
            len(source_messages[topic]) == expected_count,
            f"source count mismatch for {topic}: {len(source_messages[topic])} != {expected_count}",
        )

    environment = os.environ.copy()
    environment.setdefault("ROS_LOCALHOST_ONLY", "1")
    environment.setdefault("ROS_DOMAIN_ID", str(100 + os.getpid() % 100))
    rmw_implementation = environment.get("RMW_IMPLEMENTATION", "environment_default")

    with tempfile.TemporaryDirectory(prefix="blackboxrs-native-bag-gate-") as temporary:
        temporary_path = Path(temporary)
        output_directory = temporary_path / "capture"
        params_path = temporary_path / "params.yaml"
        recorder_log_path = temporary_path / "recorder.log"
        playback_log_path = temporary_path / "playback.log"
        _write_params(params_path, output_directory)

        recorder_command = [
            "ros2",
            "run",
            "blackbox_capture_cpp",
            "blackbox_capture",
            "--ros-args",
            "--params-file",
            str(params_path),
        ]
        playback_command = [
            "ros2",
            "bag",
            "play",
            str(bag_path),
            "--storage",
            "mcap",
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
                    playback_exit = _wait_or_kill(playback, args.playback_timeout_sec)
                _require(
                    playback_exit == 0,
                    f"rosbag playback failed ({playback_exit})\n"
                    + playback_log_path.read_text(encoding="utf-8", errors="replace"),
                )
                time.sleep(args.post_playback_drain_sec)
                _signal_group(recorder, signal.SIGINT)
                recorder_exit = _wait_or_kill(recorder, args.shutdown_timeout_sec)
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
            segments, allow_control=True, reject_unexpected=True
        )

        topic_report: dict[str, Any] = {}
        for topic, (expected_type, expected_count) in TOPICS.items():
            source_payloads = source_messages[topic]
            captured_payloads = captured_messages[topic]
            _require(captured_schemas.get(topic) == expected_type, f"captured type mismatch: {topic}")
            _require(
                len(captured_payloads) == expected_count,
                f"captured count mismatch for {topic}: {len(captured_payloads)} != {expected_count}",
            )
            _require(
                captured_payloads == source_payloads,
                f"captured payload sequence differs from source for {topic}",
            )
            source_digest = _payload_sequence_digest(source_payloads)
            capture_digest = _payload_sequence_digest(captured_payloads)
            topic_report[topic] = {
                "schema_type": expected_type,
                "source_count": len(source_payloads),
                "captured_count": len(captured_payloads),
                "source_payload_sequence_sha256": source_digest,
                "captured_payload_sequence_sha256": capture_digest,
                "payload_sequence_exact": source_digest == capture_digest,
            }

        result = {
            "schema_version": SCHEMA_VERSION,
            "valid": True,
            "source_bag": "examples/bags/go2_sim_odom_imu.mcap",
            "rmw_implementation": rmw_implementation,
            "ros_domain_id": environment["ROS_DOMAIN_ID"],
            "topics": topic_report,
            "source_messages": sum(len(values) for values in source_messages.values()),
            "captured_messages": sum(len(values) for values in captured_messages.values()),
            "capture": {
                "backend": status.get("backend"),
                "final_state": status.get("state"),
                "clean": quality.get("clean"),
                "segment_count": len(segments),
                "received": quality.get("received"),
                "committed": quality.get("committed"),
                "durable": quality.get("durable"),
                "dropped": quality.get("dropped"),
                "storage_errors": quality.get("storage_errors"),
            },
        }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", type=Path, default=repository / "examples/bags/go2_sim_odom_imu.mcap")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--playback-rate", type=float, default=4.0)
    parser.add_argument("--discovery-delay-sec", type=float, default=3.0)
    parser.add_argument("--post-playback-drain-sec", type=float, default=0.5)
    parser.add_argument("--ready-timeout-sec", type=float, default=20.0)
    parser.add_argument("--playback-timeout-sec", type=float, default=30.0)
    parser.add_argument("--shutdown-timeout-sec", type=float, default=15.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_gate(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
