"""Tests for the bounded telemetry-health contract."""

from __future__ import annotations

import pytest

from blackboxrs.incident.bundle import BundleReader
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_telemetry_health_rule,
)
from blackboxrs.prevention.telemetry_health import (
    HealthyTelemetryStatistics,
    TelemetryHealthContract,
    TelemetryHealthState,
    contract_from_rule,
    derive_thresholds,
)
from tests.telemetry_fixtures import (
    build_telemetry_provenance_fixture,
    healthy_statistics,
    write_evidence,
    write_finalized_incident,
)


def _statistics() -> HealthyTelemetryStatistics:
    return healthy_statistics()


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


def test_runtime_rule_requires_complete_provenance_and_valid_fingerprint(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = fixture.rule

    assert contract_from_rule(
        rule, trusted_rule_fingerprint=rule.rule_fingerprint
    ) == fixture.contract

    tampered = rule.model_copy(
        update={"derivation": {**rule.derivation, "source_topic": "/other"}}
    )
    with pytest.raises(ValueError, match="fingerprint"):
        contract_from_rule(
            tampered, trusted_rule_fingerprint=rule.rule_fingerprint
        )


def test_telemetry_rule_derivation_binds_incident_and_healthy_evidence(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    derivation = derive_telemetry_health_rule(
        BundleReader(fixture.bundle_path), fixture.evidence_path
    )

    assert derivation.rule.check.kind == "telemetry_health"
    assert derivation.rule.source_trigger_ids == [fixture.trigger.trigger_id]
    assert (
        derivation.rule.derivation["source_event_ref"]
        == fixture.trigger.source_event_ref
    )
    assert derivation.rule.derivation["healthy_evidence_fingerprint"]
    assert (
        contract_from_rule(
            derivation.rule,
            trusted_rule_fingerprint=derivation.rule.rule_fingerprint,
        ).minimum_rate_hz
        == 15.0
    )


def test_telemetry_rule_derivation_refuses_insufficient_healthy_evidence(tmp_path):
    bundle_path, _, _, _ = write_finalized_incident(tmp_path)
    evidence_path, _ = write_evidence(tmp_path, message_count=10)

    with pytest.raises(PreventionDerivationError, match="fewer than 1000"):
        derive_telemetry_health_rule(
            BundleReader(bundle_path),
            evidence_path,
        )
