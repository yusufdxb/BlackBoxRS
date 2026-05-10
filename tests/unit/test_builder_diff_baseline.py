"""End-to-end test that the IncidentBuilder finds a prior bundle on the
same host and produces a diff against it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident
from blackboxrs.incident.bundle import BundleReader


_T0 = datetime(2026, 5, 8, 9, 0, 0, tzinfo=timezone.utc)


def _ev(t, source, event_type, data, severity="info", metadata=None):
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "sess_test"},
    }


def _seed(path: Path, t0: datetime) -> None:
    events = [
        _ev(t0, "ros_monitor", "ros.frequency", {"topic": "/scan", "frequency_hz": 10.0}),
        _ev(t0 + timedelta(seconds=1), "system_monitor", "system.cpu",
            {"cpu_percent": 35.0}),
        _ev(t0 + timedelta(seconds=5), "anomaly_engine", "anomaly.dead_topic",
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
                "detector_class":
                    "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector",
                "signature_fields": ["topic"],
                "target_subsystem": "ros",
            }),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e))
            fh.write("\n")


def test_builder_writes_diff_against_prior_bundle(tmp_path: Path):
    """Build two consecutive bundles. The second's diff payload should
    reference the first's signature hashes, not None.

    We cannot fully control current ConfigSignature/VersionSignature
    payloads (they reflect the host running the test), so the assertion
    is on identity / non-empty baseline hashes rather than specific
    field changes.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # First bundle.
    _seed(log_dir / "blackboxrs_20260508_090000_000000.jsonl", _T0)
    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)
    incidents = tmp_path / "incidents"
    first = build_incident(
        _T0, _T0 + timedelta(seconds=10),
        config=cfg, incidents_dir=incidents,
    )

    # Second bundle in a later, non-overlapping window.
    later = _T0 + timedelta(minutes=30)
    _seed(log_dir / "blackboxrs_20260508_093000_000000.jsonl", later)
    second = build_incident(
        later, later + timedelta(seconds=10),
        config=cfg, incidents_dir=incidents,
    )

    assert first != second  # ids differ because window_start differs
    diff = BundleReader(second).load_diff()
    assert diff is not None

    # The second bundle's diff baseline must reference the first
    # bundle's signature hashes. On the same host within seconds the
    # signatures will be identical => identical=True is acceptable, but
    # baseline hash must be set (not None).
    first_cfg, first_ver = BundleReader(first).load_signatures()
    assert diff.config.baseline_hash == first_cfg.hash
    assert diff.versions.baseline_hash == first_ver.hash


def test_builder_diff_is_orphan_when_first_bundle(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _seed(log_dir / "blackboxrs_20260508_090000_000000.jsonl", _T0)
    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)

    only = build_incident(
        _T0, _T0 + timedelta(seconds=10),
        config=cfg, incidents_dir=tmp_path / "incidents",
    )
    diff = BundleReader(only).load_diff()
    assert diff is not None
    assert diff.config.baseline_hash is None
    assert diff.versions.baseline_hash is None
