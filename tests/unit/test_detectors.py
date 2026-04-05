"""Tests for blackboxrs.anomaly_engine.detectors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackboxrs.core.config import AnomalyThresholds, DeadTopicConfig, FrequencyConfig
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.anomaly_engine.detectors.threshold import ThresholdDetector
from blackboxrs.anomaly_engine.detectors.frequency import FrequencyDetector
from blackboxrs.anomaly_engine.detectors.dead_topic import DeadTopicDetector


# ---------------------------------------------------------------------------
# ThresholdDetector
# ---------------------------------------------------------------------------


class TestThresholdDetector:
    """Test threshold-based anomaly detection."""

    def _make_cpu_event(self, value: float) -> BlackBoxEvent:
        return BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="system_monitor",
            event_type="cpu_usage",
            severity="info",
            data={"value": value, "unit": "%"},
        )

    def test_fires_on_high_cpu(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        result = detector.check(self._make_cpu_event(95.0))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly_threshold"
        assert result.data["value"] == 95.0
        assert result.data["threshold"] == 90.0

    def test_does_not_fire_on_normal_cpu(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        result = detector.check(self._make_cpu_event(50.0))
        assert result is None

    def test_does_not_fire_at_exact_threshold(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        result = detector.check(self._make_cpu_event(90.0))
        assert result is None

    def test_fires_on_high_memory(self):
        detector = ThresholdDetector(AnomalyThresholds(memory_percent=85.0))
        event = BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="system_monitor",
            event_type="memory_usage",
            severity="info",
            data={"value": 90.0, "unit": "%"},
        )
        result = detector.check(event)
        assert result is not None
        assert result.data["metric"] == "memory_usage"

    def test_ignores_non_system_events(self):
        detector = ThresholdDetector(AnomalyThresholds())
        event = BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="ros_monitor",
            event_type="topic_frequency",
            severity="info",
            data={"value": 100.0},
        )
        assert detector.check(event) is None

    def test_ignores_unknown_event_type(self):
        detector = ThresholdDetector(AnomalyThresholds())
        event = BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="system_monitor",
            event_type="unknown_metric",
            severity="info",
            data={"value": 100.0},
        )
        assert detector.check(event) is None

    def test_name_property(self):
        detector = ThresholdDetector(AnomalyThresholds())
        assert detector.name == "threshold"


# ---------------------------------------------------------------------------
# FrequencyDetector
# ---------------------------------------------------------------------------


class TestFrequencyDetector:
    """Test frequency-drop anomaly detection."""

    def _make_freq_event(self, topic: str, hz: float) -> BlackBoxEvent:
        return BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="ros_monitor",
            event_type="topic_frequency",
            severity="info",
            data={"topic": topic, "frequency_hz": hz},
        )

    def test_learning_phase_returns_none(self):
        """During the first 10 samples, no anomaly should fire."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            result = detector.check(self._make_freq_event("/cmd_vel", 10.0))
            assert result is None

    def test_fires_on_frequency_drop_after_learning(self):
        """After 10 baseline samples at 10Hz, a drop to 5Hz should fire."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        # Learning phase: 10 samples at 10 Hz
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        # Now drop to 5 Hz -- well below 80% of 10Hz = 8Hz floor
        result = detector.check(self._make_freq_event("/cmd_vel", 5.0))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly_frequency"

    def test_does_not_fire_within_tolerance(self):
        """A frequency within tolerance should not trigger."""
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        # 9 Hz is above 8 Hz floor (80% of 10)
        result = detector.check(self._make_freq_event("/cmd_vel", 9.0))
        assert result is None

    def test_ignores_non_frequency_events(self):
        detector = FrequencyDetector(FrequencyConfig())
        event = BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="system_monitor",
            event_type="cpu_usage",
            data={"value": 50.0},
        )
        assert detector.check(event) is None

    def test_independent_baselines_per_topic(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))
        # Learn /cmd_vel at 10 Hz
        for _ in range(10):
            detector.check(self._make_freq_event("/cmd_vel", 10.0))
        # Learn /scan at 20 Hz
        for _ in range(10):
            detector.check(self._make_freq_event("/scan", 20.0))
        # /cmd_vel at 9 Hz is fine, /scan at 9 Hz should trigger
        assert detector.check(self._make_freq_event("/cmd_vel", 9.0)) is None
        result = detector.check(self._make_freq_event("/scan", 9.0))
        assert result is not None

    def test_name_property(self):
        detector = FrequencyDetector(FrequencyConfig())
        assert detector.name == "frequency"


# ---------------------------------------------------------------------------
# DeadTopicDetector
# ---------------------------------------------------------------------------


class TestDeadTopicDetector:
    """Test dead-topic anomaly detection."""

    def _make_topic_event(self, topic: str, ts: datetime | None = None) -> BlackBoxEvent:
        return BlackBoxEvent(
            timestamp=ts or datetime.now(timezone.utc),
            source="ros_monitor",
            event_type="topic_frequency",
            severity="info",
            data={"topic": topic, "frequency_hz": 10.0},
        )

    def test_no_anomaly_while_active(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=5.0))
        result = detector.check(self._make_topic_event("/cmd_vel"))
        assert result is None

    def test_fires_after_timeout(self):
        """Simulate a dead topic by manipulating the detector's internal state."""
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        # Register the topic
        detector.check(self._make_topic_event("/cmd_vel"))
        # Manually backdate the last-seen timestamp to simulate timeout
        old_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        detector._last_seen["/cmd_vel"] = old_time
        # Next check with a different topic event should detect /cmd_vel as dead
        trigger_event = self._make_topic_event("/scan")
        result = detector.check(trigger_event)
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly_dead_topic"
        assert "cmd_vel" in result.data["message"]

    def test_does_not_re_alert_same_topic(self):
        """Once a dead topic is alerted, it should not fire again."""
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        # First check fires
        trigger = self._make_topic_event("/scan")
        assert detector.check(trigger) is not None
        # Second check for same dead topic should not fire
        trigger2 = self._make_topic_event("/scan")
        result = detector.check(trigger2)
        # Could be None (no new dead topics) or alert for /scan going dead,
        # but /cmd_vel should not alert again
        if result is not None:
            assert "cmd_vel" not in result.data.get("metric", "")

    def test_topic_revival_clears_alert(self):
        """When a dead topic publishes again, the alert state is cleared."""
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))
        detector.check(self._make_topic_event("/cmd_vel"))
        detector._last_seen["/cmd_vel"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        trigger = self._make_topic_event("/scan")
        detector.check(trigger)  # fires for /cmd_vel
        # /cmd_vel comes back alive
        detector.check(self._make_topic_event("/cmd_vel"))
        assert "/cmd_vel" not in detector._alerted

    def test_name_property(self):
        detector = DeadTopicDetector(DeadTopicConfig())
        assert detector.name == "dead_topic"
