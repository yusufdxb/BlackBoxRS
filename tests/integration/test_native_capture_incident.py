from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blackboxrs.core.config import BlackBoxConfig, CaptureConfig
from blackboxrs.incident.api import build_incident, render_report
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.recording.native import CONTROL_SCHEMA


mcap_writer = pytest.importorskip("mcap.writer")
_START = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _write_trigger_session(root: Path) -> Path:
    session = root / "capture_native_incident"
    segments = session / "segments"
    segments.mkdir(parents=True)
    segment = segments / "0000000000000000.mcap"
    ros_base = int(_START.timestamp() * 1e9)

    trigger = {
        "schema_version": 1,
        "kind": "trigger",
        "monotonic_ns": 2_000,
        "ros_time_ns": ros_base + 5_000_000_000,
        "sequence": 11,
        "topic_id": 4,
        "flags": 8,
        "payload": {
            "code": 1,
            "severity": 2,
            "first_seen_ns": 1_000,
            "confirmed_ns": 2_000,
            "value": 5.0,
            "threshold": 3.0,
        },
    }
    control_data = json.dumps(trigger).encode()

    with segment.open("wb") as stream:
        writer = mcap_writer.Writer(
            stream,
            compression=mcap_writer.CompressionType.NONE,
            use_chunking=False,
            enable_crcs=True,
            enable_data_crcs=True,
        )
        writer.start(profile="ros2", library="blackbox_capture_cpp/test")
        ros_schema = writer.register_schema("sensor_msgs/msg/LaserScan", "ros2msg", b"")
        ros_channel = writer.register_channel(
            "/scan",
            "cdr",
            ros_schema,
            metadata={
                "blackboxrs.topic_id": "4",
                "blackboxrs.ros_type": "sensor_msgs/msg/LaserScan",
                "blackboxrs.serialization_format": "cdr",
            },
        )
        control_schema = writer.register_schema(CONTROL_SCHEMA, "jsonschema", b'{"type":"object"}')
        control_channel = writer.register_channel("/blackboxrs/events", "json", control_schema)
        writer.add_message(
            ros_channel,
            log_time=1_000,
            publish_time=ros_base,
            sequence=10,
            data=b"opaque scan",
        )
        writer.add_message(
            control_channel,
            log_time=2_000,
            publish_time=ros_base + 5_000_000_000,
            sequence=11,
            data=control_data,
        )
        writer.finish()

    sidecar = {
        "schema": "blackboxrs.capture_segment.v1",
        "session_id": "sess_native_incident",
        "segment_index": 0,
        "path": "segments/0000000000000000.mcap",
        "clean": True,
        "recovered": False,
        "first_sequence": 10,
        "last_sequence": 11,
        "received": 3,
        "admitted": 2,
        "committed": 2,
        "dropped": 1,
        "bytes_captured": len(b"opaque scan") + len(control_data),
        "bytes_dropped": 256,
        "peak_queue_utilization": 0.91,
        "storage_errors": [],
        "clock_anomalies": 0,
        "monotonic_start_ns": 1_000,
        "monotonic_end_ns": 2_000,
        "sha256": hashlib.sha256(segment.read_bytes()).hexdigest(),
    }
    segment.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    (session / "session.json").write_text(
        json.dumps(
            {
                "schema": "blackboxrs.capture_session.v1",
                "session_id": "sess_native_incident",
            }
        ),
        encoding="utf-8",
    )
    return session


def test_native_capture_flows_into_incident_with_quality_limit(tmp_path: Path):
    session = _write_trigger_session(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    config = BlackBoxConfig(
        log_dir=str(logs),
        capture=CaptureConfig(
            backend="cpp",
            native_session_path=str(session),
        ),
    )

    bundle = build_incident(
        _START,
        _START + timedelta(seconds=10),
        config=config,
        incidents_dir=tmp_path / "incidents",
    )
    reader = BundleReader(bundle)
    incident = reader.load_incident()

    assert incident.session_id == "sess_native_incident"
    assert incident.capture_quality is not None
    assert incident.capture_quality.backend == "cpp"
    assert incident.capture_quality.completeness == "incomplete"
    assert incident.capture_quality.dropped == 1
    assert incident.capture_quality.bytes_dropped == 256
    assert incident.capture_quality.peak_queue_utilization == 0.91
    assert incident.likely_causes
    triggers = reader.load_triggers()
    assert triggers[0].detector == "dead_topic"
    assert triggers[0].detector_class == "DeadTopicDetector"
    assert triggers[0].subject == "/scan"
    assert incident.likely_causes[0].confidence <= 0.69
    assert "Native capture evidence is incomplete" in (incident.likely_causes[0].caveat or "")

    events = list(reader.iter_events())
    assert len(events) == 2
    assert all("opaque scan" not in event.to_jsonl() for event in events)
    assert events[0].metadata["capture_backend"] == "cpp"
    assert events[0].data["evidence_ref"].startswith("attachments/native_capture/")
    attached_segment = (
        bundle
        / "attachments"
        / "native_capture"
        / "segments"
        / "0000000000000000.mcap"
    )
    assert attached_segment.read_bytes() == (
        session / "segments" / "0000000000000000.mcap"
    ).read_bytes()

    report = render_report(bundle)
    assert "## Capture quality" in report
    assert "**Backend**: C++" in report
    assert "**Events dropped**: 1" in report
    assert "Likely-cause confidence is limited" in report
