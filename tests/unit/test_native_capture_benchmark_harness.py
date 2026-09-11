"""Unit tests for backend-neutral native capture benchmark supervision."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import validate


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "native_capture_benchmark.py"
SPEC = importlib.util.spec_from_file_location("native_capture_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def _serialized_marker(*, sequence: int = 7, timestamp_ns: int = 123, topic_id: int = 2) -> bytes:
    payload = bytearray(64)
    payload[:8] = benchmark.BENCHMARK_MARKER
    payload[8:16] = sequence.to_bytes(8, "little")
    payload[16:24] = timestamp_ns.to_bytes(8, "little")
    payload[24:28] = topic_id.to_bytes(4, "little")
    return b"\x00\x01\x00\x00" + bytes(8) + len(payload).to_bytes(4, "little") + payload


def test_marker_decoder_validates_cdr_layout_topic_and_sequence() -> None:
    serialized = _serialized_marker()

    assert benchmark._benchmark_marker_fields(
        serialized, "/blackbox_bench/topic2", "/blackbox_bench/topic"
    ) == (7, 123, 2)
    assert (
        benchmark._benchmark_marker_fields(
            serialized, "/blackbox_bench/topic1", "/blackbox_bench/topic"
        )
        is None
    )
    malformed = benchmark.BENCHMARK_MARKER + serialized
    assert (
        benchmark._benchmark_marker_fields(
            malformed, "/blackbox_bench/topic2", "/blackbox_bench/topic"
        )
        is None
    )


def test_inspector_always_returns_negative_latency_slot() -> None:
    result = benchmark._inspect_committed_messages([], "/blackbox_bench/topic")

    assert len(result) == 6
    assert result[3] == 0
    assert result[4] == 0


def test_expected_storage_fault_accepts_incomplete_exit_and_skips_dds_gate() -> None:
    assert benchmark._capture_exit_is_acceptable(2, expect_storage_fault=True)
    assert not benchmark._capture_exit_is_acceptable(2, expect_storage_fault=False)
    assert not benchmark._requires_publisher_delivery_reconciliation(
        expect_storage_fault=True,
        workload_matched=True,
        retention_evicted_segments=0,
        sent=100,
        serialized_committed=0,
        serialized_dropped=90,
    )


def test_native_partial_segment_preserves_retained_counts_but_nulls_session_totals() -> None:
    serialized_retained = 73
    serialized_retained_bytes = 8192

    committed, committed_bytes, scope_complete = benchmark._serialized_session_totals(
        backend="native",
        serialized_retained=serialized_retained,
        serialized_retained_bytes=serialized_retained_bytes,
        retention_evicted_segments=0,
        partial_segment_count=1,
        clean_process_close=False,
    )

    assert committed is None
    assert committed_bytes is None
    assert scope_complete is False
    assert serialized_retained == 73
    assert serialized_retained_bytes == 8192


def test_both_backends_use_identical_publisher_command(tmp_path: Path) -> None:
    parser = benchmark.build_parser()
    native = parser.parse_args(["--backend", "native", "--scenario", "custom"])
    rosbag2 = parser.parse_args(["--backend", "rosbag2", "--scenario", "custom"])

    native_command = benchmark._publisher_command(native, "same-run", tmp_path / "result.json")
    rosbag2_command = benchmark._publisher_command(rosbag2, "same-run", tmp_path / "result.json")

    assert native_command == rosbag2_command


def test_rosbag2_command_records_exact_topics_with_generated_configs(tmp_path: Path) -> None:
    args = benchmark.build_parser().parse_args(
        [
            "--backend",
            "rosbag2",
            "--scenario",
            "custom",
            "--topics",
            "2",
            "--qos",
            "reliable",
        ]
    )
    qos_path = tmp_path / "qos.yaml"
    storage_path = tmp_path / "storage.yaml"
    benchmark._write_rosbag2_configs(qos_path, storage_path, args)

    command = benchmark._rosbag2_command(args, tmp_path / "capture", qos_path, storage_path)

    assert "--all" not in command
    assert command[-2:] == ["/blackbox_bench/topic0", "/blackbox_bench/topic1"]
    assert "reliability: reliable" in qos_path.read_text(encoding="utf-8")
    assert f"chunkSize: {benchmark.NATIVE_CHUNK_SIZE_BYTES}" in storage_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("backend_name", "artifact_label"), [("native", "cpp"), ("rosbag2", "rosbag2")]
)
def test_schema_accepts_qualified_backend_artifact(backend_name: str, artifact_label: str) -> None:
    args = benchmark.build_parser().parse_args(["--backend", backend_name, "--scenario", "custom"])
    comparison = (
        benchmark._native_comparison(args)
        if backend_name == "native"
        else benchmark._rosbag2_comparison(args)
    )
    artifact = {
        "schema_version": benchmark.RESULT_SCHEMA,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": None,
        "git_dirty": None,
        "machine": {},
        "build": {},
        "scenario": {
            "name": "custom",
            "run_id": "unit",
            "topics": 1,
            "aggregate_rate_hz": 100.0,
            "payload_bytes": 64,
            "duration_sec": 1.0,
            "qos": "best_effort",
        },
        "capture_backend": artifact_label,
        "comparison": comparison,
        "measurement_limitations": ["unit fixture"],
        "capture_quality": None,
        "validity": {"valid": False, "errors": ["unit fixture"], "warnings": []},
        "counters": {
            "sent": None,
            "received": None,
            "admitted": None,
            "committed": None,
            "durable": None,
            "dropped": None,
            "dropped_bytes": None,
        },
        "drop_breakdown": None,
        "latency_us": {},
        "resources": {},
        "queue": {},
        "storage": {},
        "lifecycle": {},
        "recovery": None,
        "provenance": {},
    }
    schema = json.loads(
        (REPO / "scripts" / "native_capture_benchmark.schema.json").read_text(encoding="utf-8")
    )

    validate(artifact, schema)
    if backend_name == "rosbag2":
        assert comparison["counter_reconciled"] is False
        assert comparison["unmatched_count"] > 0
