from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blackboxrs.core.config import BlackBoxConfig, CaptureConfig, ConfigError
from blackboxrs.recording import native as native_module
from blackboxrs.recording.native import (
    CONTROL_SCHEMA,
    NativeCaptureDependencyError,
    NativeCaptureEvent,
    NativeCaptureReader,
)


mcap_writer = pytest.importorskip("mcap.writer")


def _write_session(
    root: Path,
    *,
    malformed_control: bool = False,
    dropped: int = 0,
    ros_rollback: bool = False,
) -> tuple[Path, bytes]:
    session = root / "capture_test"
    segments = session / "segments"
    segments.mkdir(parents=True)
    segment = segments / "0000000000000000.mcap"
    ros_base = int(datetime(2026, 8, 8, tzinfo=timezone.utc).timestamp() * 1e9)

    with segment.open("wb") as stream:
        writer = mcap_writer.Writer(
            stream,
            compression=mcap_writer.CompressionType.NONE,
            use_chunking=False,
            enable_crcs=True,
            enable_data_crcs=True,
        )
        writer.start(profile="ros2", library="blackbox_capture_cpp/test")
        ros_schema = writer.register_schema("sensor_msgs/msg/Imu", "ros2msg", b"")
        ros_channel = writer.register_channel(
            "/imu/data",
            "cdr",
            ros_schema,
            metadata={
                "blackboxrs.topic_id": "7",
                "blackboxrs.ros_type": "sensor_msgs/msg/Imu",
                "blackboxrs.serialization_format": "cdr",
                "blackboxrs.time_contract": ("log_time_monotonic_publish_time_ros"),
            },
        )
        control_schema = writer.register_schema(
            CONTROL_SCHEMA,
            "jsonschema",
            b'{"type":"object"}',
        )
        control_channel = writer.register_channel("/blackboxrs/events", "json", control_schema)
        raw_payload = b"\x00\x01opaque-cdr-payload"
        writer.add_message(
            ros_channel,
            log_time=10_000,
            publish_time=ros_base,
            sequence=100,
            data=raw_payload,
        )
        if malformed_control:
            control_data = b'{"not":"the envelope"}'
        else:
            control_data = json.dumps(
                {
                    "schema_version": 1,
                    "kind": "graph",
                    "monotonic_ns": 11_000,
                    "ros_time_ns": ros_base - 1_000 if ros_rollback else ros_base + 1_000,
                    "sequence": 101,
                    "topic_id": 7,
                    "flags": 2,
                    "payload": {
                        "change": "publisher_appeared",
                        "topic": "/imu/data",
                    },
                }
            ).encode()
        writer.add_message(
            control_channel,
            log_time=11_000,
            publish_time=ros_base - 1_000 if ros_rollback else ros_base + 1_000,
            sequence=101,
            data=control_data,
        )
        writer.finish()

    digest = hashlib.sha256(segment.read_bytes()).hexdigest()
    sidecar = {
        "schema": "blackboxrs.capture_segment.v1",
        "accounting_scope": "session_cumulative",
        "session_id": "sess_native",
        "segment_index": 0,
        "path": "segments/0000000000000000.mcap",
        "clean": True,
        "recovered": False,
        "first_sequence": 100,
        "last_sequence": 101,
        "event_count": 2,
        "file_bytes": segment.stat().st_size,
        "received": 2 + dropped,
        "admitted": 2,
        "committed": 2,
        "dropped": dropped,
        "bytes_captured": len(raw_payload) + len(control_data),
        "bytes_dropped": dropped * 64,
        "peak_queue_utilization": 0.5,
        "storage_errors": [],
        "clock_anomalies": 0,
        "monotonic_start_ns": 10_000,
        "monotonic_end_ns": 11_000,
        "sha256": digest,
    }
    segment.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    (session / "session.json").write_text(
        json.dumps(
            {
                "schema": "blackboxrs.capture_session.v1",
                "session_id": "sess_native",
                "monotonic_anchor_ns": 10_000,
                "system_time_anchor_ns": ros_base,
            }
        ),
        encoding="utf-8",
    )
    (session / "capture_quality.json").write_text(
        json.dumps(_quality_document(sidecar, dropped=dropped)), encoding="utf-8"
    )
    return session, raw_payload


def _quality_document(
    sidecar: dict[str, object], *, dropped: int = 0, **overrides: object
) -> dict[str, object]:
    quality: dict[str, object] = {
        "schema_version": "blackboxrs.capture_quality.v1",
        "session_id": "sess_native",
        "backend": "cpp",
        "clean": True,
        "received": 2 + dropped,
        "admitted": 2,
        "committed": 2,
        "durable": 2,
        "dropped": dropped,
        "bytes_captured": sidecar["bytes_captured"],
        "bytes_dropped": dropped * 64,
        "storage_errors": 0,
        "clock_anomalies": 0,
        "graph_wait_faults": 0,
        "graph_coverage_faults": 0,
        "graph_snapshot_failures": 0,
        "node_snapshot_failures": 0,
        "endpoint_query_failures": 0,
        "subscription_failures": 0,
        "runtime_callback_faults": 0,
        "rmw_messages_lost": 0,
        "rmw_event_callbacks_unavailable": 0,
        "incompatible_qos_events": 0,
        "ambiguous_topic_types": 0,
        "best_effort_topics": 0,
        "topic_coverage_truncated": False,
        "node_coverage_truncated": False,
        "delivery_scope": "callback_received",
        "graph_scope": "configured",
        "peak_queue_depth": 2,
        "queue_capacity": 10,
        "retained_segments": 1,
        "retained_events": 2,
        "retained_bytes": sidecar["file_bytes"],
        "retention_evicted_segments": 0,
        "retention_evicted_events": 0,
        "retention_evicted_bytes": 0,
        "retention_max_segments": 4,
        "retention_max_bytes": 1_000_000,
        "monotonic_start_ns": 10_000,
        "monotonic_end_ns": 11_000,
        "capture_memory_budget_bytes": 1_000_000,
        "configured_memory_budget_bytes": 2_000_000,
        "capture_started_monotonic_ns": 9_000,
        "capture_ended_monotonic_ns": 12_000,
        "drop_breakdown": [],
    }
    quality.update(overrides)
    return quality


def test_reader_keeps_cdr_out_of_blackbox_event(tmp_path: Path):
    session, raw_payload = _write_session(tmp_path)
    reader = NativeCaptureReader(session)

    events = list(reader)

    assert [event.sequence for event in events] == [100, 101]
    assert not hasattr(events[0], "serialized_data")
    assert events[0].message_type == "sensor_msgs/msg/Imu"
    assert events[1].kind == "graph"
    assert reader.quality.completeness == "complete"
    assert reader.quality.captured == 2
    assert reader.quality.durable == 2

    adapted = events[0].to_blackbox_event()
    assert adapted.source == "native_capture"
    assert adapted.event_type == "ros.serialized_message"
    assert adapted.data["evidence_ref"].startswith("native_capture/segments/")
    assert raw_payload.hex() not in adapted.to_jsonl()
    assert "opaque-cdr-payload" not in adapted.to_jsonl()


def test_native_dead_topic_trigger_keeps_python_incident_semantics(tmp_path: Path):
    native = NativeCaptureEvent(
        kind="trigger",
        monotonic_ns=10,
        ros_time_ns=int(datetime(2026, 8, 8, tzinfo=timezone.utc).timestamp() * 1e9),
        sequence=4,
        topic_id=7,
        flags=4,
        topic="/blackboxrs/events",
        message_type=CONTROL_SCHEMA,
        serialization_format="json",
        payload_size=64,
        evidence_ref="native_capture/segment#sequence=4",
        segment_path=tmp_path / "segment.mcap",
        control_payload={"code": 1, "severity": 2, "topic": "/imu/data"},
    )

    event = native.to_blackbox_event()
    assert event.event_type == "anomaly.dead_topic"
    assert event.data["detector"] == "dead_topic"
    assert event.data["topic"] == "/imu/data"
    assert event.severity == "error"
    assert event.metadata["detector_class"] == "DeadTopicDetector"
    assert event.metadata["target_subsystem"] == "ros"


def test_python_projection_preserves_monotonic_order_across_ros_rollback(
    tmp_path: Path,
):
    session, _ = _write_session(tmp_path, ros_rollback=True)
    reader = NativeCaptureReader(session)

    events = list(reader.iter_blackbox_events())

    assert [event.metadata["sequence"] for event in events] == [100, 101]
    assert events[0].timestamp < events[1].timestamp
    assert events[0].metadata["ros_time_ns"] > events[1].metadata["ros_time_ns"]
    assert all(
        event.metadata["timestamp_source"] == "system_monotonic_anchored" for event in events
    )


def test_malformed_control_is_skipped_and_quality_is_incomplete(tmp_path: Path):
    session, _ = _write_session(tmp_path, malformed_control=True)
    reader = NativeCaptureReader(session)

    events = list(reader)

    assert [event.sequence for event in events] == [100]
    assert reader.quality.completeness == "incomplete"
    assert reader.quality.malformed_records == 1
    assert "malformed_record" in reader.quality.incomplete_reasons
    assert "committed_count_mismatch" in reader.quality.incomplete_reasons


def test_truncated_segment_returns_valid_prefix(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    segment = next((session / "segments").glob("*.mcap"))
    segment.write_bytes(segment.read_bytes()[:-64])
    reader = NativeCaptureReader(session)

    events = list(reader)

    assert [event.sequence for event in events] == [100, 101]
    assert reader.quality.completeness == "incomplete"
    assert "truncated_or_invalid_segment" in reader.quality.incomplete_reasons
    assert "segment_checksum_mismatch" in reader.quality.incomplete_reasons


def test_bounded_session_reconciles_intentional_retention(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    sidecar_path = next((session / "segments").glob("*.json"))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar.update(
        {
            "accounting_scope": "session_cumulative",
            "received": 5,
            "admitted": 5,
            "committed": 5,
            "event_count": 2,
        }
    )
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    (session / "capture_quality.json").write_text(
        json.dumps(
            _quality_document(
                sidecar,
                received=5,
                admitted=5,
                committed=5,
                durable=5,
                retained_events=2,
                retention_evicted_segments=1,
                retention_evicted_events=3,
                retention_evicted_bytes=1024,
            )
        ),
        encoding="utf-8",
    )

    reader = NativeCaptureReader(session)
    assert len(list(reader)) == 2
    assert reader.quality.completeness == "complete"
    assert reader.quality.committed == 5
    assert reader.quality.retained_events == 2
    assert reader.quality.retention_evicted_events == 3


def test_incident_manifest_exposes_truncated_requested_history(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    source_segment = next((session / "segments").glob("*.mcap"))
    source_sidecar = source_segment.with_suffix(".json")
    incident = session / "incidents" / "incident_test"
    incident.mkdir(parents=True)
    segment = incident / source_segment.name
    sidecar_path = incident / source_sidecar.name
    shutil.copy2(source_segment, segment)
    shutil.copy2(source_sidecar, sidecar_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["path"] = segment.name
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    manifest = incident / "capture.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "blackboxrs.incident_capture.v1",
                "session_id": "sess_native",
                "trigger_sequence": 101,
                "trigger_monotonic_ns": 11_000,
                "monotonic_anchor_ns": 10_000,
                "system_time_anchor_ns": int(
                    datetime(2026, 8, 8, tzinfo=timezone.utc).timestamp() * 1e9
                ),
                "requested_start_monotonic_ns": 9_000,
                "requested_end_monotonic_ns": 12_000,
                "actual_start_monotonic_ns": 10_000,
                "actual_end_monotonic_ns": 11_000,
                "window_event_count": 2,
                "history_complete": False,
                "post_window_elapsed": True,
                "links_complete": True,
                "received": 2,
                "committed": 2,
                "dropped": 0,
                "segments": [
                    {
                        "path": segment.name,
                        "segment_index": 0,
                        "first_monotonic_ns": 10_000,
                        "last_monotonic_ns": 11_000,
                        "first_sequence": 100,
                        "last_sequence": 101,
                        "event_count": 2,
                        "file_bytes": segment.stat().st_size,
                        "sha256": sidecar["sha256"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    reader = NativeCaptureReader(incident)
    assert len(list(reader)) == 2
    assert reader.quality.completeness == "incomplete"
    assert reader.quality.history_complete is False
    assert "pre_trigger_history_truncated" in reader.quality.incomplete_reasons

    manifest_reader = NativeCaptureReader(manifest)
    assert len(list(manifest_reader)) == 2
    assert manifest_reader.quality.history_complete is False
    assert "unsupported_segment_schema" not in {issue.code for issue in manifest_reader.issues}

    contradictory = json.loads(manifest.read_text(encoding="utf-8"))
    contradictory["segments"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(contradictory), encoding="utf-8")
    contradictory_reader = NativeCaptureReader(manifest)
    assert len(list(contradictory_reader)) == 2
    assert contradictory_reader.quality.completeness == "incomplete"
    assert "incident_segment_sha256_mismatch" in {
        issue.code for issue in contradictory_reader.issues
    }


def test_incomplete_incident_manifest_fails_closed(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    manifest = session / "capture.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "blackboxrs.incident_capture.v1",
                "session_id": "sess_native",
            }
        ),
        encoding="utf-8",
    )

    reader = NativeCaptureReader(manifest)
    list(reader)

    assert reader.quality.completeness == "incomplete"
    reasons = set(reader.quality.incomplete_reasons)
    assert "incident_history_complete_invalid" in reasons
    assert "incident_post_window_elapsed_invalid" in reasons
    assert "incident_links_complete_invalid" in reasons
    assert "incident_segments_invalid" in reasons


def test_contradictory_segment_identity_fails_closed(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    sidecar_path = next((session / "segments").glob("*.json"))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar.update(
        {
            "session_id": "wrong-session",
            "path": "wrong.mcap",
            "segment_index": 999,
            "file_bytes": 1,
        }
    )
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    reader = NativeCaptureReader(session)
    assert len(list(reader)) == 2
    assert reader.quality.completeness == "incomplete"
    reasons = set(reader.quality.incomplete_reasons)
    assert "segment_session_id_mismatch" in reasons
    assert "segment_path_mismatch" in reasons
    assert "segment_index_mismatch" in reasons
    assert "segment_size_mismatch" in reasons


def test_invalid_final_quality_fields_fail_closed(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    quality_path = session / "capture_quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality.update(
        {
            "clean": "yes",
            "durable": "2",
            "storage_errors": "0",
            "clock_anomalies": "0",
            "retention_evicted_events": "0",
            "drop_breakdown": "none",
        }
    )
    quality_path.write_text(json.dumps(quality), encoding="utf-8")

    reader = NativeCaptureReader(session)
    list(reader)

    assert reader.quality.completeness == "incomplete"
    reasons = set(reader.quality.incomplete_reasons)
    assert "final_quality_clean_invalid" in reasons
    assert "final_quality_durable_invalid" in reasons
    assert "final_quality_storage_errors_invalid" in reasons
    assert "final_quality_drop_breakdown_invalid" in reasons


def test_best_effort_delivery_and_rmw_loss_degrade_quality(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    quality_path = session / "capture_quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality.update({"best_effort_topics": 1, "rmw_messages_lost": 3})
    quality_path.write_text(json.dumps(quality), encoding="utf-8")

    reader = NativeCaptureReader(session)
    list(reader)

    assert reader.quality.completeness == "incomplete"
    assert "best_effort_delivery_unverified" in reader.quality.incomplete_reasons
    assert "rmw_reported_message_loss" in reader.quality.incomplete_reasons


def test_session_requires_final_quality_and_flags_partial_segment(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    (session / "capture_quality.json").unlink()
    partial = session / "segments" / "0000000000000001.partial.mcap"
    partial.write_bytes(b"incomplete")

    reader = NativeCaptureReader(session)
    list(reader)

    assert reader.quality.completeness == "incomplete"
    reasons = set(reader.quality.incomplete_reasons)
    assert "final_capture_quality_missing" in reasons
    assert "partial_segment_present" in reasons


def test_partial_segment_recovers_complete_record_prefix(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    segment = next((session / "segments").glob("*.mcap"))
    partial = segment.with_name(segment.stem + ".partial.mcap")
    segment.rename(partial)
    partial.write_bytes(partial.read_bytes()[:-64])
    (session / "capture_quality.json").unlink()

    reader = NativeCaptureReader(session)
    events = list(reader)

    assert [event.sequence for event in events] == [100, 101]
    assert reader.quality.completeness == "incomplete"
    reasons = set(reader.quality.incomplete_reasons)
    assert "partial_segment_present" in reasons
    assert "truncated_or_invalid_segment" in reasons


def test_retention_outside_requested_window_degrades_quality(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    quality_path = session / "capture_quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality.update({"retention_evicted_events": 10, "retention_evicted_segments": 1})
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    reader = NativeCaptureReader(session)
    retained_start = datetime(2026, 8, 8, tzinfo=timezone.utc)
    start = retained_start - timedelta(seconds=1)
    end = retained_start

    list(reader.iter_blackbox_events(start=start, end=end))

    assert reader.quality.completeness == "incomplete"
    assert "requested_window_precedes_retained_capture" in reader.quality.incomplete_reasons


def test_requested_window_before_session_start_degrades_without_eviction(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    reader = NativeCaptureReader(session)
    retained_start = datetime(2026, 8, 8, tzinfo=timezone.utc)

    list(
        reader.iter_blackbox_events(
            start=retained_start - timedelta(seconds=1),
            end=retained_start,
        )
    )

    assert reader.quality.completeness == "incomplete"
    assert "requested_window_precedes_capture_start" in reader.quality.incomplete_reasons


def test_empty_window_does_not_export_unreferenced_raw_segments(tmp_path: Path):
    session, _ = _write_session(tmp_path)
    reader = NativeCaptureReader(session)
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)

    assert list(reader.iter_blackbox_events(start=future, end=future)) == []
    portable = reader.portable_files()

    assert not any(source.suffix == ".mcap" for source, _ in portable)
    assert {relative.as_posix() for _, relative in portable} == {
        "session.json",
        "capture_quality.json",
    }


def test_optional_mcap_dependency_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    capture = tmp_path / "capture.mcap"
    capture.write_bytes(b"")
    real_import = native_module.importlib.import_module

    def fail_mcap(name: str):
        if name.startswith("mcap"):
            raise ImportError("not installed")
        return real_import(name)

    monkeypatch.setattr(native_module.importlib, "import_module", fail_mcap)
    reader = NativeCaptureReader(capture)
    with pytest.raises(NativeCaptureDependencyError, match=r"blackboxrs\[replay\]"):
        list(reader)


def test_capture_config_defaults_to_python_and_loads_cpp(tmp_path: Path):
    assert BlackBoxConfig.default().capture.backend == "python"
    assert CaptureConfig().native_output_dir == "~/.blackboxrs/native"

    path = tmp_path / "config.yaml"
    path.write_text(
        "capture:\n"
        "  backend: cpp\n"
        "  topics: [/imu/data]\n"
        "  native_session_path: /captures/session_1\n",
        encoding="utf-8",
    )
    loaded = BlackBoxConfig.load(path, strict=True)
    assert loaded.capture.backend == "cpp"
    assert loaded.capture.topics == ["/imu/data"]
    assert loaded.capture.native_session_path == "/captures/session_1"

    with pytest.raises(ConfigError):
        CaptureConfig(backend="rust")
