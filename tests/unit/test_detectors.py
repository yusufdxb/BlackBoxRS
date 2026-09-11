"""Unit tests for blackboxrs.anomaly_engine.detectors.

Every event constructed here matches the *real* event_type strings and
data payloads emitted by ``SystemMonitor`` and ``RosMonitor``.  Earlier
versions of these tests fabricated event types (``cpu_usage``,
``topic_frequency``) that no monitor actually emits, so the detectors
passed in isolation while being dead code in production.  Synchronising
the fixtures with monitor output is what makes these tests load-bearing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackboxrs.core.config import AnomalyThresholds, DeadTopicConfig, FrequencyConfig
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.anomaly_engine.detectors.threshold import ThresholdDetector
from blackboxrs.anomaly_engine.detectors.frequency import FrequencyDetector
from blackboxrs.anomaly_engine.detectors.dead_topic import DeadTopicDetector
from blackboxrs.anomaly_engine.detectors.qos_mismatch import QoSMismatchDetector


# ---------------------------------------------------------------------------
# ThresholdDetector
# ---------------------------------------------------------------------------


class TestThresholdDetector:
    """Test threshold-based anomaly detection.

    Events constructed here mirror SystemMonitor output: ``system.cpu``
    with a ``cpu_percent`` field, ``system.memory`` with
    ``memory_percent``, ``system.gpu`` with ``gpu_temp_c``.
    """

    def _make_cpu_event(self, value: float) -> BlackBoxEvent:
        return BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_percent": value, "cpu_count": 4, "per_cpu_percent": [value]},
        )

    def _make_memory_event(self, value: float) -> BlackBoxEvent:
        return BlackBoxEvent.system_event(
            event_type="system.memory",
            data={
                "memory_percent": value,
                "memory_used_mb": 1.0,
                "memory_total_mb": 2.0,
                "swap_percent": 0.0,
                "swap_used_mb": 0.0,
            },
        )

    def _make_gpu_event(self, temp_c: float) -> BlackBoxEvent:
        return BlackBoxEvent.system_event(
            event_type="system.gpu",
            data={
                "gpu_util_percent": 10.0,
                "gpu_temp_c": temp_c,
                "gpu_memory_used_mb": None,
                "gpu_memory_total_mb": None,
                "power_w": None,
            },
        )

    def test_fires_on_high_cpu(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0, min_consecutive_samples=1))
        result = detector.check(self._make_cpu_event(95.0))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.threshold"
        assert result.data["value"] == 95.0
        assert result.data["threshold"] == 90.0
        assert result.data["metric"] == "cpu_percent"

    def test_does_not_fire_on_normal_cpu(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        assert detector.check(self._make_cpu_event(50.0)) is None

    def test_does_not_fire_at_exact_threshold(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        assert detector.check(self._make_cpu_event(90.0)) is None

    def test_fires_on_high_memory(self):
        detector = ThresholdDetector(AnomalyThresholds(memory_percent=85.0, min_consecutive_samples=1))
        result = detector.check(self._make_memory_event(90.0))
        assert result is not None
        assert result.data["metric"] == "memory_percent"

    def test_fires_on_high_gpu_temp(self):
        detector = ThresholdDetector(AnomalyThresholds(gpu_temp_c=80.0, min_consecutive_samples=1))
        result = detector.check(self._make_gpu_event(95.0))
        assert result is not None
        assert result.data["metric"] == "gpu_temp_c"
        assert result.data["value"] == 95.0

    def test_ignores_non_system_events(self):
        detector = ThresholdDetector(AnomalyThresholds())
        event = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": "/cmd_vel", "frequency_hz": 100.0},
        )
        assert detector.check(event) is None

    def test_ignores_unknown_event_type(self):
        detector = ThresholdDetector(AnomalyThresholds())
        event = BlackBoxEvent.system_event(
            event_type="system.unknown",
            data={"value": 100.0},
        )
        assert detector.check(event) is None

    def test_ignores_missing_metric_key(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=10.0))
        # Event has the right type but no cpu_percent field.
        event = BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_count": 4},
        )
        assert detector.check(event) is None

    def test_name_property(self):
        assert ThresholdDetector(AnomalyThresholds()).name == "threshold"


    # -- Hysteresis (min_consecutive_samples) ---------------------------

    def test_threshold_single_sample_does_not_fire_with_default_min2(self):
        """With min_consecutive_samples=2, the first violating sample is silent."""
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        assert detector.check(self._make_cpu_event(95.0)) is None

    def test_threshold_two_consecutive_violations_fire_on_second(self):
        """Two consecutive violations must fire on exactly the second."""
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        assert detector.check(self._make_cpu_event(95.0)) is None
        result = detector.check(self._make_cpu_event(95.0))
        assert result is not None
        assert result.event_type == "anomaly.threshold"

    def test_threshold_healthy_sample_resets_counter(self):
        """A healthy reading between two violations resets the counter."""
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        assert detector.check(self._make_cpu_event(95.0)) is None
        # Healthy: resets counter.
        assert detector.check(self._make_cpu_event(50.0)) is None
        # Violating again: counter = 1 again, not 2.
        assert detector.check(self._make_cpu_event(95.0)) is None

    def test_threshold_sustained_violations_keep_firing_after_first(self):
        """After the first fire, sustained violations continue to fire."""
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        detector.check(self._make_cpu_event(95.0))  # count=1, silent
        result2 = detector.check(self._make_cpu_event(95.0))  # count=2, fires
        assert result2 is not None
        result3 = detector.check(self._make_cpu_event(95.0))  # count=3, fires
        assert result3 is not None

    def test_threshold_min_consecutive_1_reproduces_v040_behavior(self):
        """min_consecutive_samples=1 fires on the very first violation."""
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0, min_consecutive_samples=1))
        result = detector.check(self._make_cpu_event(95.0))
        assert result is not None


# ---------------------------------------------------------------------------
# FrequencyDetector
# ---------------------------------------------------------------------------


class TestFrequencyDetector:
    """Test frequency-drop anomaly detection against ros.frequency events."""

    def _make_freq_event(self, topic: str, hz: float) -> BlackBoxEvent:
        return BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": topic, "frequency_hz": hz},
        )

    def test_learning_phase_returns_none(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            assert detector.check(self._make_freq_event("/cmd_vel", 10.0)) is None

    def test_fires_on_frequency_drop_after_learning(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0, min_consecutive_samples=1))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        result = detector.check(self._make_freq_event("/cmd_vel", 5.0))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.frequency"

    def test_does_not_fire_within_tolerance(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        assert detector.check(self._make_freq_event("/cmd_vel", 9.0)) is None

    def test_ignores_non_frequency_events(self):
        detector = FrequencyDetector(FrequencyConfig())
        event = BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_percent": 50.0},
        )
        assert detector.check(event) is None

    def test_ignores_old_event_type_string(self):
        """Guard against regression to the old fabricated 'topic_frequency' name."""
        detector = FrequencyDetector(FrequencyConfig())
        event = BlackBoxEvent.ros_event(
            event_type="topic_frequency",  # legacy / wrong
            data={"topic": "/cmd_vel", "frequency_hz": 1.0},
        )
        assert detector.check(event) is None

    def test_independent_baselines_per_topic(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0, min_consecutive_samples=1))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/scan", 20.0))
        assert detector.check(self._make_freq_event("/cmd_vel", 9.0)) is None
        result = detector.check(self._make_freq_event("/scan", 9.0))
        assert result is not None

    def test_name_property(self):
        assert FrequencyDetector(FrequencyConfig()).name == "frequency"


    # -- Hysteresis -----------------------------------------------------

    def test_frequency_single_drop_does_not_fire_with_default_min2(self):
        """First violation is silent with min_consecutive_samples=2 (default)."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        assert detector.check(self._make_freq_event("/cmd_vel", 5.0)) is None

    def test_frequency_two_consecutive_drops_fire_on_second(self):
        """Two consecutive drops must fire on the second."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        assert detector.check(self._make_freq_event("/cmd_vel", 5.0)) is None
        result = detector.check(self._make_freq_event("/cmd_vel", 5.0))
        assert result is not None
        assert result.event_type == "anomaly.frequency"

    def test_frequency_healthy_between_drops_resets_counter(self):
        """A healthy reading between two drops resets the violation counter."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        assert detector.check(self._make_freq_event("/cmd_vel", 5.0)) is None
        # Healthy reset.
        assert detector.check(self._make_freq_event("/cmd_vel", 10.0)) is None
        # Violation again: counter = 1, should not fire.
        assert detector.check(self._make_freq_event("/cmd_vel", 5.0)) is None

    def test_frequency_sustained_rate_collapse_fires_once(self):
        """A real collapse still fires promptly without flooding triggers."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))

        results = [
            detector.check(self._make_freq_event("/cmd_vel", 1.0))
            for _ in range(100)
        ]

        anomalies = [result for result in results if result is not None]
        assert results[0] is None
        assert results[1] is not None
        assert len(anomalies) == 1

    def test_frequency_bursty_boundary_chatter_is_suppressed_until_recovery(self):
        """Bursty rate chatter produces one trigger instead of one per burst."""
        detector = FrequencyDetector(
            FrequencyConfig(
                tolerance_percent=20.0,
                recovery_tolerance_percent=10.0,
                min_consecutive_samples=2,
            )
        )
        for _ in range(10):
            detector.check(self._make_freq_event("/imu", 100.0))

        results = []
        for _ in range(100):
            for hz in (60.0, 60.0, 85.0):
                results.append(detector.check(self._make_freq_event("/imu", hz)))

        assert sum(result is not None for result in results) == 1

        assert detector.check(self._make_freq_event("/imu", 89.9)) is None
        assert detector.check(self._make_freq_event("/imu", 60.0)) is None
        assert detector.check(self._make_freq_event("/imu", 60.0)) is None

        assert detector.check(self._make_freq_event("/imu", 90.0)) is None
        assert detector.check(self._make_freq_event("/imu", 60.0)) is None
        assert detector.check(self._make_freq_event("/imu", 60.0)) is not None

    def test_frequency_min_consecutive_1_reproduces_v040_behavior(self):
        """min_consecutive_samples=1 fires on the first drop."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0, min_consecutive_samples=1))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        result = detector.check(self._make_freq_event("/cmd_vel", 5.0))
        assert result is not None


# ---------------------------------------------------------------------------
# DeadTopicDetector
# ---------------------------------------------------------------------------


class TestDeadTopicDetector:
    """Test dead-topic detection against real ros.frequency events."""

    def _make_topic_event(self, topic: str, ts: datetime | None = None) -> BlackBoxEvent:
        ev = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": topic, "frequency_hz": 10.0},
        )
        if ts is not None:
            ev.timestamp = ts
        return ev

    def test_no_anomaly_while_active(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=5.0))
        assert detector.check(self._make_topic_event("/cmd_vel")) is None

    def test_fires_after_timeout(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        result = detector.check(self._make_topic_event("/scan"))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.dead_topic"
        assert "cmd_vel" in result.data["message"]

    def test_dead_topic_still_fires_with_frequency_hysteresis_enabled(self):
        frequency = FrequencyDetector(FrequencyConfig())
        dead_topic = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))

        for _ in range(10):
            event = self._make_topic_event("/cmd_vel")
            frequency.check(event)
            dead_topic.check(event)

        collapse = self._make_topic_event("/cmd_vel")
        collapse.data["frequency_hz"] = 1.0
        assert frequency.check(collapse) is None
        assert dead_topic.check(collapse) is None
        assert frequency.check(collapse) is not None
        assert dead_topic.check(collapse) is None

        dead_topic._last_seen["/cmd_vel"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        )
        result = dead_topic.check(self._make_topic_event("/scan"))

        assert result is not None
        assert result.event_type == "anomaly.dead_topic"
        assert result.data["topic"] == "/cmd_vel"

    def test_does_not_re_alert_same_topic(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert detector.check(self._make_topic_event("/scan")) is not None
        result = detector.check(self._make_topic_event("/scan"))
        if result is not None:
            assert "cmd_vel" not in result.data.get("metric", "")

    def test_topic_revival_clears_alert(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        detector.check(self._make_topic_event("/scan"))
        detector.check(self._make_topic_event("/cmd_vel"))
        assert "/cmd_vel" not in detector._alerted

    def test_qos_metadata_does_not_refresh_liveness(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        qos_event = BlackBoxEvent.ros_event(
            event_type="ros.qos",
            data={
                "topic": "/cmd_vel",
                "msg_type": "std_msgs/msg/String",
                "publisher_count": 1,
                "subscriber_count": 1,
                "publisher_qos_profiles": [{"reliability": "ReliabilityPolicy.RELIABLE"}],
                "subscriber_qos_profiles": [{"reliability": "ReliabilityPolicy.RELIABLE"}],
            },
        )
        result = detector.check(qos_event)
        assert result is not None
        assert result.event_type == "anomaly.dead_topic"

    def test_name_property(self):
        assert DeadTopicDetector(DeadTopicConfig()).name == "dead_topic"


# ---------------------------------------------------------------------------
# QoSMismatchDetector
# ---------------------------------------------------------------------------


class TestQoSMismatchDetector:
    """Test QoS-mismatch detection against ros.qos events.

    Event payloads here mirror what RosMonitor + RosIntrospector now
    emit: separate publisher_qos_profiles and subscriber_qos_profiles
    lists, each entry shaped like ``introspection._serialise_qos`` output.
    """

    def _make_qos_event(self, pub_qos: list[dict], sub_qos: list[dict]) -> BlackBoxEvent:
        return BlackBoxEvent.ros_event(
            event_type="ros.qos",
            data={
                "topic": "/test_topic",
                "msg_type": "std_msgs/msg/String",
                "publisher_count": len(pub_qos),
                "subscriber_count": len(sub_qos),
                "publisher_qos_profiles": pub_qos,
                "subscriber_qos_profiles": sub_qos,
            },
        )

    def test_fires_on_reliability_mismatch(self):
        detector = QoSMismatchDetector()
        # Publisher BEST_EFFORT but subscriber demands RELIABLE -> incompatible
        result = detector.check(
            self._make_qos_event(
                pub_qos=[{"reliability": "ReliabilityPolicy.BEST_EFFORT",
                          "durability": "DurabilityPolicy.VOLATILE"}],
                sub_qos=[{"reliability": "ReliabilityPolicy.RELIABLE",
                          "durability": "DurabilityPolicy.VOLATILE"}],
            )
        )
        assert result is not None
        assert result.event_type == "anomaly.qos_mismatch"
        assert "reliability" in result.data["message"].lower()

    def test_fires_on_durability_mismatch(self):
        detector = QoSMismatchDetector()
        result = detector.check(
            self._make_qos_event(
                pub_qos=[{"reliability": "ReliabilityPolicy.RELIABLE",
                          "durability": "DurabilityPolicy.VOLATILE"}],
                sub_qos=[{"reliability": "ReliabilityPolicy.RELIABLE",
                          "durability": "DurabilityPolicy.TRANSIENT_LOCAL"}],
            )
        )
        assert result is not None
        assert "durability" in result.data["message"].lower()

    def test_silent_when_compatible(self):
        detector = QoSMismatchDetector()
        result = detector.check(
            self._make_qos_event(
                pub_qos=[{"reliability": "ReliabilityPolicy.RELIABLE",
                          "durability": "DurabilityPolicy.TRANSIENT_LOCAL"}],
                sub_qos=[{"reliability": "ReliabilityPolicy.BEST_EFFORT",
                          "durability": "DurabilityPolicy.VOLATILE"}],
            )
        )
        assert result is None

    def test_silent_when_no_subscribers(self):
        detector = QoSMismatchDetector()
        result = detector.check(
            self._make_qos_event(
                pub_qos=[{"reliability": "ReliabilityPolicy.RELIABLE",
                          "durability": "DurabilityPolicy.VOLATILE"}],
                sub_qos=[],
            )
        )
        assert result is None

    def test_ignores_other_event_types(self):
        detector = QoSMismatchDetector()
        event = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": "/x", "frequency_hz": 10.0},
        )
        assert detector.check(event) is None

    def test_name_property(self):
        assert QoSMismatchDetector().name == "qos_mismatch"
