"""End-to-end test for IncidentBuilder against a synthetic JSONL fixture."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident, render_report
from blackboxrs.incident.bundle import BundleReader


_START = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _ev(t, source, event_type, data, severity="info", metadata=None):
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "sess_test"},
    }


def _seed_jsonl(path: Path) -> None:
    events = [
        _ev(_START, "ros_monitor", "ros.frequency",
            {"topic": "/scan", "frequency_hz": 10.0}),
        _ev(_START + timedelta(seconds=1), "system_monitor", "system.cpu",
            {"metric": "cpu_percent", "value": 35.0, "unit": "%"}),
        _ev(_START + timedelta(seconds=5), "anomaly_engine", "anomaly.dead_topic",
            {
                "detector": "DeadTopicDetector",
                "topic": "/scan",
                "metric": "/scan",
                "value": 0.0,
                "threshold": 5.0,
                "message": "Topic /scan silent for 5s.",
            },
            severity="error",
            metadata={
                "session_id": "sess_test",
                "detector_class": "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector",
                "signature_fields": ["topic"],
            }),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")


def test_builder_produces_valid_bundle(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _seed_jsonl(log_dir / "blackboxrs_20260507_120000_000000.jsonl")

    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)

    bundle = build_incident(
        _START,
        _START + timedelta(seconds=10),
        config=cfg,
        incidents_dir=tmp_path / "incidents",
    )

    reader = BundleReader(bundle)  # strict=True by default
    incident = reader.load_incident()

    assert incident.session_id == "sess_test"
    assert incident.severity in ("error", "critical")

    triggers = reader.load_triggers()
    assert len(triggers) == 1
    assert triggers[0].subject == "/scan"
    assert triggers[0].source_event_ref == "events.jsonl#L3"

    timeline = reader.load_timeline()
    assert any(e.kind == "trigger" for e in timeline)
    assert all(0.0 <= e.confidence <= 1.0 for e in timeline)

    fp = reader.load_fingerprint()
    assert fp is not None
    assert fp.fingerprint_id.startswith("fpr_")

    cfg_sig, ver_sig = reader.load_signatures()
    assert len(cfg_sig.hash) == 64
    assert len(ver_sig.hash) == 64

    text = render_report(bundle)
    assert "## Timeline" in text
    assert "## Triggers" in text
    assert "## Likely causes" in text
    assert "## Fingerprint" in text
    # Top hypothesis is high-confidence so a YAML block should appear.
    assert "```yaml" in text


def test_builder_idempotent_id(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _seed_jsonl(log_dir / "blackboxrs_20260507_120000_000000.jsonl")

    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)

    a = build_incident(_START, _START + timedelta(seconds=10),
                       config=cfg, incidents_dir=tmp_path / "out")
    b = build_incident(_START, _START + timedelta(seconds=10),
                       config=cfg, incidents_dir=tmp_path / "out")
    assert a == b  # same path → idempotent on inputs
