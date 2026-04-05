"""Synthetic failure scenario tests for BlackBoxRS.

These tests exercise realistic failure detection pipelines using
real components (no mocking of core code).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty

from blackboxrs.core.config import (
    AnomalyEngineConfig,
    AnomalyThresholds,
    BlackBoxConfig,
    DeadTopicConfig,
    FrequencyConfig,
)
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session
from blackboxrs.anomaly_engine.detectors.threshold import ThresholdDetector
from blackboxrs.anomaly_engine.detectors.frequency import FrequencyDetector
from blackboxrs.anomaly_engine.detectors.dead_topic import DeadTopicDetector
from blackboxrs.anomaly_engine.engine import AnomalyEngine
from blackboxrs.logging.pipeline import LoggingPipeline
from blackboxrs.logging.reader import LogReader


class TestCpuSpikeDetected:
    """Verify that a CPU spike triggers the ThresholdDetector."""

    def test_cpu_spike_detected(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        # Simulate a burst of high-CPU events
        anomalies = []
        for cpu_val in [50.0, 60.0, 75.0, 92.0, 95.0, 98.0, 85.0, 70.0]:
            event = BlackBoxEvent.system_event(
                event_type="cpu_usage",
                data={"value": cpu_val, "unit": "%"},
            )
            result = detector.check(event)
            if result is not None:
                anomalies.append(result)

        # Should have fired for 92, 95, 98
        assert len(anomalies) == 3
        for a in anomalies:
            assert a.source == "anomaly_engine"
            assert a.data["value"] > 90.0


class TestTopicDeathDetected:
    """Verify that a topic going silent triggers DeadTopicDetector."""

    def test_topic_death_detected(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))

        # Topic is alive
        for _ in range(5):
            event = BlackBoxEvent.ros_event(
                event_type="topic_frequency",
                data={"topic": "/lidar", "frequency_hz": 20.0},
            )
            result = detector.check(event)
            assert result is None

        # Simulate time passing: backdate the last-seen timestamp
        detector._last_seen["/lidar"] = datetime.now(timezone.utc) - timedelta(seconds=3)

        # A different topic event triggers the dead-topic scan
        trigger = BlackBoxEvent.ros_event(
            event_type="topic_frequency",
            data={"topic": "/imu", "frequency_hz": 100.0},
        )
        result = detector.check(trigger)
        assert result is not None
        assert result.event_type == "anomaly_dead_topic"
        assert "/lidar" in result.data["message"]


class TestFrequencyDropDetected:
    """Verify that a frequency drop triggers FrequencyDetector."""

    def test_frequency_drop_detected(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))

        # Learning phase: 10 samples at 30 Hz
        for _ in range(10):
            event = BlackBoxEvent.ros_event(
                event_type="topic_frequency",
                data={"topic": "/odom", "frequency_hz": 30.0},
            )
            detector.check(event)

        # Gradual frequency drop
        anomalies = []
        for hz in [28.0, 25.0, 22.0, 18.0, 12.0, 5.0]:
            event = BlackBoxEvent.ros_event(
                event_type="topic_frequency",
                data={"topic": "/odom", "frequency_hz": hz},
            )
            result = detector.check(event)
            if result is not None:
                anomalies.append(result)

        # Floor = 30 * 0.8 = 24 Hz. Values 22, 18, 12, 5 should fire.
        assert len(anomalies) == 4
        for a in anomalies:
            assert a.event_type == "anomaly_frequency"
            assert a.data["value"] < 24.0


class TestFullPipeline:
    """End-to-end: EventBus + LoggingPipeline + AnomalyEngine."""

    def test_full_pipeline(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        bus = EventBus()
        session = Session()
        config = BlackBoxConfig(
            log_dir=str(log_dir),
            log_rotation_mb=1,
            log_max_files=5,
            anomaly_engine=AnomalyEngineConfig(
                thresholds=AnomalyThresholds(cpu_percent=80.0),
            ),
        )

        # Start the logging pipeline and anomaly engine
        pipeline = LoggingPipeline(bus, config, session)
        engine = AnomalyEngine(bus, config.anomaly_engine, session)

        pipeline.start()
        engine.start()

        try:
            # Publish normal events
            for val in [40.0, 50.0, 60.0]:
                bus.publish(BlackBoxEvent.system_event(
                    event_type="cpu_usage",
                    data={"value": val, "unit": "%"},
                ))
                time.sleep(0.05)

            # Publish a spike that should trigger the threshold detector
            bus.publish(BlackBoxEvent.system_event(
                event_type="cpu_usage",
                data={"value": 95.0, "unit": "%"},
            ))

            # Give the pipeline threads time to process
            time.sleep(1.0)

        finally:
            engine.stop()
            pipeline.stop()

        # Verify logs were written
        reader = LogReader(log_dir)
        all_events = list(reader.read_all())
        assert len(all_events) >= 4  # at least the 4 system events

        # Check that at least some events have session metadata
        events_with_session = [
            e for e in all_events if "session_id" in e.metadata
        ]
        assert len(events_with_session) > 0

        # Check that an anomaly event was generated and logged
        anomaly_events = [
            e for e in all_events if e.source == "anomaly_engine"
        ]
        assert len(anomaly_events) >= 1
        assert anomaly_events[0].event_type == "anomaly_threshold"
        assert anomaly_events[0].data["value"] == 95.0
