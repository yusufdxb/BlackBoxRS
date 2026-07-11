"""Recurrence module tests.

The :class:`FingerprintIndex` walks a directory of bundles to answer
"have we seen this fingerprint before?". These tests build small,
synthetic bundle directories on disk and exercise the lookup contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.incident.recurrence import (
    FingerprintIndex,
    build_recurrence_lookup,
)


_T0 = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _write_bundle(
    root: Path,
    incident_id: str,
    fp_id: str,
    *,
    host: str = "dev-workstation",
    window_start: datetime,
    window_end: datetime,
    bundle_path: str | None = None,
) -> Path:
    """Write a *minimum-viable* bundle: just incident.json + fingerprint.json."""
    bundle = root / incident_id
    bundle.mkdir(parents=True)
    inc = {
        "incident_id": incident_id,
        "schema_version": "1.0",
        "created_at": window_end.isoformat().replace("+00:00", "Z"),
        "window_start": window_start.isoformat().replace("+00:00", "Z"),
        "window_end": window_end.isoformat().replace("+00:00", "Z"),
        "session_id": "s",
        "severity": "warning",
        "title": "t",
        "summary": "",
        "host": host,
        "tags": [],
        "sessions": [],
        "triggers": [],
        "fingerprint": None,
        "likely_causes": [],
        "linked_prevention_rules": [],
        "notes": None,
        "bundle_path": bundle_path or str(bundle),
    }
    fp = {
        "fingerprint_id": fp_id,
        "algorithm_version": "v1",
        "payload": {},
        "cluster_id": None,
        "human_label": None,
    }
    (bundle / "incident.json").write_text(json.dumps(inc), encoding="utf-8")
    (bundle / "fingerprint.json").write_text(json.dumps(fp), encoding="utf-8")
    return bundle


_FP_A = "fpr_" + "a" * 16
_FP_B = "fpr_" + "b" * 16


# ---------------------------------------------------------------------------
# Empty / smoke
# ---------------------------------------------------------------------------


def test_empty_directory_returns_none(tmp_path: Path):
    idx = FingerprintIndex(tmp_path / "missing")
    assert idx.lookup(_FP_A) is None


def test_no_priors_returns_none(tmp_path: Path):
    _write_bundle(
        tmp_path, "inc_2026-05-07T12-00-00_aaaa", _FP_A,
        window_start=_T0 - timedelta(hours=2),
        window_end=_T0 - timedelta(hours=1),
    )
    idx = FingerprintIndex(tmp_path)
    assert idx.lookup(_FP_B) is None


# ---------------------------------------------------------------------------
# Counting + ID surfacing
# ---------------------------------------------------------------------------


def test_lookup_counts_priors_and_surfaces_ids(tmp_path: Path):
    for i in range(3):
        _write_bundle(
            tmp_path, f"inc_2026-05-07T12-{i:02d}-00_aaaa{i}", _FP_A,
            window_start=_T0 - timedelta(hours=10 - i),
            window_end=_T0 - timedelta(hours=9 - i),
        )
    idx = FingerprintIndex(tmp_path)
    ctx = idx.lookup(_FP_A)
    assert ctx is not None
    assert ctx.prior_count == 3
    assert len(ctx.prior_incident_ids) == 3
    assert ctx.last_seen_at is not None


def test_lookup_caps_prior_ids_at_five(tmp_path: Path):
    """When more than five priors exist, only the most recent five
    should be surfaced. Total count must still be accurate."""
    for i in range(8):
        _write_bundle(
            tmp_path, f"inc_2026-05-07T12-{i:02d}-00_aaaa{i}", _FP_A,
            window_start=_T0 - timedelta(hours=20 - i),
            window_end=_T0 - timedelta(hours=19 - i),
        )
    ctx = FingerprintIndex(tmp_path).lookup(_FP_A)
    assert ctx is not None
    assert ctx.prior_count == 8
    assert len(ctx.prior_incident_ids) == 5


# ---------------------------------------------------------------------------
# Filters: host, window_end, exclude_id
# ---------------------------------------------------------------------------


def test_other_host_priors_excluded(tmp_path: Path):
    _write_bundle(
        tmp_path, "inc_2026-05-07T12-00-00_aaaa", _FP_A,
        host="otherhost",
        window_start=_T0 - timedelta(hours=2),
        window_end=_T0 - timedelta(hours=1),
    )
    idx = FingerprintIndex(tmp_path, host="dev-workstation")
    assert idx.lookup(_FP_A) is None


def test_future_or_concurrent_bundles_excluded(tmp_path: Path):
    _write_bundle(
        tmp_path, "inc_2026-05-08T13-00-00_aaaa", _FP_A,
        window_start=_T0 + timedelta(hours=1),
        window_end=_T0 + timedelta(hours=2),
    )
    idx = FingerprintIndex(tmp_path, before=_T0)
    assert idx.lookup(_FP_A) is None


def test_self_excluded_by_id(tmp_path: Path):
    _write_bundle(
        tmp_path, "inc_self", _FP_A,
        window_start=_T0 - timedelta(hours=2),
        window_end=_T0 - timedelta(hours=1),
    )
    idx = FingerprintIndex(tmp_path, exclude_id="inc_self")
    assert idx.lookup(_FP_A) is None


# ---------------------------------------------------------------------------
# Resilience to broken bundles
# ---------------------------------------------------------------------------


def test_unreadable_bundle_does_not_break_lookup(tmp_path: Path):
    # One valid prior.
    _write_bundle(
        tmp_path, "inc_good", _FP_A,
        window_start=_T0 - timedelta(hours=2),
        window_end=_T0 - timedelta(hours=1),
    )
    # One garbage directory.
    bad = tmp_path / "inc_bad"
    bad.mkdir()
    (bad / "incident.json").write_text("not json", encoding="utf-8")
    (bad / "fingerprint.json").write_text("also not json", encoding="utf-8")
    # And one with no fingerprint at all.
    no_fp = tmp_path / "inc_no_fp"
    no_fp.mkdir()
    (no_fp / "incident.json").write_text(
        '{"incident_id":"inc_no_fp","schema_version":"1.0",'
        '"created_at":"2026-05-07T12:00:00Z","window_start":"2026-05-07T11:00:00Z",'
        '"window_end":"2026-05-07T11:30:00Z","session_id":"s","severity":"warning",'
        '"title":"t","summary":"","host":"dev-workstation","tags":[],"sessions":[],'
        '"triggers":[],"fingerprint":null,"likely_causes":[],'
        '"linked_prevention_rules":[],"notes":null,"bundle_path":""}',
        encoding="utf-8",
    )

    ctx = FingerprintIndex(tmp_path).lookup(_FP_A)
    assert ctx is not None
    assert ctx.prior_count == 1


# ---------------------------------------------------------------------------
# build_recurrence_lookup factory
# ---------------------------------------------------------------------------


def test_build_recurrence_lookup_returns_callable(tmp_path: Path):
    _write_bundle(
        tmp_path, "inc_x", _FP_A,
        window_start=_T0 - timedelta(hours=2),
        window_end=_T0 - timedelta(hours=1),
    )
    fn = build_recurrence_lookup(
        tmp_path, host="dev-workstation", before=_T0, exclude_id="inc_self",
    )
    ctx = fn(_FP_A)
    assert ctx is not None
    assert ctx.prior_count == 1


def test_lookup_empty_string_returns_none(tmp_path: Path):
    idx = FingerprintIndex(tmp_path)
    assert idx.lookup("") is None
