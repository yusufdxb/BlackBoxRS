"""Pydantic domain models for incident intelligence.

Schemas correspond 1:1 with ``ARCHITECTURE_PIVOT.md`` §1. Every model is
designed to JSON round-trip without loss; bundles on disk are made of
these objects serialized to ``incident.json``, ``triggers.json``,
``timeline.json``, ``snapshots.json``, ``signatures/*.json``, and
``fingerprint.json``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "1.0"

EventKind = Literal["raw", "derived", "trigger", "snapshot", "external"]
Subsystem = Literal["ros", "system", "gpu", "anomaly", "recorder", "config", "external"]
Severity = Literal["info", "warning", "error", "critical"]
CausalityHint = Literal["precursor", "consequence", "independent"]


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_ID = re.compile(r"^fpr_[0-9a-f]{16}$")
_TRIGGER_ID = re.compile(r"^trg_[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# DetectorTrigger
# ---------------------------------------------------------------------------


class DetectorTrigger(BaseModel):
    """A typed promotion of an ``anomaly_engine`` event.

    See ``ARCHITECTURE_PIVOT.md`` §1.4. Constructed from raw
    ``BlackBoxEvent`` instances via :meth:`from_event`; the deterministic
    ``trigger_id`` makes triggers stable across rebuilds.
    """

    model_config = ConfigDict(extra="forbid")

    trigger_id: str = Field(..., description="Deterministic trg_<sha8>.")
    detector: str
    detector_class: str
    t: datetime
    subsystem: Subsystem
    subject: str
    severity: Severity = "warning"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    signature_fields: list[str] = Field(default_factory=list)
    source_event_ref: str | None = None

    @field_validator("trigger_id")
    @classmethod
    def _validate_trigger_id(cls, v: str) -> str:
        if not _TRIGGER_ID.match(v):
            raise ValueError(f"trigger_id must match trg_<8 hex>, got {v!r}")
        return v

    @staticmethod
    def make_id(detector: str, t: datetime, subject: str) -> str:
        """Stable id from (detector, timestamp, subject)."""
        payload = f"{detector}|{t.isoformat()}|{subject}".encode("utf-8")
        return "trg_" + hashlib.sha256(payload).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TopicSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    msg_type: str | None = None
    pub_count: int = 0
    sub_count: int = 0
    last_freq_hz: float | None = None
    qos_summary: str | None = None


class ProcessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int
    name: str
    cpu_percent: float | None = None
    mem_percent: float | None = None


class GPUSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    util_percent: float | None = None
    mem_percent: float | None = None
    temp_c: float | None = None
    power_w: float | None = None


class SystemSnapshot(BaseModel):
    """Periodic system+graph snapshot used for incident reconstruction.

    See ``ARCHITECTURE_PIVOT.md`` §1.5.
    """

    model_config = ConfigDict(extra="forbid")

    t: datetime
    host: str
    cpu_percent: float | None = None
    mem_percent: float | None = None
    disk_percent: float | None = None
    disk_io: dict[str, Any] | None = None
    thermal_zones: dict[str, float] | None = None
    gpu: GPUSnapshot | None = None
    topics: list[TopicSnapshot] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    processes: list[ProcessSnapshot] | None = None


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TimelineEvent(BaseModel):
    """A single point on the incident timeline.

    Distinct from raw ``BlackBoxEvent`` because it carries derivation
    metadata (kind, confidence) and an evidence reference that *always*
    resolves inside the bundle.
    """

    model_config = ConfigDict(extra="forbid")

    t: datetime
    kind: EventKind
    subsystem: Subsystem
    summary: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_ref: str
    data: dict[str, Any] = Field(default_factory=dict)
    correlated_event_refs: list[str] = Field(default_factory=list)
    causality_hint: CausalityHint | None = None


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


class ConfigSignature(BaseModel):
    """Reproducibility hash of *how* the robot was configured.

    Required for bundle reproducibility. See ``ARCHITECTURE_PIVOT.md``
    §1.6.
    """

    model_config = ConfigDict(extra="forbid")

    t: datetime
    hash: str = Field(..., description="sha256 hex of canonical payload.")
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _HEX_64.match(v):
            raise ValueError("hash must be 64-char lowercase sha256 hex")
        return v


class VersionSignature(BaseModel):
    """Reproducibility hash of *what* software was running.

    See ``ARCHITECTURE_PIVOT.md`` §1.7.
    """

    model_config = ConfigDict(extra="forbid")

    t: datetime
    hash: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _HEX_64.match(v):
            raise ValueError("hash must be 64-char lowercase sha256 hex")
        return v


# ---------------------------------------------------------------------------
# Failure fingerprint
# ---------------------------------------------------------------------------


class FailureFingerprint(BaseModel):
    """Stable identifier for "this kind of failure."

    See ``ARCHITECTURE_PIVOT.md`` §1.8.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint_id: str
    algorithm_version: str = "v1"
    payload: dict[str, Any] = Field(default_factory=dict)
    cluster_id: str | None = None
    human_label: str | None = None

    @field_validator("fingerprint_id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _FINGERPRINT_ID.match(v):
            raise ValueError("fingerprint_id must match fpr_<16 hex>")
        return v


# ---------------------------------------------------------------------------
# Likely cause
# ---------------------------------------------------------------------------


class PrecursorRef(BaseModel):
    """A single precursor row in a likely-cause hypothesis chain.

    Each entry points back into the bundle (``evidence_ref``) and carries
    the relative gap from the trigger so the report can render
    "5.2 s before the trigger, ...". The renderer never invents these:
    the ranker fills them only when it actually used the row to score.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    t: datetime
    kind: str            # raw / derived / trigger / snapshot / external
    subsystem: str
    summary: str
    gap_sec: float       # positive = before the trigger; 0 = at trigger
    relevance: float     # 0..1 contribution this row made to the score


class RecurrenceContext(BaseModel):
    """How often this fingerprint has been seen on this host before.

    Filled in only when a recurrence lookup actually found prior
    bundles. Absence (None on the hypothesis) means we did not look or
    we found nothing, NOT that we proved this is novel.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint_id: str
    prior_count: int = Field(..., ge=0)
    prior_incident_ids: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None


class LikelyCauseHypothesis(BaseModel):
    """A ranked hypothesis with evidence references.

    The renderer never invents content here; the cause module fills it
    based on triggers, derived timeline events, snapshots, config /
    version diffs, and (when available) recurrence context. Confidence
    < 0.5 must carry a caveat.
    """

    model_config = ConfigDict(extra="forbid")

    cause: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    caveat: str | None = None

    # New fields (optional, additive). Older bundles deserialise without
    # them via Pydantic defaults; the renderer treats absence as "no
    # extra reasoning available."
    score: float | None = Field(default=None, ge=0.0)
    subsystem: str | None = None
    reasoning: list[str] = Field(default_factory=list)
    precursor_chain: list[PrecursorRef] = Field(default_factory=list)
    recurrence: RecurrenceContext | None = None


# ---------------------------------------------------------------------------
# Incident (top-level)
# ---------------------------------------------------------------------------


class Incident(BaseModel):
    """Top-level identity for a single failure or investigation.

    See ``ARCHITECTURE_PIVOT.md`` §1.1. ``incident.json`` in a bundle is
    the canonical serialisation; ``report.md`` is the human view.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    schema_version: str = SCHEMA_VERSION
    created_at: datetime
    window_start: datetime
    window_end: datetime
    session_id: str
    host: str | None = None
    # In onboard mode `host` is the robot; both observer_host and
    # observed_host are None. In observer mode `host` mirrors
    # observer_host for backwards compatibility (older readers fall back
    # to it), while observed_host carries the operator-supplied label
    # for the robot the bundle is *about*.
    observer_host: str | None = None
    observed_host: str | None = None
    severity: Severity = "warning"
    title: str = Field(..., max_length=200)
    summary: str = ""
    bundle_path: str

    tags: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    fingerprint: FailureFingerprint | None = None
    likely_causes: list[LikelyCauseHypothesis] = Field(default_factory=list)
    linked_prevention_rules: list[str] = Field(default_factory=list)
    notes: str | None = None


# ---------------------------------------------------------------------------
# EvidenceBundle (path holder; layout enforced by bundle.py)
# ---------------------------------------------------------------------------


class EvidenceBundle(BaseModel):
    """Reference to an on-disk bundle directory.

    The bundle directory is the source of truth. This model is a tiny
    handle for code that wants to pass a bundle around without committing
    to a particular reader/writer; layout invariants live in ``bundle.py``.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    bundle_path: str
