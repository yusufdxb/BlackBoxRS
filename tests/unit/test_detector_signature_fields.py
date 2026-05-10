"""Detector signature_fields integration with the fingerprint algorithm.

These tests assert three behaviours per detector:

1. The detector emits the values of its declared signature fields into
   ``event.data`` so the trigger model preserves them.
2. Two synthetic events with the same signature-field values produce
   the same fingerprint (semantic stability).
3. Perturbing a signature field changes the fingerprint, and perturbing
   a non-signature field (severity, message, run-specific noise) does
   NOT change it.

The detectors run in-process; we hand them events constructed directly
rather than going through the full daemon pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from blackboxrs.anomaly_engine.detectors import (
    DeadTopicDetector,
    FrequencyDetector,
    QoSMismatchDetector,
    ThresholdDetector,
)
from blackboxrs.core.config import (
    AnomalyThresholds,
    DeadTopicConfig,
    FrequencyConfig,
)
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.incident.builder import _promote_trigger
from blackboxrs.incident.fingerprint import compute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trigger_from_anomaly(anomaly_event: BlackBoxEvent, line: int = 1):
    """Promote an anomaly event the way the incident builder would."""
    return _promote_trigger(anomaly_event, line_index=line)


def _ros_freq_event(topic: str, hz: float, *, t: datetime | None = None) -> BlackBoxEvent:
    return BlackBoxEvent.ros_event(
        event_type="ros.frequency",
        data={"topic": topic, "frequency_hz": hz, "interval_ms": 1000.0 / hz},
        session_id="t",
    )


def _system_event(metric: str, value: float, unit: str = "%") -> BlackBoxEvent:
    return BlackBoxEvent.system_event(
        event_type=f"system.{metric.split('_')[0]}",
        data={metric: value, "metric": metric, "value": value, "unit": unit},
        session_id="t",
    )


# ---------------------------------------------------------------------------
# ThresholdDetector
# ---------------------------------------------------------------------------


def test_threshold_emits_signature_fields_in_metadata():
    det = ThresholdDetector(AnomalyThresholds(cpu_percent=50.0))
    ev = _system_event("cpu_percent", 95.0)
    out = det.check(ev)
    assert out is not None
    assert out.metadata["signature_fields"] == ["metric"]
    assert out.metadata["target_subsystem"] == "system"
    assert out.data["metric"] == "cpu_percent"


def test_threshold_gpu_metric_tags_subsystem_gpu():
    det = ThresholdDetector(AnomalyThresholds(gpu_temp_c=50.0))
    ev = BlackBoxEvent.system_event(
        event_type="system.gpu",
        data={"gpu_temp_c": 90.0},
        session_id="t",
    )
    out = det.check(ev)
    assert out is not None
    assert out.metadata["target_subsystem"] == "gpu"


def test_threshold_fingerprint_collision_same_metric():
    det = ThresholdDetector(AnomalyThresholds(cpu_percent=50.0))
    a = _trigger_from_anomaly(det.check(_system_event("cpu_percent", 95.0)))
    b = _trigger_from_anomaly(det.check(_system_event("cpu_percent", 99.5)))
    assert compute([a]).fingerprint_id == compute([b]).fingerprint_id


def test_threshold_fingerprint_diff_on_different_metrics():
    det = ThresholdDetector(
        AnomalyThresholds(cpu_percent=50.0, memory_percent=50.0)
    )
    a = _trigger_from_anomaly(det.check(_system_event("cpu_percent", 95.0)))
    b = _trigger_from_anomaly(det.check(BlackBoxEvent.system_event(
        event_type="system.memory",
        data={"memory_percent": 95.0},
        session_id="t",
    )))
    assert compute([a]).fingerprint_id != compute([b]).fingerprint_id


# ---------------------------------------------------------------------------
# FrequencyDetector
# ---------------------------------------------------------------------------


def _train_and_drop(detector: FrequencyDetector, topic: str,
                    baseline_hz: float, drop_hz: float) -> BlackBoxEvent:
    """Drive the detector through the 10-sample learning phase, then drop.

    Returns the anomaly event the detector emits on the dropped sample.
    """
    for _ in range(11):
        out = detector.check(_ros_freq_event(topic, baseline_hz))
        # Learning phase returns None for the first 10; the 11th sample
        # is the first monitoring observation. We force the drop on a
        # subsequent call to keep the contract crisp.
    out = detector.check(_ros_freq_event(topic, drop_hz))
    assert out is not None, "frequency detector did not fire on drop"
    return out


def test_frequency_emits_topic_in_data_and_signature_fields():
    det = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    out = _train_and_drop(det, "/scan", baseline_hz=10.0, drop_hz=1.0)
    assert out.data["topic"] == "/scan"
    assert out.metadata["signature_fields"] == ["topic"]
    assert out.metadata["target_subsystem"] == "ros"


def test_frequency_fingerprint_collides_on_same_topic():
    det1 = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    det2 = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    a = _trigger_from_anomaly(_train_and_drop(det1, "/scan", 10.0, 1.0))
    b = _trigger_from_anomaly(_train_and_drop(det2, "/scan", 10.0, 0.5))
    assert compute([a]).fingerprint_id == compute([b]).fingerprint_id


def test_frequency_fingerprint_differs_on_different_topic():
    det1 = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    det2 = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    a = _trigger_from_anomaly(_train_and_drop(det1, "/scan", 10.0, 1.0))
    b = _trigger_from_anomaly(_train_and_drop(det2, "/odom", 10.0, 1.0))
    assert compute([a]).fingerprint_id != compute([b]).fingerprint_id


# ---------------------------------------------------------------------------
# DeadTopicDetector
# ---------------------------------------------------------------------------


def test_dead_topic_emits_topic_in_data_and_metadata():
    det = DeadTopicDetector(DeadTopicConfig(timeout_sec=0.0))
    # First sample registers the topic; subsequent same-instant call
    # passes the timeout because timeout_sec == 0.
    det.check(_ros_freq_event("/tf_static", 1.0))
    out = det.check(BlackBoxEvent.system_event(
        event_type="system.cpu",
        data={"cpu_percent": 1.0},
        session_id="t",
    ))
    assert out is not None
    assert out.data["topic"] == "/tf_static"
    assert out.metadata["signature_fields"] == ["topic"]
    assert out.metadata["target_subsystem"] == "ros"


def test_dead_topic_fingerprint_collides_on_same_topic():
    det1 = DeadTopicDetector(DeadTopicConfig(timeout_sec=0.0))
    det2 = DeadTopicDetector(DeadTopicConfig(timeout_sec=0.0))
    det1.check(_ros_freq_event("/tf_static", 1.0))
    det2.check(_ros_freq_event("/tf_static", 1.0))
    a = _trigger_from_anomaly(det1.check(_system_event("cpu_percent", 5.0)))
    b = _trigger_from_anomaly(det2.check(_system_event("cpu_percent", 5.0)))
    assert compute([a]).fingerprint_id == compute([b]).fingerprint_id


# ---------------------------------------------------------------------------
# QoSMismatchDetector
# ---------------------------------------------------------------------------


def _qos_event(topic: str, pub_reliability: str, sub_reliability: str,
               pub_dur: str = "VOLATILE", sub_dur: str = "VOLATILE"):
    return BlackBoxEvent.ros_event(
        event_type="ros.qos",
        data={
            "topic": topic,
            "publisher_qos_profiles": [
                {"reliability": pub_reliability, "durability": pub_dur},
            ],
            "subscriber_qos_profiles": [
                {"reliability": sub_reliability, "durability": sub_dur},
            ],
        },
        session_id="t",
    )


def test_qos_emits_dimension_flags():
    det = QoSMismatchDetector()
    out = det.check(_qos_event("/scan", "BEST_EFFORT", "RELIABLE"))
    assert out is not None
    assert out.data["topic"] == "/scan"
    assert out.data["reliability_mismatch"] is True
    assert out.data["durability_mismatch"] is False
    assert out.metadata["signature_fields"] == [
        "topic", "reliability_mismatch", "durability_mismatch"
    ]
    assert out.metadata["target_subsystem"] == "ros"


def test_qos_fingerprint_collides_on_same_dimension():
    det = QoSMismatchDetector()
    a = _trigger_from_anomaly(det.check(
        _qos_event("/scan", "BEST_EFFORT", "RELIABLE")))
    b = _trigger_from_anomaly(det.check(
        _qos_event("/scan", "BEST_EFFORT", "RELIABLE")))
    assert compute([a]).fingerprint_id == compute([b]).fingerprint_id


def test_qos_fingerprint_differs_on_dimension():
    det = QoSMismatchDetector()
    a = _trigger_from_anomaly(det.check(
        _qos_event("/scan", "BEST_EFFORT", "RELIABLE")))
    b = _trigger_from_anomaly(det.check(
        _qos_event("/scan", "RELIABLE", "RELIABLE",
                   pub_dur="VOLATILE", sub_dur="TRANSIENT_LOCAL")))
    assert compute([a]).fingerprint_id != compute([b]).fingerprint_id


def test_qos_noise_does_not_perturb_fingerprint():
    """Pair labels and value counts vary across runs; identity should not."""
    det = QoSMismatchDetector()
    a = _trigger_from_anomaly(det.check(
        _qos_event("/scan", "BEST_EFFORT", "RELIABLE")))
    b = _trigger_from_anomaly(det.check(BlackBoxEvent.ros_event(
        event_type="ros.qos",
        data={
            "topic": "/scan",
            "publisher_qos_profiles": [
                {"reliability": "BEST_EFFORT", "durability": "VOLATILE"},
                {"reliability": "BEST_EFFORT", "durability": "VOLATILE"},
            ],
            "subscriber_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "VOLATILE"},
            ],
        },
        session_id="t",
    )))
    # Two pubs vs one sub means more pairwise mismatches and a different
    # numeric ``value``, but the dimension flags + topic are identical.
    # Fingerprint must collide.
    assert compute([a]).fingerprint_id == compute([b]).fingerprint_id


# ---------------------------------------------------------------------------
# Builder integration: target_subsystem propagates to the trigger
# ---------------------------------------------------------------------------


def test_promote_trigger_uses_target_subsystem_from_metadata():
    det = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
    out = _train_and_drop(det, "/scan", 10.0, 1.0)
    trig = _promote_trigger(out, line_index=42)
    assert trig.subsystem == "ros"
    assert trig.subject == "/scan"


def test_promote_trigger_falls_back_to_anomaly_when_metadata_missing():
    """Backwards compat: legacy events with no target_subsystem in
    metadata still produce a trigger with subsystem='anomaly'."""
    legacy = BlackBoxEvent(
        timestamp=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
        source="anomaly_engine",
        event_type="anomaly.dead_topic",
        severity="warning",
        data={"detector": "dead_topic", "topic": "/scan", "message": "m"},
        metadata={"session_id": "legacy"},
    )
    trig = _promote_trigger(legacy, line_index=1)
    assert trig.subsystem == "anomaly"
