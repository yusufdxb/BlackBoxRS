"""Synthetic failure-scenario tests for BlackBoxRS.

These tests exercise realistic failure-detection pipelines using real
components (no mocking of core code).  Every event constructed here
matches the actual event_type strings and data payloads emitted by
:class:`SystemMonitor` and :class:`RosMonitor`, so the tests fail loudly
if the monitor → detector contract drifts.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    """Verify a CPU spike triggers ThresholdDetector via the real contract."""

    def test_cpu_spike_detected(self):
        detector = ThresholdDetector(AnomalyThresholds(cpu_percent=90.0))
        anomalies = []
        for cpu_val in [50.0, 60.0, 75.0, 92.0, 95.0, 98.0, 85.0, 70.0]:
            event = BlackBoxEvent.system_event(
                event_type="system.cpu",
                data={"cpu_percent": cpu_val, "cpu_count": 4, "per_cpu_percent": [cpu_val]},
            )
            result = detector.check(event)
            if result is not None:
                anomalies.append(result)

        assert len(anomalies) == 2
        for a in anomalies:
            assert a.source == "anomaly_engine"
            assert a.event_type == "anomaly.threshold"
            assert a.data["metric"] == "cpu_percent"
            assert a.data["value"] > 90.0


class TestTopicDeathDetected:
    """A previously-active topic going silent triggers DeadTopicDetector."""

    def test_topic_death_detected(self):
        detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=1.0))

        for _ in range(5):
            event = BlackBoxEvent.ros_event(
                event_type="ros.frequency",
                data={"topic": "/lidar", "frequency_hz": 20.0},
            )
            assert detector.check(event) is None

        detector._last_seen["/lidar"] = datetime.now(timezone.utc) - timedelta(seconds=3)

        trigger = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": "/imu", "frequency_hz": 100.0},
        )
        result = detector.check(trigger)
        assert result is not None
        assert result.event_type == "anomaly.dead_topic"
        assert "/lidar" in result.data["message"]


class TestFrequencyDropDetected:
    """A measured frequency drop triggers FrequencyDetector."""

    def test_frequency_drop_detected(self):
        detector = FrequencyDetector(FrequencyConfig(tolerance_percent=20.0))

        for _ in range(10):
            event = BlackBoxEvent.ros_event(
                event_type="ros.frequency",
                data={"topic": "/odom", "frequency_hz": 30.0},
            )
            detector.check(event)

        anomalies = []
        for hz in [28.0, 25.0, 22.0, 18.0, 12.0, 5.0]:
            event = BlackBoxEvent.ros_event(
                event_type="ros.frequency",
                data={"topic": "/odom", "frequency_hz": hz},
            )
            result = detector.check(event)
            if result is not None:
                anomalies.append(result)

        # Floor = 30 * 0.8 = 24 Hz.  With min_consecutive_samples=2, 18, 12, 5 fire (22 is count=1).
        assert len(anomalies) == 3
        for a in anomalies:
            assert a.event_type == "anomaly.frequency"
            assert a.data["value"] < 24.0


class TestEventBusToAnomalyToLog:
    """End-to-end: bus -> AnomalyEngine -> LoggingPipeline -> JSONL."""

    def test_threshold_anomaly_propagates_to_disk(self, tmp_path: Path):
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

        # Engine before pipeline so it observes events from t=0.
        engine = AnomalyEngine(bus, config.anomaly_engine, session)
        pipeline = LoggingPipeline(bus, config, session)

        engine.start()
        pipeline.start()

        try:
            for val in [40.0, 50.0, 60.0]:
                bus.publish(BlackBoxEvent.system_event(
                    event_type="system.cpu",
                    data={"cpu_percent": val, "cpu_count": 4, "per_cpu_percent": [val]},
                ))
                time.sleep(0.02)

            # Two consecutive spikes to satisfy min_consecutive_samples=2.
            for _ in range(2):
                bus.publish(BlackBoxEvent.system_event(
                    event_type="system.cpu",
                    data={"cpu_percent": 95.0, "cpu_count": 4, "per_cpu_percent": [95.0]},
                ))
                time.sleep(0.02)

            # Allow detector + writer threads to drain.
            time.sleep(1.0)
        finally:
            pipeline.stop()
            engine.stop()

        all_events = list(LogReader(log_dir).read_all())
        # 4 system events plus at least one anomaly.
        assert len(all_events) >= 5

        # Session metadata enrichment is real, not mocked.
        assert any("session_id" in e.metadata for e in all_events)

        anomalies = [e for e in all_events if e.source == "anomaly_engine"]
        assert len(anomalies) >= 1
        assert anomalies[0].event_type == "anomaly.threshold"
        assert anomalies[0].data["metric"] == "cpu_percent"
        assert anomalies[0].data["value"] == 95.0
