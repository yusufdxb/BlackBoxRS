"""Incident-to-prevention rule derivation tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackboxrs.incident.models import (
    DetectorTrigger,
    FailureFingerprint,
    Incident,
    LikelyCauseHypothesis,
)
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_rule_from_incident,
)


_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _incident(confidence: float = 0.9) -> Incident:
    return Incident(
        incident_id="inc_2026-05-07T12-00-00_0badc0de",
        created_at=_NOW,
        window_start=_NOW,
        window_end=_NOW,
        session_id="s",
        title="dead topic",
        bundle_path="/tmp/inc",
        fingerprint=FailureFingerprint(fingerprint_id="fpr_" + "a" * 16),
        likely_causes=[
            LikelyCauseHypothesis(
                cause="Topic /scan stopped emitting messages.",
                confidence=confidence,
                evidence_refs=["triggers.json#trg_12345678"],
            )
        ],
    )


def _dead_topic_trigger() -> DetectorTrigger:
    return DetectorTrigger(
        trigger_id="trg_12345678",
        detector="dead_topic",
        detector_class=(
            "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector"
        ),
        t=_NOW,
        subsystem="ros",
        subject="/scan",
        severity="error",
        message="Topic /scan stopped.",
        data={"topic": "/scan"},
        source_event_ref="events.jsonl#L3",
    )


def _qos_trigger() -> DetectorTrigger:
    return DetectorTrigger(
        trigger_id="trg_abcdef12",
        detector="qos_mismatch",
        detector_class=(
            "blackboxrs.anomaly_engine.detectors.qos_mismatch."
            "QoSMismatchDetector"
        ),
        t=_NOW,
        subsystem="ros",
        subject="/cmd_vel",
        severity="error",
        message="QoS mismatch on /cmd_vel.",
        data={"topic": "/cmd_vel", "reliability_mismatch": True},
        source_event_ref="events.jsonl#L9",
    )


def test_dead_topic_incident_derives_traceable_topic_present_rule():
    derivation = derive_rule_from_incident(_incident(), [_dead_topic_trigger()])

    rule = derivation.rule
    assert rule.check.kind == "topic_present"
    assert rule.check.params["topic"] == "/scan"
    assert rule.check.params["min_publishers"] == 1
    assert rule.source_incident_id == "inc_2026-05-07T12-00-00_0badc0de"
    assert rule.source_fingerprint_id == "fpr_" + "a" * 16
    assert rule.source_trigger_ids == ["trg_12345678"]
    assert rule.derivation["source_trigger_id"] == "trg_12345678"
    assert "DeadTopicDetector" in rule.derivation["source_detector_class"]


def test_low_confidence_incident_is_not_auto_adopted():
    with pytest.raises(PreventionDerivationError, match="below threshold"):
        derive_rule_from_incident(_incident(confidence=0.4), [_dead_topic_trigger()])


def test_unsupported_trigger_is_not_auto_adopted():
    trigger = _dead_topic_trigger().model_copy(
        update={
            "trigger_id": "trg_87654321",
            "detector": "threshold",
            "detector_class": "blackboxrs.detectors.ThresholdDetector",
            "subject": "cpu_percent",
        }
    )
    with pytest.raises(PreventionDerivationError, match="No supported trigger"):
        derive_rule_from_incident(_incident(), [trigger])


def test_unrelated_supported_trigger_is_not_auto_adopted():
    incident = _incident()
    incident.likely_causes[0].evidence_refs = ["triggers.json#trg_87654321"]

    with pytest.raises(PreventionDerivationError, match="No supported trigger"):
        derive_rule_from_incident(incident, [_dead_topic_trigger()])


def test_explicit_ref_must_match_top_cause_failure_family():
    incident = _incident()
    incident.likely_causes[0].evidence_refs = ["triggers.json#trg_abcdef12"]

    with pytest.raises(PreventionDerivationError, match="No supported trigger"):
        derive_rule_from_incident(incident, [_qos_trigger()])


def test_legacy_cause_text_fallback_requires_matching_unique_trigger():
    incident = _incident()
    incident.likely_causes[0].evidence_refs = []

    derivation = derive_rule_from_incident(incident, [_dead_topic_trigger()])

    assert derivation.source_trigger.trigger_id == "trg_12345678"
