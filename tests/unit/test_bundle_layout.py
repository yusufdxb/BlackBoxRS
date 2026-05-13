"""BundleWriter / BundleReader layout tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.incident.bundle import BundleReader, BundleWriter
from blackboxrs.incident.models import (
    ConfigSignature,
    FailureFingerprint,
    Incident,
    TimelineEvent,
    VersionSignature,
)


_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
_HASH = "0" * 64


def _minimal_inputs(bundle: Path) -> Incident:
    return Incident(
        incident_id="inc_2026-05-07T12-00-00_0badc0de",
        created_at=_NOW,
        window_start=_NOW,
        window_end=_NOW,
        session_id="s",
        title="t",
        bundle_path=str(bundle),
    )


def test_writer_creates_required_directories(tmp_path: Path):
    BundleWriter(tmp_path / "inc")
    assert (tmp_path / "inc" / "evidence").is_dir()
    assert (tmp_path / "inc" / "signatures").is_dir()
    assert (tmp_path / "inc" / "attachments").is_dir()


def test_writer_full_layout_then_reader_roundtrip(tmp_path: Path):
    bundle = tmp_path / "inc"
    w = BundleWriter(bundle)
    inc = _minimal_inputs(bundle)
    w.write_incident(inc)
    w.write_events_jsonl([
        BlackBoxEvent(
            timestamp=_NOW,
            source="system_monitor",
            event_type="system.cpu",
            data={"metric": "cpu_percent", "value": 1.0, "unit": "%"},
        ),
    ])
    w.write_triggers([])
    w.write_snapshots([])
    w.write_signatures(
        ConfigSignature(t=_NOW, hash=_HASH, payload={}),
        VersionSignature(t=_NOW, hash=_HASH, payload={}),
    )
    w.write_timeline([
        TimelineEvent(
            t=_NOW,
            kind="raw",
            subsystem="system",
            summary="s",
            confidence=1.0,
            evidence_ref="events.jsonl#L1",
        ),
    ])
    w.write_fingerprint(FailureFingerprint(
        fingerprint_id="fpr_" + "a" * 16,
        payload={},
    ))
    w.write_report("# stub\n")

    assert w.required_files_missing() == []

    r = BundleReader(bundle)
    loaded = r.load_incident()
    assert loaded.incident_id == inc.incident_id
    assert len(list(r.iter_events())) == 1
    assert r.load_triggers() == []
    assert r.load_snapshots() == []
    assert r.load_timeline()[0].evidence_ref == "events.jsonl#L1"
    cfg, ver = r.load_signatures()
    assert cfg.hash == _HASH
    assert ver.hash == _HASH
    assert r.load_fingerprint() is not None


def test_reader_rejects_missing_required(tmp_path: Path):
    bundle = tmp_path / "inc"
    BundleWriter(bundle)  # creates dirs but no files
    with pytest.raises(ValueError):
        BundleReader(bundle)

    # strict=False allows partial bundles for the in-build phase.
    r = BundleReader(bundle, strict=False)
    assert r.load_fingerprint() is None
    assert r.load_triggers() == []
