"""Fingerprint determinism + collision tests."""

from __future__ import annotations

from datetime import datetime, timezone

from blackboxrs.incident.fingerprint import compute
from blackboxrs.incident.models import (
    DetectorTrigger,
    SystemSnapshot,
    TopicSnapshot,
)


_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _trig(detector_class: str, subject: str, **data) -> DetectorTrigger:
    return DetectorTrigger(
        trigger_id=DetectorTrigger.make_id(detector_class, _NOW, subject),
        detector=detector_class.rsplit(".", 1)[-1],
        detector_class=detector_class,
        t=_NOW,
        subsystem="anomaly",
        subject=subject,
        message="m",
        data=data,
        signature_fields=list(data.keys()),
    )


def test_fingerprint_deterministic():
    triggers = [
        _trig("pkg.QoSMismatchDetector", "/scan", reliability="BEST_EFFORT"),
        _trig("pkg.DeadTopicDetector", "/scan"),
    ]
    a = compute(triggers, [])
    b = compute(triggers, [])
    assert a.fingerprint_id == b.fingerprint_id


def test_fingerprint_collides_on_same_surface():
    a = compute([_trig("pkg.DeadTopicDetector", "/scan")], [])
    b = compute([_trig("pkg.DeadTopicDetector", "/scan")], [])
    assert a.fingerprint_id == b.fingerprint_id


def test_fingerprint_changes_on_perturbation():
    base = _trig("pkg.QoSMismatchDetector", "/scan", reliability="BEST_EFFORT")
    perturb = _trig("pkg.QoSMismatchDetector", "/scan", reliability="RELIABLE")
    a = compute([base], [])
    b = compute([perturb], [])
    assert a.fingerprint_id != b.fingerprint_id


def test_topology_signature_set_invariant():
    snaps_a = [
        SystemSnapshot(
            t=_NOW, host="h",
            topics=[
                TopicSnapshot(name="/a", qos_summary="reliable"),
                TopicSnapshot(name="/b", qos_summary="best_effort"),
            ],
        ),
    ]
    snaps_b = [
        SystemSnapshot(
            t=_NOW, host="h",
            topics=[
                TopicSnapshot(name="/b", qos_summary="best_effort"),
                TopicSnapshot(name="/a", qos_summary="reliable"),
            ],
        ),
    ]
    a = compute([_trig("pkg.X", "/a")], snaps_a)
    b = compute([_trig("pkg.X", "/a")], snaps_b)
    assert a.fingerprint_id == b.fingerprint_id


def test_fingerprint_id_format():
    fp = compute([_trig("pkg.X", "/a")], [])
    assert fp.fingerprint_id.startswith("fpr_")
    assert len(fp.fingerprint_id) == len("fpr_") + 16
