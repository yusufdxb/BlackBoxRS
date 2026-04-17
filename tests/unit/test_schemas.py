"""Tests for blackboxrs.core.schemas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from blackboxrs.core.schemas import (
    AnomalyData,
    BlackBoxEvent,
    QoSProfileData,
    SystemMetricData,
    TopicFrequencyData,
)


class TestBlackBoxEventCreation:
    """Test direct construction of BlackBoxEvent.

    Event-type strings follow the ``<subsystem>.<specific>`` convention
    emitted by the runtime producers (see ``blackboxrs.ros_monitor`` and
    ``blackboxrs.system_monitor``).  These tests pin the runtime contract
    so drift in either direction shows up immediately.
    """

    def test_create_with_all_fields(self):
        event = BlackBoxEvent(
            timestamp=datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc),
            source="ros_monitor",
            event_type="ros.frequency",
            severity="info",
            data={"topic": "/cmd_vel", "frequency_hz": 10.0},
            metadata={"session_id": "abc123"},
        )
        assert event.source == "ros_monitor"
        assert event.event_type == "ros.frequency"
        assert event.severity == "info"
        assert event.data["topic"] == "/cmd_vel"
        assert event.metadata["session_id"] == "abc123"

    def test_default_severity_is_info(self):
        event = BlackBoxEvent(
            timestamp=datetime(2026, 4, 5, tzinfo=timezone.utc),
            source="system_monitor",
            event_type="system.cpu",
        )
        assert event.severity == "info"

    def test_default_data_and_metadata_are_empty_dicts(self):
        event = BlackBoxEvent(
            timestamp=datetime(2026, 4, 5, tzinfo=timezone.utc),
            source="system_monitor",
            event_type="system.cpu",
        )
        assert event.data == {}
        assert event.metadata == {}

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            BlackBoxEvent(
                timestamp=datetime(2026, 4, 5, tzinfo=timezone.utc),
                source="invalid_source",
                event_type="test",
            )

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValidationError):
            BlackBoxEvent(
                timestamp=datetime(2026, 4, 5, tzinfo=timezone.utc),
                source="ros_monitor",
                event_type="test",
                severity="fatal",
            )


class TestBlackBoxEventConstructors:
    """Test the ros_event(), system_event(), anomaly_event() classmethods."""

    def test_ros_event(self):
        event = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": "/scan", "frequency_hz": 20.0},
            node="/lidar_driver",
        )
        assert event.source == "ros_monitor"
        assert event.event_type == "ros.frequency"
        assert event.severity == "info"
        assert event.data["topic"] == "/scan"
        assert event.metadata["node"] == "/lidar_driver"
        assert event.timestamp.tzinfo is not None

    def test_system_event(self):
        event = BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_percent": 55.0, "cpu_count": 8},
        )
        assert event.source == "system_monitor"
        assert event.event_type == "system.cpu"
        assert event.severity == "info"

    def test_anomaly_event_default_severity_is_warning(self):
        event = BlackBoxEvent.anomaly_event(
            event_type="anomaly.threshold",
            data={"detector": "threshold", "metric": "cpu_percent", "value": 95.0,
                  "threshold": 90.0, "message": "test"},
        )
        assert event.source == "anomaly_engine"
        assert event.severity == "warning"

    def test_ros_event_custom_severity(self):
        event = BlackBoxEvent.ros_event(
            event_type="ros.qos",
            data={},
            severity="error",
        )
        assert event.severity == "error"


class TestBlackBoxEventSerialization:
    """Test to_jsonl() and from_jsonl() round-trip."""

    def test_round_trip(self, sample_ros_event: BlackBoxEvent):
        jsonl = sample_ros_event.to_jsonl()
        restored = BlackBoxEvent.from_jsonl(jsonl)
        assert restored.source == sample_ros_event.source
        assert restored.event_type == sample_ros_event.event_type
        assert restored.severity == sample_ros_event.severity
        assert restored.data == sample_ros_event.data
        assert restored.timestamp == sample_ros_event.timestamp

    def test_to_jsonl_is_single_line(self, sample_ros_event: BlackBoxEvent):
        jsonl = sample_ros_event.to_jsonl()
        assert "\n" not in jsonl

    def test_from_jsonl_strips_whitespace(self, sample_ros_event: BlackBoxEvent):
        jsonl = "  " + sample_ros_event.to_jsonl() + "  \n"
        restored = BlackBoxEvent.from_jsonl(jsonl)
        assert restored.event_type == sample_ros_event.event_type

    def test_from_jsonl_bad_json_raises(self):
        with pytest.raises(Exception):
            BlackBoxEvent.from_jsonl("not json at all")


class TestTypedDataModels:
    """Test the typed payload models."""

    def test_topic_frequency_data(self):
        data = TopicFrequencyData(topic="/cmd_vel", frequency_hz=10.0, expected_hz=10.0)
        assert data.topic == "/cmd_vel"
        assert data.frequency_hz == 10.0

    def test_system_metric_data(self):
        data = SystemMetricData(metric="cpu_percent", value=55.0, unit="%")
        assert data.metric == "cpu_percent"
        assert data.value == 55.0

    def test_anomaly_data(self):
        data = AnomalyData(
            detector="threshold",
            metric="cpu_percent",
            value=95.0,
            threshold=90.0,
            message="CPU too high",
        )
        assert data.detector == "threshold"
        assert data.value == 95.0

    def test_qos_profile_data(self):
        data = QoSProfileData(
            topic="/scan",
            publisher_qos={"reliability": "reliable"},
            subscriber_qos={"reliability": "best_effort"},
            compatible=False,
        )
        assert data.compatible is False
        assert data.publisher_qos["reliability"] == "reliable"

    def test_topic_frequency_data_missing_field_raises(self):
        with pytest.raises(ValidationError):
            TopicFrequencyData(topic="/cmd_vel", frequency_hz=10.0)  # type: ignore[call-arg]
