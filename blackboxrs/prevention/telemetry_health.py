"""Bounded telemetry-health contract and state machine.

This module deliberately implements one operational contract:
a named ROS 2 topic must start, remain fresh, sustain a minimum rate,
and advance its message header timestamps. It is not a temporal-policy
language and it does not infer thresholds at runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.incident.fingerprint import compute as compute_incident_fingerprint
from blackboxrs.incident.models import DetectorTrigger

from .bag_manifest import (
    BAG_MANIFEST_SCHEMA,
    BagManifest,
    compute_manifest_sha256,
    verify_bag_manifest,
)
from .rules import PreventionRule, verify_rule_fingerprint


class TelemetryHealthContract(BaseModel):
    """Runtime parameters fixed by one evidence-derived rule.

    ``aggregate_topic`` evaluates the combined traffic from all compatible
    publishers. It does not protect the identity or health of any specific
    producer while another publisher keeps the aggregate traffic healthy.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str
    expected_type: str
    expected_qos: dict[str, Any]
    declared_context_label: str
    publisher_semantics: Literal["aggregate_topic"] = "aggregate_topic"
    startup_grace_sec: float = Field(..., gt=0.0)
    stale_timeout_sec: float = Field(..., gt=0.0)
    minimum_rate_hz: float = Field(..., gt=0.0)
    rate_window_sec: float = Field(..., gt=0.0)
    header_progress_timeout_sec: float = Field(..., gt=0.0)
    require_header_progress: bool = True
    lifecycle_stages: list[Literal["startup", "runtime"]]

    @model_validator(mode="after")
    def _validate_bounds(self) -> "TelemetryHealthContract":
        if not self.topic.startswith("/"):
            raise ValueError("telemetry-health topic must be fully qualified")
        if self.rate_window_sec <= self.stale_timeout_sec:
            raise ValueError("rate_window_sec must exceed stale_timeout_sec")
        if set(self.lifecycle_stages) != {"startup", "runtime"}:
            raise ValueError("telemetry-health contract must cover startup and runtime")
        return self


class HealthyTelemetryStatistics(BaseModel):
    """Measured healthy behavior used to select the contract."""

    model_config = ConfigDict(extra="forbid")

    message_count: int = Field(..., gt=1)
    startup_delay_sec: float = Field(..., ge=0.0)
    observed_duration_sec: float = Field(..., gt=0.0)
    mean_rate_hz: float = Field(..., gt=0.0)
    median_rate_hz: float = Field(..., gt=0.0)
    inter_arrival_sec: dict[str, float]
    rolling_rate_hz: dict[str, dict[str, float]]
    header_nonprogressing_deltas: int = Field(..., ge=0)
    header_frozen_deltas: int = Field(..., ge=0)
    header_negative_deltas: int = Field(..., ge=0)
    payload_nonfinite_values: int = Field(..., ge=0)
    consecutive_exact_pose_repeats: int = Field(..., ge=0)
    unique_pose_vectors: int = Field(..., gt=0)


class TelemetryThresholds(BaseModel):
    """Selected thresholds and their deterministic derivation."""

    model_config = ConfigDict(extra="forbid")

    startup_grace_sec: float = Field(..., gt=0.0)
    stale_timeout_sec: float = Field(..., gt=0.0)
    minimum_rate_hz: float = Field(..., gt=0.0)
    rate_window_sec: float = Field(..., gt=0.0)
    header_progress_timeout_sec: float = Field(..., gt=0.0)
    allowed_jitter_sec: float = Field(..., gt=0.0)


class HistoricalTelemetryHealthEvidenceV1(BaseModel):
    """Readable historical evidence that is never eligible for trusted adoption."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["telemetry-health-evidence-v1"]
    evidence_id: str
    evidence_fingerprint: str | None = None
    source_bag_path: str
    source_bag_sha256: str
    metadata_sha256: str
    source_bag_size_bytes: int = Field(..., gt=0)
    source_bag_duration_sec: float = Field(..., gt=0.0)
    source_bag_message_count: int = Field(..., gt=0)
    topic: str
    message_type: str
    offered_qos: dict[str, Any]
    graph_context: str
    statistics: HealthyTelemetryStatistics
    thresholds: TelemetryThresholds
    derivation_method: dict[str, str]
    confidence_bounds: dict[str, Any]


class TelemetryHealthEvidence(BaseModel):
    """Content-addressed v2 characterization with a framed bag manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["telemetry-health-evidence-v2"]
    digest_schema: Literal["blackboxrs-bag-manifest-v2"] = BAG_MANIFEST_SCHEMA
    evidence_id: str
    evidence_fingerprint: str | None = None
    source_bag_path: str
    source_bag_manifest_sha256: str
    source_bag_manifest: BagManifest
    metadata_sha256: str
    source_bag_size_bytes: int = Field(..., gt=0)
    source_bag_duration_sec: float = Field(..., gt=0.0)
    source_bag_message_count: int = Field(..., gt=0)
    topic: str
    message_type: str
    offered_qos: dict[str, Any]
    declared_context_label: str
    statistics: HealthyTelemetryStatistics
    thresholds: TelemetryThresholds
    derivation_method: dict[str, str]
    confidence_bounds: dict[str, Any]

    @model_validator(mode="after")
    def _validate_manifest_binding(self) -> "TelemetryHealthEvidence":
        if compute_manifest_sha256(self.source_bag_manifest) != (
            self.source_bag_manifest_sha256
        ):
            raise ValueError("Telemetry bag manifest fingerprint mismatch")
        if self.metadata_sha256 != self.source_bag_manifest.metadata.sha256:
            raise ValueError("Telemetry metadata hash differs from bag manifest")
        if self.source_bag_size_bytes != self.source_bag_manifest.total_size:
            raise ValueError("Telemetry source bag size differs from bag manifest")
        return self


TelemetryEvidenceDocument = (
    HistoricalTelemetryHealthEvidenceV1 | TelemetryHealthEvidence
)


def compute_evidence_fingerprint(evidence: TelemetryEvidenceDocument) -> str:
    """Compute the SHA-256 for an evidence document, excluding its hash field."""
    payload = evidence.model_dump(
        mode="json",
        by_alias=True,
        exclude={"evidence_fingerprint"},
    )
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_telemetry_evidence(path: Path) -> TelemetryEvidenceDocument:
    """Load and verify a content-addressed healthy-telemetry evidence file."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        schema_version = json.loads(raw).get("schema_version")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Telemetry evidence is malformed: {path}") from exc
    model = (
        HistoricalTelemetryHealthEvidenceV1
        if schema_version == "telemetry-health-evidence-v1"
        else TelemetryHealthEvidence
    )
    evidence = model.model_validate_json(raw)
    expected = compute_evidence_fingerprint(evidence)
    if evidence.evidence_fingerprint != expected:
        raise ValueError(f"Telemetry evidence fingerprint mismatch: {path}")
    return evidence


def verify_evidence_sources(evidence: TelemetryHealthEvidence) -> None:
    """Verify the current bag against the evidence's complete v2 manifest."""
    verify_bag_manifest(Path(evidence.source_bag_path), evidence.source_bag_manifest)


def load_source_event(bundle_path: Path, event_ref: str) -> BlackBoxEvent:
    """Resolve one stable ``events.jsonl#L<n>`` reference inside a bundle."""
    if not event_ref.startswith("events.jsonl#L"):
        raise ValueError(
            "Telemetry source event reference is not a stable events.jsonl line"
        )
    try:
        line_no = int(event_ref.rsplit("L", 1)[1])
        lines = (
            (Path(bundle_path) / "evidence" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if line_no < 1 or line_no > len(lines) or not lines[line_no - 1].strip():
            raise ValueError
        return BlackBoxEvent.from_jsonl(lines[line_no - 1])
    except (OSError, ValueError) as exc:
        raise ValueError("Telemetry source event reference does not resolve") from exc


def verify_source_event_binding(
    bundle_path: Path, trigger: DetectorTrigger
) -> BlackBoxEvent:
    """Verify trigger identity against the referenced source event.

    This checks provenance identity only. It does not validate ROS message
    payload semantics.
    """
    if not trigger.source_event_ref:
        raise ValueError("Telemetry source trigger has no event cross-reference")
    event = load_source_event(bundle_path, trigger.source_event_ref)
    detector = str(event.data.get("detector", "unknown"))
    detector_class = str(
        event.metadata.get("detector_class", detector) or detector
    )
    subject = str(
        event.data.get("topic")
        or event.data.get("metric")
        or event.data.get("subject")
        or event.event_type
    )
    if event.source != "anomaly_engine":
        raise ValueError("Telemetry source event is not an anomaly event")
    if event.timestamp != trigger.t:
        raise ValueError("Telemetry source event timestamp differs from trigger")
    if detector != trigger.detector:
        raise ValueError("Telemetry source event detector differs from trigger")
    if detector_class != trigger.detector_class:
        raise ValueError("Telemetry source event detector class differs from trigger")
    if subject != trigger.subject:
        raise ValueError("Telemetry source event topic differs from trigger")
    expected_trigger_id = DetectorTrigger.make_id(
        trigger.detector, trigger.t, trigger.subject
    )
    if trigger.trigger_id != expected_trigger_id:
        raise ValueError("Telemetry source trigger ID is not deterministically derived")
    return event


def verify_incident_source(
    rule: PreventionRule, contract: TelemetryHealthContract
) -> None:
    """Verify the finalized incident, trigger, and source-event rule bindings."""
    incident_path = Path(str(rule.derivation["source_incident_ref"]))
    manifest_path = incident_path / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ValueError("Telemetry source incident manifest is unavailable") from exc
    if (
        hashlib.sha256(manifest_bytes).hexdigest()
        != rule.derivation["source_incident_manifest_sha256"]
    ):
        raise ValueError("Telemetry source incident manifest hash mismatch")

    try:
        reader = BundleReader(incident_path, strict=False)
        result = reader.validate(require_finalized=True)
    except (OSError, ValueError) as exc:
        raise ValueError("Telemetry source incident is unavailable") from exc
    if result.errors:
        details = "; ".join(
            f"{issue.code}:{issue.path or '-'}:{issue.message}"
            for issue in result.errors
        )
        raise ValueError(
            f"Telemetry source incident failed integrity validation: {details}"
        )

    incident = reader.load_incident()
    if incident.incident_id != rule.source_incident_id:
        raise ValueError("Telemetry source incident ID differs from rule")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("Telemetry source incident manifest is malformed") from exc
    if manifest.get("incident_id") != incident.incident_id:
        raise ValueError("Telemetry manifest incident ID differs from incident")
    if incident.fingerprint is None:
        raise ValueError("Telemetry source incident has no failure fingerprint")
    if incident.fingerprint.fingerprint_id != rule.source_fingerprint_id:
        raise ValueError("Telemetry source incident fingerprint differs from rule")
    bundle_fingerprint = reader.load_fingerprint()
    if bundle_fingerprint != incident.fingerprint:
        raise ValueError("Telemetry source incident fingerprints are inconsistent")

    triggers = reader.load_triggers()
    matching = [
        trigger
        for trigger in triggers
        if trigger.trigger_id == rule.derivation["source_trigger_id"]
    ]
    if len(matching) != 1:
        raise ValueError(
            "Telemetry source trigger does not resolve uniquely in incident"
        )
    trigger = matching[0]
    if trigger.trigger_id not in incident.triggers:
        raise ValueError("Telemetry source trigger is not cross-referenced by incident")
    if trigger.detector_class != rule.derivation["source_detector_class"]:
        raise ValueError("Telemetry source detector class differs from rule")
    if trigger.subject != rule.derivation["source_topic"]:
        raise ValueError("Telemetry source topic differs from incident evidence")
    if trigger.subject != contract.topic:
        raise ValueError("Telemetry contract topic differs from incident evidence")
    if trigger.source_event_ref != rule.derivation["source_event_ref"]:
        raise ValueError("Telemetry source event reference differs from trigger")

    top = incident.likely_causes[0] if incident.likely_causes else None
    if top is None:
        raise ValueError("Telemetry source incident has no likely cause")
    required_refs = {
        trigger.source_event_ref,
        f"triggers.json#{trigger.trigger_id}",
    }
    if None in required_refs or not required_refs.issubset(set(top.evidence_refs)):
        raise ValueError(
            "Telemetry source incident is missing trigger or event cross-reference"
        )
    if top.confidence != float(rule.derivation["hypothesis_confidence"]):
        raise ValueError("Telemetry source hypothesis confidence differs from rule")

    verify_source_event_binding(incident_path, trigger)
    computed = compute_incident_fingerprint(triggers, reader.load_snapshots())
    if computed.fingerprint_id != incident.fingerprint.fingerprint_id:
        raise ValueError(
            "Telemetry source incident fingerprint does not match its triggers"
        )


def derive_thresholds(
    statistics: HealthyTelemetryStatistics,
) -> TelemetryThresholds:
    """Apply the fixed v1 guard-band method to measured healthy statistics."""
    max_gap = statistics.inter_arrival_sec["max"]
    stale_timeout = _ceil_step(2.0 * max_gap, 0.05)
    startup_grace = _ceil_step(3.0 * statistics.startup_delay_sec, 0.05)
    minimum_rate = math.floor(
        (0.80 * statistics.median_rate_hz) / 0.5
    ) * 0.5
    return TelemetryThresholds(
        startup_grace_sec=startup_grace,
        stale_timeout_sec=stale_timeout,
        minimum_rate_hz=minimum_rate,
        rate_window_sec=2.0,
        header_progress_timeout_sec=stale_timeout,
        allowed_jitter_sec=stale_timeout,
    )


def _ceil_step(value: float, step: float) -> float:
    return round(math.ceil((value / step) - 1e-12) * step, 10)


def contract_from_rule(
    rule: PreventionRule, *, trusted_rule_fingerprint: str | None = None
) -> TelemetryHealthContract:
    """Validate telemetry rule provenance and return its runtime contract.

    Runtime enforcement fails closed when any source identity, evidence
    reference, confidence field, or fingerprint is absent.
    """
    if rule.check.kind != "telemetry_health":
        raise ValueError(f"Expected telemetry_health rule, got {rule.check.kind!r}")
    if not verify_rule_fingerprint(rule):
        raise ValueError("Telemetry-health rule fingerprint is absent or invalid")
    if trusted_rule_fingerprint is None or rule.rule_fingerprint != trusted_rule_fingerprint:
        raise ValueError("Telemetry-health rule is not in the trusted local allowlist")
    if not rule.source_incident_id:
        raise ValueError("Telemetry-health rule has no source incident")
    if not rule.source_fingerprint_id:
        raise ValueError("Telemetry-health rule has no source incident fingerprint")
    if len(rule.source_trigger_ids) != 1:
        raise ValueError("Telemetry-health rule requires exactly one source trigger")

    required = {
        "confidence_bounds",
        "healthy_statistics",
        "strategy",
        "source_detector_class",
        "source_trigger_id",
        "source_event_ref",
        "source_topic",
        "hypothesis_confidence",
        "healthy_evidence_ref",
        "healthy_evidence_fingerprint",
        "source_bag_manifest_schema",
        "source_bag_manifest_sha256",
        "threshold_derivation",
        "source_incident_ref",
        "source_incident_manifest_sha256",
        "selected_thresholds",
    }
    missing = sorted(key for key in required if not rule.derivation.get(key))
    if missing:
        raise ValueError(
            "Telemetry-health rule provenance is incomplete: " + ", ".join(missing)
        )
    if rule.derivation["strategy"] != "dead_topic_telemetry_health_v2":
        raise ValueError("Unsupported telemetry-health derivation strategy")
    if not str(rule.derivation["source_detector_class"]).endswith(
        "DeadTopicDetector"
    ):
        raise ValueError("Telemetry-health rule was not derived from a dead-topic trigger")
    if rule.derivation["source_trigger_id"] != rule.source_trigger_ids[0]:
        raise ValueError("Telemetry-health source trigger identity is inconsistent")
    if float(rule.derivation["hypothesis_confidence"]) < 0.70:
        raise ValueError("Telemetry-health source hypothesis is below confidence floor")

    contract = TelemetryHealthContract.model_validate(rule.check.params)
    if contract.topic != rule.derivation["source_topic"]:
        raise ValueError("Telemetry-health contract topic differs from source topic")
    verify_incident_source(rule, contract)
    evidence_ref = str(rule.derivation["healthy_evidence_ref"]).split("#", 1)[0]
    evidence = load_telemetry_evidence(Path(evidence_ref))
    if not isinstance(evidence, TelemetryHealthEvidence):
        raise ValueError(
            "Historical telemetry-health evidence v1 requires explicit migration "
            "before trusted adoption"
        )
    verify_evidence_sources(evidence)
    if evidence.evidence_fingerprint != rule.derivation["healthy_evidence_fingerprint"]:
        raise ValueError("Telemetry evidence fingerprint differs from rule")
    if evidence.topic != contract.topic or evidence.message_type != contract.expected_type:
        raise ValueError("Telemetry evidence identity differs from rule")
    if evidence.offered_qos != contract.expected_qos:
        raise ValueError("Telemetry evidence QoS differs from rule")
    if evidence.declared_context_label != contract.declared_context_label:
        raise ValueError("Telemetry evidence declared context label differs from rule")
    if evidence.thresholds.model_dump(mode="json") != rule.derivation.get("selected_thresholds"):
        raise ValueError("Telemetry thresholds differ from selected provenance")
    contract_thresholds = {
        "startup_grace_sec": contract.startup_grace_sec,
        "stale_timeout_sec": contract.stale_timeout_sec,
        "minimum_rate_hz": contract.minimum_rate_hz,
        "rate_window_sec": contract.rate_window_sec,
        "header_progress_timeout_sec": contract.header_progress_timeout_sec,
    }
    selected_thresholds = dict(rule.derivation["selected_thresholds"])
    if any(
        contract_thresholds[key] != selected_thresholds.get(key)
        for key in contract_thresholds
    ):
        raise ValueError("Telemetry contract thresholds differ from selected provenance")
    healthy_statistics = {
        "message_count": evidence.statistics.message_count,
        "mean_rate_hz": evidence.statistics.mean_rate_hz,
        "median_rate_hz": evidence.statistics.median_rate_hz,
        "p95_inter_arrival_sec": evidence.statistics.inter_arrival_sec["p95"],
        "p99_inter_arrival_sec": evidence.statistics.inter_arrival_sec["p99"],
        "max_healthy_gap_sec": evidence.statistics.inter_arrival_sec["max"],
    }
    if healthy_statistics != rule.derivation["healthy_statistics"]:
        raise ValueError("Telemetry healthy statistics differ from rule provenance")
    if evidence.derivation_method != rule.derivation["threshold_derivation"]:
        raise ValueError("Telemetry threshold derivation differs from evidence")
    if evidence.confidence_bounds != rule.derivation["confidence_bounds"]:
        raise ValueError("Telemetry confidence bounds differ from evidence")
    if rule.derivation["source_bag_manifest_schema"] != BAG_MANIFEST_SCHEMA:
        raise ValueError("Telemetry source bag manifest schema differs from rule")
    if (
        evidence.source_bag_manifest_sha256
        != rule.derivation["source_bag_manifest_sha256"]
    ):
        raise ValueError("Telemetry source bag manifest differs from rule")
    return contract


HealthState = Literal["starting", "healthy", "failed"]
FailureReason = Literal[
    "startup_timeout",
    "stale",
    "below_rate",
    "frozen_timestamp",
    "graph_inconsistent",
]


@dataclass(frozen=True)
class TelemetryHealthEvaluation:
    """One deterministic state-machine evaluation."""

    state: HealthState
    evaluated_at: float
    observed_rate_hz: float | None = None
    reason: FailureReason | None = None
    detail: str = ""


class TelemetryHealthState:
    """Monotonic-clock state machine shared by replay and live ROS guards."""

    def __init__(self, contract: TelemetryHealthContract, *, started_at: float) -> None:
        self.contract = contract
        self.started_at = started_at
        self.first_received_at: float | None = None
        self.last_received_at: float | None = None
        self.last_header_stamp_ns: int | None = None
        self.last_header_progress_at: float | None = None
        self.maximum_observed_gap_sec = 0.0
        self._arrivals: deque[float] = deque()
        self._failure: TelemetryHealthEvaluation | None = None

    def observe(self, *, received_at: float, header_stamp_ns: int) -> None:
        """Record one message without clearing a latched health failure."""
        if self._failure is not None:
            return
        if self.first_received_at is None:
            self.first_received_at = received_at
        if self.last_received_at is not None:
            self.maximum_observed_gap_sec = max(
                self.maximum_observed_gap_sec,
                received_at - self.last_received_at,
            )
        self.last_received_at = received_at
        self._arrivals.append(received_at)
        self._prune(received_at)

        if self.last_header_stamp_ns is None or header_stamp_ns > self.last_header_stamp_ns:
            self.last_header_stamp_ns = header_stamp_ns
            self.last_header_progress_at = received_at

    def evaluate(self, now: float) -> TelemetryHealthEvaluation:
        """Evaluate startup, freshness, header progress, and sustained rate."""
        if self._failure is not None:
            return self._failure

        first = self.first_received_at
        if first is None:
            if now - self.started_at > self.contract.startup_grace_sec:
                return self._fail(
                    now,
                    "startup_timeout",
                    f"no message within {self.contract.startup_grace_sec:.3f}s",
                )
            return TelemetryHealthEvaluation(
                state="starting", evaluated_at=now, detail="waiting for first message"
            )

        assert self.last_received_at is not None
        receive_age = now - self.last_received_at
        if receive_age > self.contract.stale_timeout_sec:
            return self._fail(
                now,
                "stale",
                f"last message age {receive_age:.6f}s exceeds "
                f"{self.contract.stale_timeout_sec:.6f}s",
            )

        if self.contract.require_header_progress:
            progress_at = self.last_header_progress_at
            if (
                progress_at is None
                or now - progress_at > self.contract.header_progress_timeout_sec
            ):
                age = now - (progress_at if progress_at is not None else first)
                return self._fail(
                    now,
                    "frozen_timestamp",
                    f"header timestamp has not progressed for {age:.6f}s",
                )

        if now - first < self.contract.rate_window_sec:
            return TelemetryHealthEvaluation(
                state="starting",
                evaluated_at=now,
                detail="collecting rate qualification window",
            )

        self._prune(now)
        if len(self._arrivals) < 2:
            return TelemetryHealthEvaluation(state="starting", evaluated_at=now, detail="collecting rate intervals")
        intervals = [b - a for a, b in zip(self._arrivals, list(self._arrivals)[1:])]
        observed_rate = 1.0 / statistics.fmean(intervals) if intervals else 0.0
        # Preserve the mathematical hard boundary despite binary floating
        # point noise in otherwise exact interval schedules.
        if observed_rate + 1e-9 < self.contract.minimum_rate_hz:
            return self._fail(
                now,
                "below_rate",
                f"observed {observed_rate:.3f}Hz below "
                f"{self.contract.minimum_rate_hz:.3f}Hz",
                observed_rate_hz=observed_rate,
            )
        return TelemetryHealthEvaluation(
            state="healthy",
            evaluated_at=now,
            observed_rate_hz=observed_rate,
            detail="telemetry contract satisfied",
        )

    def fail(self, now: float, reason: FailureReason, detail: str) -> TelemetryHealthEvaluation:
        """Latch an external structural failure detected by the ROS adapter."""
        return self._fail(now, reason, detail)

    def _prune(self, now: float) -> None:
        cutoff = now - self.contract.rate_window_sec
        while self._arrivals and self._arrivals[0] < cutoff:
            self._arrivals.popleft()

    def _fail(
        self,
        now: float,
        reason: FailureReason,
        detail: str,
        *,
        observed_rate_hz: float | None = None,
    ) -> TelemetryHealthEvaluation:
        self._failure = TelemetryHealthEvaluation(
            state="failed",
            evaluated_at=now,
            observed_rate_hz=observed_rate_hz,
            reason=reason,
            detail=detail,
        )
        return self._failure
