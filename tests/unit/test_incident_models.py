"""Unit tests for incident.models pydantic schemas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from blackboxrs.incident.models import (
    ConfigSignature,
    DetectorTrigger,
    FailureFingerprint,
    Incident,
    LikelyCauseHypothesis,
    SystemSnapshot,
    TimelineEvent,
    VersionSignature,
)


_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
_HASH64 = "0" * 64


def test_trigger_id_is_deterministic():
    a = DetectorTrigger.make_id("DeadTopicDetector", _NOW, "/scan")
    b = DetectorTrigger.make_id("DeadTopicDetector", _NOW, "/scan")
    assert a == b
    assert a.startswith("trg_")
    assert len(a) == len("trg_") + 8


def test_trigger_round_trip():
    trig = DetectorTrigger(
        trigger_id=DetectorTrigger.make_id("Foo", _NOW, "x"),
        detector="Foo",
        detector_class="pkg.mod.Foo",
        t=_NOW,
        subsystem="anomaly",
        subject="x",
        severity="warning",
        message="m",
        data={"k": 1},
    )
    again = DetectorTrigger.model_validate_json(trig.model_dump_json())
    assert again == trig


def test_trigger_id_validation():
    with pytest.raises(ValidationError):
        DetectorTrigger(
            trigger_id="not_a_trigger_id",
            detector="d",
            detector_class="x.D",
            t=_NOW,
            subsystem="anomaly",
            subject="s",
            message="m",
        )


def test_timeline_event_confidence_bounds():
    base = dict(
        t=_NOW,
        kind="raw",
        subsystem="ros",
        summary="s",
        evidence_ref="events.jsonl#L1",
    )
    TimelineEvent(**base, confidence=1.0)
    with pytest.raises(ValidationError):
        TimelineEvent(**base, confidence=1.5)
    with pytest.raises(ValidationError):
        TimelineEvent(**base, confidence=-0.1)


def test_signature_hash_validator():
    with pytest.raises(ValidationError):
        ConfigSignature(t=_NOW, hash="too-short", payload={})
    with pytest.raises(ValidationError):
        VersionSignature(t=_NOW, hash="X" * 64, payload={})

    cfg = ConfigSignature(t=_NOW, hash=_HASH64, payload={"a": 1})
    ver = VersionSignature(t=_NOW, hash=_HASH64, payload={"b": 2})
    assert cfg.hash == _HASH64
    assert ver.hash == _HASH64


def test_fingerprint_id_format():
    fp = FailureFingerprint(fingerprint_id="fpr_" + "0" * 16, payload={})
    assert fp.algorithm_version == "v1"
    with pytest.raises(ValidationError):
        FailureFingerprint(fingerprint_id="bad")


def test_likely_cause_confidence_bounds():
    with pytest.raises(ValidationError):
        LikelyCauseHypothesis(cause="x", confidence=2.0)
    h = LikelyCauseHypothesis(cause="x", confidence=0.5, evidence_refs=["a"])
    assert h.evidence_refs == ["a"]


def test_incident_roundtrip():
    inc = Incident(
        incident_id="inc_2026-05-07T14-22-13_a3f2b00d",
        created_at=_NOW,
        window_start=_NOW,
        window_end=_NOW,
        session_id="sess",
        title="t",
        bundle_path="/tmp/x",
    )
    again = Incident.model_validate_json(inc.model_dump_json())
    assert again == inc


def test_system_snapshot_minimal():
    s = SystemSnapshot(t=_NOW, host="h")
    assert s.topics == []
    assert s.nodes == []
    assert s.gpu is None
