"""Tests for the bounded telemetry-health contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from blackboxrs.incident.models import (
    DetectorTrigger,
    FailureFingerprint,
    Incident,
    LikelyCauseHypothesis,
)
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_telemetry_health_rule,
)
from blackboxrs.prevention.rules import PreflightCheck, make_rule
from blackboxrs.prevention.telemetry_health import (
    HealthyTelemetryStatistics,
    TelemetryHealthContract,
    TelemetryHealthEvidence,
    TelemetryHealthState,
    compute_evidence_fingerprint,
    contract_from_rule,
    derive_thresholds,
)


def _statistics() -> HealthyTelemetryStatistics:
    return HealthyTelemetryStatistics(
        message_count=6177,
        startup_delay_sec=0.158142077,
        observed_duration_sec=329.443225884,
        mean_rate_hz=18.746780976988813,
        median_rate_hz=18.756079021048965,
        inter_arrival_sec={
            "minimum": 0.045329218,
            "mean": 0.053342491237694294,
            "median": 0.0533160475,
            "p90": 0.0544107025,
            "p95": 0.0559826245,
            "p99": 0.05693549725,
            "p99_5": 0.057507992125,
            "p99_9": 0.06031294775,
            "max": 0.070847572,
        },
        rolling_rate_hz={"2s": {"minimum": 18.5}},
        header_nonprogressing_deltas=0,
        header_frozen_deltas=0,
        header_negative_deltas=0,
        payload_nonfinite_values=0,
        consecutive_exact_pose_repeats=0,
        unique_pose_vectors=6177,
    )


def _contract() -> TelemetryHealthContract:
    thresholds = derive_thresholds(_statistics())
    return TelemetryHealthContract(
        topic="/utlidar/robot_pose",
        expected_type="geometry_msgs/msg/PoseStamped",
        expected_qos={
            "history": "keep_last",
            "depth": 1,
            "reliability": "reliable",
            "durability": "volatile",
        },
        graph_context="go2_utlidar_hardware_eval_20260406",
        startup_grace_sec=thresholds.startup_grace_sec,
        stale_timeout_sec=thresholds.stale_timeout_sec,
        minimum_rate_hz=thresholds.minimum_rate_hz,
        rate_window_sec=thresholds.rate_window_sec,
        header_progress_timeout_sec=thresholds.header_progress_timeout_sec,
        require_header_progress=True,
        lifecycle_stages=["startup", "runtime"],
    )


def _observe_rate(
    state: TelemetryHealthState,
    *,
    start: float,
    end: float,
    rate_hz: float,
    frozen_after: float | None = None,
) -> None:
    step = 1.0 / rate_hz
    index = 0
    current = start
    while current <= end + 1e-12:
        header_index = (
            int((frozen_after - start) / step)
            if frozen_after is not None and current >= frozen_after
            else index
        )
        state.observe(
            received_at=current,
            header_stamp_ns=1_000_000_000 + header_index * int(step * 1e9),
        )
        index += 1
        current += step


def test_thresholds_are_fixed_transform_of_genuine_statistics():
    thresholds = derive_thresholds(_statistics())

    assert thresholds.startup_grace_sec == 0.5
    assert thresholds.stale_timeout_sec == 0.15
    assert thresholds.minimum_rate_hz == 15.0
    assert thresholds.rate_window_sec == 2.0
    assert thresholds.header_progress_timeout_sec == 0.15


def test_healthy_rate_qualifies_and_remains_healthy():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe_rate(state, start=0.1, end=2.3, rate_hz=18.75)

    result = state.evaluate(2.3)

    assert result.state == "healthy"
    assert result.observed_rate_hz is not None
    assert result.observed_rate_hz >= 18.0


def test_publisher_present_but_silent_fails_freshness():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe_rate(state, start=0.1, end=2.3, rate_hz=18.75)
    assert state.evaluate(2.3).state == "healthy"

    result = state.evaluate(2.451)

    assert result.state == "failed"
    assert result.reason == "stale"


def test_slow_publisher_fails_after_rate_window():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe_rate(state, start=0.1, end=2.2, rate_hz=10.0)

    result = state.evaluate(2.2)

    assert result.state == "failed"
    assert result.reason == "below_rate"


def test_frozen_header_fails_while_messages_continue():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe_rate(
        state,
        start=0.1,
        end=2.5,
        rate_hz=18.75,
        frozen_after=2.25,
    )

    result = state.evaluate(2.5)

    assert result.state == "failed"
    assert result.reason == "frozen_timestamp"


def test_short_gap_below_stale_timeout_passes():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe_rate(state, start=0.1, end=2.3, rate_hz=18.75)
    assert state.evaluate(2.3).state == "healthy"

    assert state.last_received_at is not None
    resume_at = state.last_received_at + 0.149
    before_resume = state.evaluate(resume_at)
    state.observe(received_at=resume_at, header_stamp_ns=4_000_000_000)
    after_resume = state.evaluate(resume_at)

    assert before_resume.state == "healthy"
    assert after_resume.state == "healthy"


def test_startup_delay_just_inside_grace_passes():
    state = TelemetryHealthState(_contract(), started_at=0.0)
    assert state.evaluate(0.499).state == "starting"
    _observe_rate(state, start=0.499, end=2.6, rate_hz=18.75)

    assert state.evaluate(2.6).state == "healthy"


def test_zero_messages_fails_startup():
    state = TelemetryHealthState(_contract(), started_at=0.0)

    result = state.evaluate(0.501)

    assert result.state == "failed"
    assert result.reason == "startup_timeout"


def test_runtime_rule_requires_complete_provenance_and_valid_fingerprint():
    contract = _contract()
    rule = make_rule(
        PreflightCheck(
            name="runtime telemetry",
            kind="telemetry_health",
            params=contract.model_dump(mode="json"),
            applies_to=[contract.graph_context],
        ),
        source_incident_id="inc_2026-07-23T00-00-00_12345678",
        source_fingerprint_id="fpr_1234567890abcdef",
        source_trigger_ids=["trg_12345678"],
        derivation={
            "strategy": "dead_topic_telemetry_health_v1",
            "source_detector_class": "pkg.DeadTopicDetector",
            "source_trigger_id": "trg_12345678",
            "source_event_ref": "events.jsonl#L2",
            "source_topic": contract.topic,
            "hypothesis_confidence": 0.98,
            "healthy_evidence_ref": "/tmp/evidence.json#statistics",
            "healthy_evidence_fingerprint": "a" * 64,
            "source_bag_sha256": "b" * 64,
            "threshold_derivation": {"method": "test"},
        },
    )

    assert contract_from_rule(rule) == contract

    tampered = rule.model_copy(
        update={"derivation": {**rule.derivation, "source_topic": "/other"}}
    )
    try:
        contract_from_rule(tampered)
    except ValueError as exc:
        assert "fingerprint" in str(exc)
    else:
        raise AssertionError("tampered rule was admitted")


def _incident_and_trigger() -> tuple[Incident, DetectorTrigger]:
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    trigger = DetectorTrigger(
        trigger_id="trg_12345678",
        detector="dead_topic",
        detector_class="pkg.DeadTopicDetector",
        t=now,
        subsystem="ros",
        subject="/utlidar/robot_pose",
        severity="error",
        message="stopped",
        data={"topic": "/utlidar/robot_pose"},
        source_event_ref="events.jsonl#L2231",
    )
    incident = Incident(
        incident_id="inc_2026-07-23T00-00-00_12345678",
        created_at=now,
        window_start=now,
        window_end=now,
        session_id="s",
        title="dead pose",
        bundle_path="/tmp/incident",
        fingerprint=FailureFingerprint(
            fingerprint_id="fpr_1234567890abcdef"
        ),
        likely_causes=[
            LikelyCauseHypothesis(
                cause="Topic /utlidar/robot_pose stopped emitting messages.",
                confidence=0.98,
                evidence_refs=[
                    "events.jsonl#L2231",
                    "triggers.json#trg_12345678",
                ],
            )
        ],
    )
    return incident, trigger


def _write_evidence(tmp_path, *, message_count: int = 6177):
    stats = _statistics().model_copy(update={"message_count": message_count})
    evidence = TelemetryHealthEvidence(
        schema_version="telemetry-health-evidence-v1",
        evidence_id="evh_123456789abc",
        source_bag_path="/data/go2",
        source_bag_sha256="a" * 64,
        metadata_sha256="b" * 64,
        source_bag_size_bytes=651_000_000,
        source_bag_duration_sec=329.6,
        source_bag_message_count=94_325,
        topic="/utlidar/robot_pose",
        message_type="geometry_msgs/msg/PoseStamped",
        offered_qos={
            "history": "keep_last",
            "depth": 1,
            "reliability": "reliable",
            "durability": "volatile",
        },
        graph_context="go2_utlidar_hardware_eval_20260406",
        statistics=stats,
        thresholds=derive_thresholds(stats),
        derivation_method={"method": "test"},
        confidence_bounds={"descriptive": True},
    )
    evidence = evidence.model_copy(
        update={"evidence_fingerprint": compute_evidence_fingerprint(evidence)}
    )
    path = tmp_path / "healthy.json"
    path.write_text(
        json.dumps(evidence.model_dump(mode="json")),
        encoding="utf-8",
    )
    return path


def test_telemetry_rule_derivation_binds_incident_and_healthy_evidence(tmp_path):
    incident, trigger = _incident_and_trigger()
    reader = SimpleNamespace(
        validate=lambda require_finalized: SimpleNamespace(errors=[]),
        load_incident=lambda: incident,
        load_triggers=lambda: [trigger],
    )

    derivation = derive_telemetry_health_rule(reader, _write_evidence(tmp_path))

    assert derivation.rule.check.kind == "telemetry_health"
    assert derivation.rule.source_trigger_ids == ["trg_12345678"]
    assert derivation.rule.derivation["source_event_ref"] == "events.jsonl#L2231"
    assert derivation.rule.derivation["healthy_evidence_fingerprint"]
    assert contract_from_rule(derivation.rule).minimum_rate_hz == 15.0


def test_telemetry_rule_derivation_refuses_insufficient_healthy_evidence(tmp_path):
    incident, trigger = _incident_and_trigger()
    reader = SimpleNamespace(
        validate=lambda require_finalized: SimpleNamespace(errors=[]),
        load_incident=lambda: incident,
        load_triggers=lambda: [trigger],
    )

    with pytest.raises(PreventionDerivationError, match="fewer than 1000"):
        derive_telemetry_health_rule(
            reader,
            _write_evidence(tmp_path, message_count=10),
        )

