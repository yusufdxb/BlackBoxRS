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

from .rules import PreventionRule, verify_rule_fingerprint


class TelemetryHealthContract(BaseModel):
    """Runtime parameters fixed by one evidence-derived rule."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    expected_type: str
    expected_qos: dict[str, Any]
    graph_context: str
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


class TelemetryHealthEvidence(BaseModel):
    """Content-addressed healthy-bag characterization."""

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


def compute_evidence_fingerprint(evidence: TelemetryHealthEvidence) -> str:
    """Compute the SHA-256 for an evidence document, excluding its hash field."""
    payload = evidence.model_dump(mode="json", exclude={"evidence_fingerprint"})
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_telemetry_evidence(path: Path) -> TelemetryHealthEvidence:
    """Load and verify a content-addressed healthy-telemetry evidence file."""
    evidence = TelemetryHealthEvidence.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
    expected = compute_evidence_fingerprint(evidence)
    if evidence.evidence_fingerprint != expected:
        raise ValueError(f"Telemetry evidence fingerprint mismatch: {path}")
    return evidence


def verify_evidence_sources(evidence: TelemetryHealthEvidence) -> None:
    """Verify the recorded bag and metadata bytes, not just the JSON self-hash."""
    bag = Path(evidence.source_bag_path)
    metadata = bag / "metadata.yaml"
    if not bag.is_dir() or not metadata.is_file():
        raise ValueError(f"Telemetry source bag is unavailable: {bag}")
    metadata_hash = hashlib.sha256(metadata.read_bytes()).hexdigest()
    if metadata_hash != evidence.metadata_sha256:
        raise ValueError("Telemetry metadata hash mismatch")
    files = sorted((p for p in bag.rglob("*") if p.is_file()), key=lambda p: p.name)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(bag).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    if digest.hexdigest() != evidence.source_bag_sha256:
        raise ValueError("Telemetry source bag hash mismatch")


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
        "strategy",
        "source_detector_class",
        "source_trigger_id",
        "source_event_ref",
        "source_topic",
        "hypothesis_confidence",
        "healthy_evidence_ref",
        "healthy_evidence_fingerprint",
        "source_bag_sha256",
        "threshold_derivation",
        "source_incident_ref",
        "source_incident_manifest_sha256",
    }
    missing = sorted(key for key in required if not rule.derivation.get(key))
    if missing:
        raise ValueError(
            "Telemetry-health rule provenance is incomplete: " + ", ".join(missing)
        )
    if rule.derivation["strategy"] != "dead_topic_telemetry_health_v1":
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
    evidence_ref = str(rule.derivation["healthy_evidence_ref"]).split("#", 1)[0]
    evidence = load_telemetry_evidence(Path(evidence_ref))
    verify_evidence_sources(evidence)
    if evidence.evidence_fingerprint != rule.derivation["healthy_evidence_fingerprint"]:
        raise ValueError("Telemetry evidence fingerprint differs from rule")
    if evidence.topic != contract.topic or evidence.message_type != contract.expected_type:
        raise ValueError("Telemetry evidence identity differs from rule")
    if evidence.graph_context != contract.graph_context:
        raise ValueError("Telemetry evidence context differs from rule")
    if evidence.thresholds.model_dump(mode="json") != rule.derivation.get("selected_thresholds"):
        raise ValueError("Telemetry thresholds differ from selected provenance")
    if evidence.source_bag_sha256 != rule.derivation["source_bag_sha256"]:
        raise ValueError("Telemetry source bag differs from rule")
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
        if observed_rate < self.contract.minimum_rate_hz:
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
