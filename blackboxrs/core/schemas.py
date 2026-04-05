"""Unified event schemas for BlackBoxRS.

All data flowing through the BlackBoxRS pipeline is represented as a
:class:`BlackBoxEvent`.  Typed data models for common event payloads
are also provided so that producers and consumers can share a strict
contract without passing raw dicts around.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from blackboxrs.core.clock import Clock


# ---------------------------------------------------------------------------
# Typed payload models
# ---------------------------------------------------------------------------


class TopicFrequencyData(BaseModel):
    """Payload for topic frequency measurements."""

    topic: str = Field(..., description="Fully qualified ROS topic name.")
    frequency_hz: float = Field(..., description="Measured publish frequency in Hz.")
    expected_hz: float = Field(..., description="Expected publish frequency in Hz.")


class SystemMetricData(BaseModel):
    """Payload for system-level metric readings."""

    metric: str = Field(..., description="Metric identifier, e.g. 'cpu_usage'.")
    value: float = Field(..., description="Measured value.")
    unit: str = Field(..., description="Unit of measurement, e.g. '%', 'C', 'MB'.")


class AnomalyData(BaseModel):
    """Payload emitted when an anomaly detector fires."""

    detector: str = Field(..., description="Name of the anomaly detector.")
    metric: str = Field(..., description="Metric that triggered the anomaly.")
    value: float = Field(..., description="Observed value at trigger time.")
    threshold: float = Field(..., description="Configured threshold that was exceeded.")
    message: str = Field(..., description="Human-readable anomaly description.")


class QoSProfileData(BaseModel):
    """Payload for QoS compatibility reports between pub/sub pairs."""

    topic: str = Field(..., description="Fully qualified ROS topic name.")
    publisher_qos: dict[str, Any] = Field(
        ..., description="QoS settings on the publisher side."
    )
    subscriber_qos: dict[str, Any] = Field(
        ..., description="QoS settings on the subscriber side."
    )
    compatible: bool = Field(
        ..., description="Whether publisher and subscriber QoS profiles are compatible."
    )


# ---------------------------------------------------------------------------
# Unified event envelope
# ---------------------------------------------------------------------------

_SOURCE_TYPE = Literal["ros_monitor", "system_monitor", "anomaly_engine"]
_SEVERITY_TYPE = Literal["debug", "info", "warning", "error", "critical"]


class BlackBoxEvent(BaseModel):
    """Canonical event envelope for every datum in the BlackBoxRS pipeline.

    All monitors, detectors, and loggers communicate through this single
    schema.  Helper constructors are provided so callers don't have to
    fill in boilerplate fields every time.
    """

    timestamp: datetime = Field(
        ..., description="UTC timestamp in ISO 8601 format."
    )
    source: _SOURCE_TYPE = Field(
        ..., description="Subsystem that produced this event."
    )
    event_type: str = Field(
        ...,
        description="Specific event kind, e.g. 'topic_frequency', 'cpu_usage'.",
    )
    severity: _SEVERITY_TYPE = Field(
        default="info", description="Log-style severity level."
    )
    data: dict[str, Any] = Field(
        default_factory=dict, description="Metric-specific payload."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Contextual metadata: hostname, session_id, node name, etc.",
    )

    # -- Helper constructors ------------------------------------------------

    @classmethod
    def ros_event(
        cls,
        event_type: str,
        data: dict[str, Any],
        severity: _SEVERITY_TYPE = "info",
        **meta: Any,
    ) -> BlackBoxEvent:
        """Create an event originating from the ROS monitor.

        Args:
            event_type: Specific event kind (e.g. ``"topic_frequency"``).
            data: Metric payload dictionary.
            severity: Severity level.
            **meta: Additional metadata key-value pairs.

        Returns:
            A fully populated :class:`BlackBoxEvent`.
        """
        return cls(
            timestamp=Clock.now(),
            source="ros_monitor",
            event_type=event_type,
            severity=severity,
            data=data,
            metadata=dict(meta),
        )

    @classmethod
    def system_event(
        cls,
        event_type: str,
        data: dict[str, Any],
        severity: _SEVERITY_TYPE = "info",
        **meta: Any,
    ) -> BlackBoxEvent:
        """Create an event originating from the system monitor.

        Args:
            event_type: Specific event kind (e.g. ``"cpu_usage"``).
            data: Metric payload dictionary.
            severity: Severity level.
            **meta: Additional metadata key-value pairs.

        Returns:
            A fully populated :class:`BlackBoxEvent`.
        """
        return cls(
            timestamp=Clock.now(),
            source="system_monitor",
            event_type=event_type,
            severity=severity,
            data=data,
            metadata=dict(meta),
        )

    @classmethod
    def anomaly_event(
        cls,
        event_type: str,
        data: dict[str, Any],
        severity: _SEVERITY_TYPE = "warning",
        **meta: Any,
    ) -> BlackBoxEvent:
        """Create an event originating from the anomaly engine.

        Args:
            event_type: Specific event kind (e.g. ``"anomaly_threshold"``).
            data: Anomaly payload dictionary.
            severity: Severity level.
            **meta: Additional metadata key-value pairs.

        Returns:
            A fully populated :class:`BlackBoxEvent`.
        """
        return cls(
            timestamp=Clock.now(),
            source="anomaly_engine",
            event_type=event_type,
            severity=severity,
            data=data,
            metadata=dict(meta),
        )

    # -- Serialization ------------------------------------------------------

    def to_jsonl(self) -> str:
        """Serialize the event to a single-line JSON string.

        Returns:
            A compact JSON string with no trailing newline.
        """
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> BlackBoxEvent:
        """Deserialize an event from a single-line JSON string.

        Args:
            line: A JSON string (as produced by :meth:`to_jsonl`).

        Returns:
            A validated :class:`BlackBoxEvent` instance.

        Raises:
            pydantic.ValidationError: If the JSON does not match the schema.
            json.JSONDecodeError: If *line* is not valid JSON.
        """
        return cls.model_validate_json(line.strip())
