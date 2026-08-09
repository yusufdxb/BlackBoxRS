"""Unified event schemas for BlackBoxRS.

All data flowing through the BlackBoxRS pipeline is represented as a
:class:`BlackBoxEvent`.  Typed data models for common event payloads
are also provided so that producers and consumers can share a strict
contract without passing raw dicts around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from blackboxrs.core.clock import Clock


# ---------------------------------------------------------------------------
# Typed payload models
# ---------------------------------------------------------------------------


class TopicFrequencyData(BaseModel):
    """Payload for topic frequency measurements."""

    topic: str = Field(..., description="Fully qualified ROS topic name.")
    frequency_hz: float = Field(..., description="Measured publish frequency in Hz.")
    interval_ms: float | None = Field(
        default=None,
        description="Estimated mean inter-message interval in milliseconds.",
    )


class SystemMetricData(BaseModel):
    """Payload for system-level metric readings."""

    metric: str = Field(..., description="Metric identifier, e.g. 'cpu_percent'.")
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
    """Payload for a ROS QoS snapshot."""

    topic: str = Field(..., description="Fully qualified ROS topic name.")
    msg_type: str = Field(..., description="ROS interface type for the topic.")
    publisher_count: int = Field(..., description="Number of discovered publishers.")
    subscriber_count: int = Field(..., description="Number of discovered subscribers.")
    publisher_qos_profiles: list[dict[str, Any]] = Field(
        ..., description="QoS settings on the publisher side."
    )
    subscriber_qos_profiles: list[dict[str, Any]] = Field(
        ..., description="QoS settings on the subscriber side."
    )


class CaptureQuality(BaseModel):
    """Evidence-integrity summary supplied by a capture backend."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["python", "cpp"]
    completeness: Literal["complete", "incomplete", "unknown"] = "unknown"
    received: int | None = Field(default=None, ge=0)
    captured: int | None = Field(default=None, ge=0)
    committed: int | None = Field(default=None, ge=0)
    durable: int | None = Field(default=None, ge=0)
    dropped: int | None = Field(default=None, ge=0)
    bytes_captured: int | None = Field(default=None, ge=0)
    bytes_dropped: int | None = Field(default=None, ge=0)
    drop_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    peak_queue_utilization: float | None = Field(default=None, ge=0.0, le=1.0)
    storage_errors: list[str] = Field(default_factory=list)
    clock_anomalies: int = Field(default=0, ge=0)
    graph_coverage_faults: int = Field(default=0, ge=0)
    subscription_failures: int = Field(default=0, ge=0)
    runtime_callback_faults: int = Field(default=0, ge=0)
    rmw_messages_lost: int = Field(default=0, ge=0)
    delivery_observability_faults: int = Field(default=0, ge=0)
    best_effort_topics: int = Field(default=0, ge=0)
    topic_coverage_truncated: bool = False
    node_coverage_truncated: bool = False
    delivery_scope: str | None = None
    capture_start: datetime | None = None
    capture_end: datetime | None = None
    monotonic_start_ns: int | None = Field(default=None, ge=0)
    monotonic_end_ns: int | None = Field(default=None, ge=0)
    clean: bool | None = None
    recovered: bool = False
    recovery_discarded_tail_bytes: int | None = Field(default=None, ge=0)
    recovery_corruption_reason: str | None = None
    segments: int = Field(default=0, ge=0)
    retained_events: int | None = Field(default=None, ge=0)
    retention_evicted_segments: int = Field(default=0, ge=0)
    retention_evicted_events: int = Field(default=0, ge=0)
    retention_evicted_bytes: int = Field(default=0, ge=0)
    history_complete: bool | None = None
    post_window_elapsed: bool | None = None
    malformed_records: int = Field(default=0, ge=0)
    sequence_gaps: list[tuple[int, int]] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified event envelope
# ---------------------------------------------------------------------------

_SOURCE_TYPE = Literal[
    "ros_monitor",
    "system_monitor",
    "anomaly_engine",
    "rosbag_recorder",
    "native_capture",
]
_SEVERITY_TYPE = Literal["debug", "info", "warning", "error", "critical"]


class BlackBoxEvent(BaseModel):
    """Canonical event envelope for every datum in the BlackBoxRS pipeline.

    All monitors, detectors, and loggers communicate through this single
    schema.  Helper constructors are provided so callers don't have to
    fill in boilerplate fields every time.
    """

    timestamp: datetime = Field(..., description="UTC timestamp in ISO 8601 format.")
    source: _SOURCE_TYPE = Field(..., description="Subsystem that produced this event.")
    event_type: str = Field(
        ...,
        description="Specific event kind, e.g. 'ros.frequency', 'system.cpu'.",
    )
    severity: _SEVERITY_TYPE = Field(default="info", description="Log-style severity level.")
    data: dict[str, Any] = Field(default_factory=dict, description="Metric-specific payload.")
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
            event_type: Specific event kind (e.g. ``"ros.frequency"``).
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
            event_type: Specific event kind (e.g. ``"system.cpu"``).
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
            event_type: Specific event kind (e.g. ``"anomaly.threshold"``).
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

    @classmethod
    def recorder_event(
        cls,
        event_type: str,
        data: dict[str, Any],
        severity: _SEVERITY_TYPE = "info",
        **meta: Any,
    ) -> BlackBoxEvent:
        """Create an event originating from the rosbag recorder."""
        return cls(
            timestamp=Clock.now(),
            source="rosbag_recorder",
            event_type=event_type,
            severity=severity,
            data=data,
            metadata=dict(meta),
        )

    @classmethod
    def native_event(
        cls,
        event_type: str,
        data: dict[str, Any],
        severity: _SEVERITY_TYPE = "info",
        **meta: Any,
    ) -> BlackBoxEvent:
        """Create an event adapted from the native capture stream."""
        return cls(
            timestamp=Clock.now(),
            source="native_capture",
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
