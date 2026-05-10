"""Failure fingerprint computation (algorithm v1).

Deterministic. Two bundles whose triggers fire on the same detector
classes against the same subsystems and topologies collide; perturb any
of those and the id changes. See ``ARCHITECTURE_PIVOT.md`` §4.D.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import DetectorTrigger, FailureFingerprint, SystemSnapshot


_FLOAT_PRECISION = 1


def _normalize(value: Any) -> Any:
    """Round floats and unwrap simple containers for stable hashing."""
    if isinstance(value, float):
        return round(value, _FLOAT_PRECISION)
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_normalize(v) for v in value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _topology_signature(snapshots: Iterable[SystemSnapshot]) -> str:
    """8-char sha256 of the sorted (topic, qos_summary) set across snapshots.

    A *set*, not a sequence, because we want order independence; two
    runs with the same set of topics + QoS classes should match even
    if they discovered them in a different order.

    ``qos_summary`` is normalised to an empty string when unknown so
    ``sorted()`` does not have to compare ``None`` against a string
    (Python 3 raises on mixed-type ordering).
    """
    seen: set[tuple[str, str]] = set()
    for snap in snapshots:
        for topic in snap.topics:
            seen.add((topic.name, topic.qos_summary or ""))
    payload = json.dumps(sorted(list(t) for t in seen), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _group_by(items: list[DetectorTrigger], key: str) -> dict[str, list[DetectorTrigger]]:
    out: dict[str, list[DetectorTrigger]] = {}
    for item in items:
        out.setdefault(getattr(item, key), []).append(item)
    return out


def compute(
    triggers: list[DetectorTrigger],
    snapshots: list[SystemSnapshot] | None = None,
) -> FailureFingerprint:
    """Compute a :class:`FailureFingerprint` from triggers + snapshots."""
    snapshots = snapshots or []

    detector_classes = sorted({t.detector_class for t in triggers})
    subsystems = sorted({t.subsystem for t in triggers})

    by_class = _group_by(triggers, "detector_class")
    signature_fields: dict[str, list[Any]] = {}
    for cls, group in by_class.items():
        # Each detector declares which fields are stable enough to be
        # part of the fingerprint. We collect them across all triggers
        # of that class, normalize, and sort.
        rows: list[tuple[str, Any]] = []
        for trig in group:
            for field in trig.signature_fields:
                if field in trig.data:
                    rows.append((field, _normalize(trig.data[field])))
        rows.sort(key=lambda r: (r[0], json.dumps(r[1], sort_keys=True, default=str)))
        signature_fields[cls] = rows

    topology = _topology_signature(snapshots) if snapshots else None

    payload = {
        "detector_classes": detector_classes,
        "subsystems": subsystems,
        "signature_fields": signature_fields,
        "topology_signature": topology,
    }

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    fp_id = "fpr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return FailureFingerprint(
        fingerprint_id=fp_id,
        algorithm_version="v1",
        payload=payload,
    )
