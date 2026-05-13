"""Tests for incident.diff: structured signature comparison."""

from __future__ import annotations

from datetime import datetime, timezone

from blackboxrs.incident.diff import IncidentDiff, compute
from blackboxrs.incident.models import ConfigSignature, VersionSignature


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _cfg(hash_: str, payload: dict) -> ConfigSignature:
    return ConfigSignature(t=_NOW, hash=hash_, payload=payload)


def _ver(hash_: str, payload: dict) -> VersionSignature:
    return VersionSignature(t=_NOW, hash=hash_, payload=payload)


def test_diff_identical_when_hashes_match():
    cfg = _cfg(_HASH_A, {"ros_distro": "humble"})
    ver = _ver(_HASH_A, {"python": {"version": "3.10.12"}})
    out = compute(cfg, ver, cfg, ver)
    assert out.config.identical is True
    assert out.versions.identical is True


def test_diff_no_baseline_marks_only_in_current():
    cfg = _cfg(_HASH_A, {"ros_distro": "humble", "ros_domain_id": 0})
    ver = _ver(_HASH_A, {"blackboxrs_version": "0.4.0"})
    out = compute(None, None, cfg, ver)
    assert out.config.baseline_hash is None
    assert out.config.identical is False
    assert {c.key for c in out.config.only_in_current} == {
        "ros_distro", "ros_domain_id"
    }
    assert {c.key for c in out.versions.only_in_current} == {
        "blackboxrs_version"
    }


def test_diff_changed_field():
    prev = _cfg(_HASH_A, {"ros_distro": "humble", "ros_domain_id": 0})
    curr = _cfg(_HASH_B, {"ros_distro": "iron",   "ros_domain_id": 0})
    out = compute(prev, _ver(_HASH_A, {}), curr, _ver(_HASH_A, {}))
    assert out.config.identical is False
    assert len(out.config.changed) == 1
    ch = out.config.changed[0]
    assert ch.key == "ros_distro"
    assert ch.before == "humble"
    assert ch.after == "iron"


def test_diff_added_and_removed_keys():
    prev = _cfg(_HASH_A, {"a": 1, "b": 2})
    curr = _cfg(_HASH_B, {"a": 1, "c": 3})
    out = compute(prev, _ver(_HASH_A, {}), curr, _ver(_HASH_A, {}))
    assert {c.key for c in out.config.only_in_current} == {"c"}
    assert {c.key for c in out.config.only_in_baseline} == {"b"}


def test_diff_nested_payload_is_flattened():
    prev = _cfg(_HASH_A, {"os": {"name": "Ubuntu", "version": "22.04"}})
    curr = _cfg(_HASH_B, {"os": {"name": "Ubuntu", "version": "24.04"}})
    out = compute(prev, _ver(_HASH_A, {}), curr, _ver(_HASH_A, {}))
    assert len(out.config.changed) == 1
    assert out.config.changed[0].key == "os.version"
    assert out.config.changed[0].before == "22.04"
    assert out.config.changed[0].after == "24.04"


def test_diff_round_trip_through_json():
    prev = _cfg(_HASH_A, {"a": 1})
    curr = _cfg(_HASH_B, {"a": 2})
    out = compute(prev, _ver(_HASH_A, {}), curr, _ver(_HASH_A, {}))
    again = IncidentDiff.model_validate_json(out.model_dump_json())
    assert again == out
