"""Read native BlackBoxRS MCAP sessions without deserializing ROS payloads.

The native recorder owns high-rate CDR ingestion. This module is the narrow
compatibility seam into the Python incident pipeline: it validates versioned
control records and segment sidecars, exposes lightweight native records, and
projects metadata-only :class:`BlackBoxEvent` objects. Raw CDR bytes stay in
MCAP and are represented by stable evidence references.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from blackboxrs.core.schemas import BlackBoxEvent, CaptureQuality


logger = logging.getLogger(__name__)

CONTROL_TOPIC = "/blackboxrs/events"
CONTROL_SCHEMA = "blackboxrs.capture_event.v1"
SEGMENT_SCHEMA = "blackboxrs.capture_segment.v1"
SESSION_SCHEMA = "blackboxrs.capture_session.v1"
QUALITY_SCHEMA = "blackboxrs.capture_quality.v1"
INCIDENT_CAPTURE_SCHEMA = "blackboxrs.incident_capture.v1"
CURRENT_CAPTURE_SCHEMA = "blackboxrs.current_capture.v1"

SERIALIZED_MESSAGE = 1 << 0
ROS_TIME_VALID = 1 << 16
_UINT32_MODULUS = 1 << 32
_KNOWN_CONTROL_KINDS = {"graph", "drop", "trigger", "clock", "status", "storage"}
_VALID_SOURCES = {
    "ros_monitor",
    "system_monitor",
    "anomaly_engine",
    "rosbag_recorder",
    "native_capture",
}
_VALID_SEVERITIES = {"debug", "info", "warning", "error", "critical"}
_NATIVE_TRIGGER_CODES = {
    1: ("dead_topic", "DeadTopicDetector", "anomaly.dead_topic", "ros"),
    2: ("rate_low", "FrequencyDetector", "anomaly.frequency", "ros"),
    3: ("rate_high", "FrequencyDetector", "anomaly.frequency", "ros"),
    4: (
        "queue_high_watermark",
        "NativeQueuePressureTrigger",
        "capture.queue_pressure",
        "recorder",
    ),
    5: (
        "queue_overflow",
        "NativeQueuePressureTrigger",
        "capture.queue_overflow",
        "recorder",
    ),
    6: (
        "payload_exhausted",
        "NativePayloadTrigger",
        "capture.payload_exhausted",
        "recorder",
    ),
    7: (
        "writer_lag",
        "NativeWriterLagTrigger",
        "capture.writer_lag",
        "recorder",
    ),
    8: (
        "storage_fault",
        "NativeStorageTrigger",
        "capture.storage_fault",
        "recorder",
    ),
    9: ("clock_backward", "ClockSkewDetector", "anomaly.clock_skew", "ros"),
    10: ("clock_forward", "ClockSkewDetector", "anomaly.clock_skew", "ros"),
}
_NATIVE_SEVERITIES = {0: "info", 1: "warning", 2: "error", 3: "critical"}


class NativeCaptureError(RuntimeError):
    """Base error for native capture ingestion."""


class NativeCaptureDependencyError(NativeCaptureError):
    """Raised when the optional MCAP reader is unavailable."""


class NativeCaptureFormatError(NativeCaptureError):
    """Raised for invalid native data when strict reading is requested."""


def resolve_current_native_session(output_directory: str | Path) -> Path | None:
    """Resolve the recorder-published current session without path traversal."""
    root = Path(output_directory).expanduser()
    pointer = root / "current_session.json"
    try:
        metadata = json.loads(pointer.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("schema_version") != CURRENT_CAPTURE_SCHEMA:
        return None
    raw_path = metadata.get("path")
    if not isinstance(raw_path, str):
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or relative.name != raw_path or not raw_path.startswith("capture_"):
        return None
    session = root / relative
    return session if session.is_dir() else None


@dataclass(frozen=True, slots=True)
class NativeCaptureIssue:
    """A machine-readable validation or recovery issue."""

    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class NativeCaptureEvent:
    """A single native record with opaque serialized data kept separate."""

    kind: str
    monotonic_ns: int
    ros_time_ns: int | None
    sequence: int
    topic_id: int
    flags: int
    topic: str
    message_type: str | None
    serialization_format: str | None
    payload_size: int
    evidence_ref: str
    segment_path: Path
    channel_metadata: Mapping[str, str] = field(default_factory=dict)
    control_payload: Mapping[str, Any] | None = None

    @property
    def is_control(self) -> bool:
        return self.control_payload is not None

    def to_blackbox_event(
        self,
        *,
        ordered_timestamp: datetime | None = None,
        timestamp_source: str = "monotonic_unanchored",
    ) -> BlackBoxEvent:
        """Project this record into the existing intelligence-plane envelope.

        Serialized bytes are intentionally omitted. The MCAP reference,
        sequence, timing, type, and size are sufficient for chronology and
        evidence lookup without duplicating large payloads into JSONL.
        """
        timestamp = ordered_timestamp or _ns_to_datetime(self.monotonic_ns)
        native_metadata: dict[str, Any] = {
            "capture_backend": "cpp",
            "native_evidence_ref": self.evidence_ref,
            "monotonic_ns": self.monotonic_ns,
            "ros_time_ns": self.ros_time_ns,
            "timestamp_source": timestamp_source,
            "sequence": self.sequence,
            "topic_id": self.topic_id,
            "flags": self.flags,
            "payload_size": self.payload_size,
            "message_type": self.message_type,
            "serialization_format": self.serialization_format,
            "channel_metadata": dict(self.channel_metadata),
        }

        if not self.is_control:
            return BlackBoxEvent(
                timestamp=timestamp,
                source="native_capture",
                event_type="ros.serialized_message",
                severity="info",
                data={
                    "topic": self.topic,
                    "msg_type": self.message_type,
                    "payload_size": self.payload_size,
                    "sequence": self.sequence,
                    "evidence_ref": self.evidence_ref,
                },
                metadata=native_metadata,
            )

        payload = dict(self.control_payload or {})
        nested_data = payload.pop("data", None)
        data = dict(nested_data) if isinstance(nested_data, dict) else payload
        metadata = payload.pop("metadata", None)
        if isinstance(metadata, dict):
            native_metadata.update(metadata)

        default_source, default_event_type = _control_mapping(self.kind)
        source = payload.pop("source", default_source)
        if source not in _VALID_SOURCES:
            source = default_source
        event_type = str(payload.pop("event_type", default_event_type))
        raw_severity = payload.pop("severity", _control_severity(self.kind))
        severity = raw_severity
        if severity not in _VALID_SEVERITIES:
            severity = _control_severity(self.kind)

        if self.kind == "trigger":
            code = data.get("code")
            trigger_name, detector_class, native_event_type, subsystem = _NATIVE_TRIGGER_CODES.get(
                code,
                (
                    f"native_trigger_{code}",
                    "NativeCaptureTrigger",
                    "anomaly.native_trigger",
                    "recorder",
                ),
            )
            data.setdefault("detector", trigger_name)
            data.setdefault("message", f"Native capture trigger: {trigger_name}")
            native_metadata.setdefault("detector_class", detector_class)
            native_metadata.setdefault("target_subsystem", subsystem)
            event_type = native_event_type
            severity = _NATIVE_SEVERITIES.get(raw_severity, severity)

        return BlackBoxEvent(
            timestamp=timestamp,
            source=source,
            event_type=event_type,
            severity=severity,
            data=data,
            metadata=native_metadata,
        )


@dataclass(slots=True)
class _Segment:
    path: Path
    relative_path: str
    sidecar: dict[str, Any] | None
    first_sequence: int | None
    last_sequence: int | None


class NativeCaptureReader:
    """Iterate a native MCAP file or capture session directory.

    The default recovery mode yields the valid prefix of a truncated segment
    and records the failure in :attr:`quality`. ``strict=True`` converts the
    first validation or parsing issue into :class:`NativeCaptureFormatError`.
    Access ``quality`` after exhausting the iterator for final malformed-record
    and sequence-gap accounting.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        strict: bool = False,
        validate_crcs: bool = True,
    ) -> None:
        self.path = Path(path).expanduser()
        self.strict = strict
        self.validate_crcs = validate_crcs
        self._issues: list[NativeCaptureIssue] = []
        self._segments: list[_Segment] = []
        self._counts: dict[str, int | None] = {
            "received": 0,
            "captured": 0,
            "committed": 0,
            "dropped": 0,
            "bytes_captured": 0,
            "bytes_dropped": 0,
        }
        self._count_seen: set[str] = set()
        self._peak_queue: float | None = None
        self._storage_errors: list[str] = []
        self._storage_error_count = 0
        self._clock_anomalies = 0
        self._graph_coverage_faults = 0
        self._subscription_failures = 0
        self._runtime_callback_faults = 0
        self._rmw_messages_lost = 0
        self._delivery_observability_faults = 0
        self._best_effort_topics = 0
        self._topic_coverage_truncated = False
        self._node_coverage_truncated = False
        self._delivery_scope: str | None = None
        self._monotonic_start: int | None = None
        self._monotonic_end: int | None = None
        self._capture_start: datetime | None = None
        self._capture_end: datetime | None = None
        self._monotonic_anchor_ns: int | None = None
        self._system_time_anchor_ns: int | None = None
        self._capture_started_monotonic_ns: int | None = None
        self._capture_ended_monotonic_ns: int | None = None
        self._clean_values: list[bool] = []
        self._recovered = False
        self._recovery_discarded_tail_bytes: int | None = None
        self._recovery_corruption_reason: str | None = None
        self._malformed_records = 0
        self._sequence_gaps: list[tuple[int, int]] = []
        self._drop_ranges: list[tuple[int, int]] = []
        self._drop_breakdown: list[dict[str, Any]] = []
        self._final_drop_breakdown_loaded = False
        self._last_yielded_sequence: int | None = None
        self._last_yielded_monotonic: int | None = None
        self._records_read = 0
        self._session_id: str | None = None
        self._retained_events: int | None = None
        self._retention_evicted_segments = 0
        self._retention_evicted_events = 0
        self._retention_evicted_bytes = 0
        self._history_complete: bool | None = None
        self._post_window_elapsed: bool | None = None
        self._links_complete: bool | None = None
        self._durable: int | None = None
        self._is_incident_window = False
        self._incident_segments: dict[str, dict[str, Any]] = {}
        self._final_quality_loaded = False
        self._topics_by_id: dict[int, str] = {}
        self._selected_segment_paths: set[Path] = set()
        self._selection_applied = False
        self._prepare()

    @property
    def issues(self) -> tuple[NativeCaptureIssue, ...]:
        return tuple(self._issues)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def quality(self) -> CaptureQuality:
        unresolved = [gap for gap in self._sequence_gaps if not self._gap_accounted(gap)]
        reasons = list(dict.fromkeys(issue.code for issue in self._issues))
        storage_errors = list(self._storage_errors)
        if self._storage_error_count > 0:
            storage_errors.append(f"Recorder reported {self._storage_error_count} storage error(s)")
        dropped = self._value("dropped")
        if dropped is not None and dropped > 0:
            reasons.append("reported_drops")
        if unresolved:
            reasons.append("unresolved_sequence_gap")
        if storage_errors:
            reasons.append("storage_error")
        if self._history_complete is False:
            reasons.append("pre_trigger_history_truncated")
        if self._post_window_elapsed is False:
            reasons.append("post_trigger_window_truncated")
        if self._links_complete is False:
            reasons.append("incident_segment_link_failed")
        if self._graph_coverage_faults > 0:
            reasons.append("graph_coverage_fault")
        if self._subscription_failures > 0:
            reasons.append("subscription_failure")
        if self._runtime_callback_faults > 0:
            reasons.append("runtime_callback_fault")
        if self._rmw_messages_lost > 0:
            reasons.append("rmw_reported_message_loss")
        if self._delivery_observability_faults > 0:
            reasons.append("delivery_observability_fault")
        if self._best_effort_topics > 0:
            reasons.append("best_effort_delivery_unverified")
        if self._topic_coverage_truncated:
            reasons.append("topic_coverage_truncated")
        if self._node_coverage_truncated:
            reasons.append("node_coverage_truncated")
        if self._delivery_scope != "callback_received":
            # Without an authoritative delivery scope from the recorder we cannot
            # account for pre-callback (DDS) loss at all, so completeness is not
            # something this reader is entitled to assert. This is the guard that
            # stops an incident window -- which carries no capture_quality.json --
            # from silently reading as complete while the session it came from
            # recorded RMW message loss.
            reasons.append("delivery_scope_unverified")
        clean: bool | None
        if not self._clean_values:
            clean = None
            reasons.append("clean_state_unknown")
        else:
            clean = all(self._clean_values)
            if not clean:
                reasons.append("unclean_segment")

        reasons = list(dict.fromkeys(reasons))
        if not self._segments:
            completeness = "incomplete" if reasons else "unknown"
        elif reasons:
            completeness = "incomplete"
        else:
            completeness = "complete"

        committed = self._value("committed")
        durable = (
            self._durable if self._durable is not None else committed if clean is True else None
        )
        return CaptureQuality(
            backend="cpp",
            completeness=completeness,
            received=self._value("received"),
            captured=self._value("captured"),
            committed=committed,
            durable=durable,
            dropped=dropped,
            bytes_captured=self._value("bytes_captured"),
            bytes_dropped=self._value("bytes_dropped"),
            drop_breakdown=list(self._drop_breakdown),
            peak_queue_utilization=self._peak_queue,
            storage_errors=storage_errors,
            clock_anomalies=self._clock_anomalies,
            graph_coverage_faults=self._graph_coverage_faults,
            subscription_failures=self._subscription_failures,
            runtime_callback_faults=self._runtime_callback_faults,
            rmw_messages_lost=self._rmw_messages_lost,
            delivery_observability_faults=self._delivery_observability_faults,
            best_effort_topics=self._best_effort_topics,
            topic_coverage_truncated=self._topic_coverage_truncated,
            node_coverage_truncated=self._node_coverage_truncated,
            delivery_scope=self._delivery_scope,
            capture_start=self._capture_start,
            capture_end=self._capture_end,
            monotonic_start_ns=self._monotonic_start,
            monotonic_end_ns=self._monotonic_end,
            clean=clean,
            recovered=self._recovered,
            recovery_discarded_tail_bytes=self._recovery_discarded_tail_bytes,
            recovery_corruption_reason=self._recovery_corruption_reason,
            segments=len(self._segments),
            retained_events=self._retained_events,
            retention_evicted_segments=self._retention_evicted_segments,
            retention_evicted_events=self._retention_evicted_events,
            retention_evicted_bytes=self._retention_evicted_bytes,
            history_complete=self._history_complete,
            post_window_elapsed=self._post_window_elapsed,
            malformed_records=self._malformed_records,
            sequence_gaps=unresolved,
            incomplete_reasons=reasons,
        )

    def __iter__(self) -> Iterator[NativeCaptureEvent]:
        return self.iter_events()

    def iter_events(self) -> Iterator[NativeCaptureEvent]:
        """Yield validated records in segment and global-sequence order."""
        api = _load_mcap()
        for segment in self._segments:
            yield from self._iter_segment(segment, api)
        committed = self._value("committed")
        expected_records = self._retained_events
        if expected_records is None and not self._is_incident_window:
            expected_records = committed
        if expected_records is not None and expected_records != self._records_read:
            self._issue(
                "committed_count_mismatch",
                f"Metadata expects {expected_records} retained records but reader recovered "
                f"{self._records_read}",
                self.path,
            )

    def iter_blackbox_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[BlackBoxEvent]:
        """Yield metadata-only events consumable by existing Python logic."""
        self._selection_applied = True
        monotonic_to_wall_offset_ns: int | None = None
        if self._monotonic_anchor_ns is not None and self._system_time_anchor_ns is not None:
            monotonic_to_wall_offset_ns = self._system_time_anchor_ns - self._monotonic_anchor_ns
        for native_event in self:
            if monotonic_to_wall_offset_ns is None and native_event.ros_time_ns is not None:
                monotonic_to_wall_offset_ns = native_event.ros_time_ns - native_event.monotonic_ns
            if monotonic_to_wall_offset_ns is None:
                ordered_timestamp = _ns_to_datetime(native_event.monotonic_ns)
                timestamp_source = "monotonic_unanchored"
            else:
                ordered_timestamp = _ns_to_datetime(
                    native_event.monotonic_ns + monotonic_to_wall_offset_ns
                )
                timestamp_source = (
                    "system_monotonic_anchored"
                    if self._system_time_anchor_ns is not None
                    else "ros_monotonic_anchored_legacy"
                )
            event = native_event.to_blackbox_event(
                ordered_timestamp=ordered_timestamp,
                timestamp_source=timestamp_source,
            )
            if self._session_id:
                event.metadata.setdefault("session_id", self._session_id)
            if start is not None and event.timestamp < start:
                continue
            if end is not None and event.timestamp > end:
                continue
            self._selected_segment_paths.add(native_event.segment_path)
            yield event
        if not self._is_incident_window and monotonic_to_wall_offset_ns is not None:
            if start is not None and self._monotonic_start is not None:
                retained_start = _ns_to_datetime(
                    self._monotonic_start + monotonic_to_wall_offset_ns
                )
                if start < retained_start:
                    if self._retention_evicted_events > 0:
                        code = "requested_window_precedes_retained_capture"
                        message = "Requested incident start predates retained native evidence"
                    else:
                        code = "requested_window_precedes_capture_start"
                        message = "Requested incident start predates the native capture session"
                    self._issue(code, message, self.path)
            if (
                end is not None
                and self._capture_ended_monotonic_ns is not None
                and end
                > _ns_to_datetime(self._capture_ended_monotonic_ns + monotonic_to_wall_offset_ns)
            ):
                self._issue(
                    "requested_window_exceeds_capture_end",
                    "Requested incident end is later than the native capture session",
                    self.path,
                )

    def portable_files(self) -> tuple[tuple[Path, Path], ...]:
        """Return validated source files and safe bundle-relative destinations."""
        selected = (
            self._selected_segment_paths
            if self._selection_applied
            else {segment.path for segment in self._segments}
        )
        files: list[tuple[Path, Path]] = []
        for segment in self._segments:
            if segment.path not in selected:
                continue
            relative = Path(segment.relative_path)
            files.append((segment.path, relative))
            sidecar = _sidecar_path(segment.path)
            if sidecar.is_file():
                files.append((sidecar, relative.with_suffix(".json")))
            recovery = Path(str(segment.path) + ".recovery.json")
            if recovery.is_file():
                files.append((recovery, Path(str(relative) + ".recovery.json")))
        for name in ("session.json", "capture_quality.json", "capture.json"):
            source = self.path / name
            if source.is_file():
                files.append((source, Path(name)))
        return tuple(files)

    def _prepare(self) -> None:
        if self.path.is_file() and self.path.name == "capture.json":
            # Incident windows are addressed either by their directory or by
            # the manifest itself. Normalize both forms before file handling
            # so a JSON manifest is never interpreted as an MCAP segment.
            self.path = self.path.parent
        if self.path.is_file():
            self._segments = [self._segment_from_path(self.path)]
            self._validate_accounting()
            return
        if not self.path.is_dir():
            self._issue("capture_path_missing", "Capture path does not exist", self.path)
            return

        incident_path = self.path / "capture.json"
        incident = self._load_json(incident_path, "invalid_incident_capture")
        if incident is not None:
            self._validate_schema(
                incident, INCIDENT_CAPTURE_SCHEMA, incident_path, "incident capture"
            )
            self._is_incident_window = True
            if incident.get("session_id") is not None:
                self._session_id = str(incident["session_id"])
            self._retained_events = self._required_nonnegative_int(
                incident, "window_event_count", incident_path
            )
            self._history_complete = self._required_bool(
                incident, "history_complete", incident_path
            )
            self._post_window_elapsed = self._required_bool(
                incident, "post_window_elapsed", incident_path
            )
            self._links_complete = self._required_bool(incident, "links_complete", incident_path)
            for key in (
                "trigger_sequence",
                "trigger_monotonic_ns",
                "monotonic_anchor_ns",
                "system_time_anchor_ns",
                "requested_start_monotonic_ns",
                "requested_end_monotonic_ns",
                "actual_start_monotonic_ns",
                "actual_end_monotonic_ns",
                "received",
                "committed",
                "dropped",
            ):
                value = self._required_nonnegative_int(incident, key, incident_path)
                if key == "monotonic_anchor_ns":
                    self._monotonic_anchor_ns = value
                elif key == "system_time_anchor_ns":
                    self._system_time_anchor_ns = value
            self._prepare_incident_segments(incident, incident_path)

        session_path = self.path / "session.json"
        if not self._is_incident_window and session_path.exists():
            session = self._load_json(session_path, "invalid_session_metadata")
            if session is not None:
                self._validate_schema(session, SESSION_SCHEMA, session_path, "session")
                session_id = session.get("session_id")
                if session_id is not None:
                    self._session_id = str(session_id)
                else:
                    self._issue(
                        "session_id_missing",
                        "Capture session metadata has no session_id",
                        session_path,
                    )
                self._monotonic_anchor_ns = self._required_session_integer(
                    session, "monotonic_anchor_ns", session_path
                )
                self._system_time_anchor_ns = self._required_session_integer(
                    session, "system_time_anchor_ns", session_path
                )
        elif not self._is_incident_window:
            self._issue(
                "session_metadata_missing",
                "Capture session directory has no session.json",
                session_path,
            )

        quality_path = self.path / "capture_quality.json"
        quality_metadata = self._load_json(quality_path, "invalid_capture_quality")
        if quality_metadata is not None:
            self._validate_schema(quality_metadata, QUALITY_SCHEMA, quality_path, "capture quality")
            self._ingest_final_quality(quality_metadata, quality_path)
        elif not self._is_incident_window:
            self._issue(
                "final_capture_quality_missing",
                "Capture session has no authoritative final capture quality",
                quality_path,
            )

        partials = sorted((self.path / "segments").glob("*.partial.mcap"))
        partials.extend(sorted(self.path.glob("*.partial.mcap")))
        for partial in partials:
            self._issue(
                "partial_segment_present",
                "Capture session contains an unfinalized partial segment",
                partial,
            )

        if self._is_incident_window:
            actual = {path.name: path for path in self.path.glob("*.mcap")}
            expected = set(self._incident_segments)
            for missing in sorted(expected.difference(actual)):
                self._issue(
                    "incident_segment_missing",
                    f"Incident manifest segment {missing!r} is missing",
                    self.path / missing,
                )
            for extra in sorted(set(actual).difference(expected)):
                self._issue(
                    "incident_segment_unlisted",
                    f"Incident directory contains unlisted segment {extra!r}",
                    actual[extra],
                )
            candidates = [actual[name] for name in self._incident_segments if name in actual]
        else:
            candidates = sorted((self.path / "segments").glob("*.mcap"))
            if not candidates:
                candidates = sorted(self.path.glob("*.mcap"))
        if not candidates:
            self._issue("segments_missing", "Capture session contains no MCAP segments", self.path)
            return

        self._segments = [self._segment_from_path(path) for path in candidates]
        self._segments.sort(
            key=lambda item: (
                item.first_sequence is None,
                item.first_sequence if item.first_sequence is not None else 0,
                item.relative_path,
            )
        )
        self._validate_accounting()

    def _required_nonnegative_int(
        self, metadata: Mapping[str, Any], key: str, path: Path
    ) -> int | None:
        value = _optional_nonnegative_int(metadata.get(key))
        if value is None:
            self._issue(
                f"incident_{key}_invalid",
                f"Incident manifest {key} must be a nonnegative integer",
                path,
            )
        return value

    def _required_bool(self, metadata: Mapping[str, Any], key: str, path: Path) -> bool | None:
        value = _optional_bool(metadata.get(key))
        if value is None:
            self._issue(
                f"incident_{key}_invalid",
                f"Incident manifest {key} must be a boolean",
                path,
            )
        return value

    def _required_session_integer(
        self, metadata: Mapping[str, Any], key: str, path: Path
    ) -> int | None:
        value = _optional_nonnegative_int(metadata.get(key))
        if value is None:
            self._issue(
                f"session_{key}_invalid",
                f"Capture session {key} must be a nonnegative integer",
                path,
            )
        return value

    def _prepare_incident_segments(self, incident: Mapping[str, Any], path: Path) -> None:
        entries = incident.get("segments")
        if not isinstance(entries, list):
            self._issue(
                "incident_segments_invalid",
                "Incident manifest segments must be an array",
                path,
            )
            return
        event_total = 0
        for position, raw_entry in enumerate(entries):
            if not isinstance(raw_entry, dict):
                self._issue(
                    "incident_segment_entry_invalid",
                    f"Incident segment entry {position} must be an object",
                    path,
                )
                continue
            raw_name = raw_entry.get("path")
            if not isinstance(raw_name, str) or not raw_name:
                self._issue(
                    "incident_segment_path_invalid",
                    f"Incident segment entry {position} has no valid path",
                    path,
                )
                continue
            segment_name = Path(raw_name)
            if (
                segment_name.is_absolute()
                or segment_name.name != raw_name
                or raw_name in (".", "..")
                or segment_name.suffix != ".mcap"
            ):
                self._issue(
                    "incident_segment_path_invalid",
                    f"Incident segment path {raw_name!r} must be a safe MCAP filename",
                    path,
                )
                continue
            if raw_name in self._incident_segments:
                self._issue(
                    "incident_segment_duplicate",
                    f"Incident segment path {raw_name!r} is duplicated",
                    path,
                )
                continue
            valid = True
            for key in (
                "segment_index",
                "first_monotonic_ns",
                "last_monotonic_ns",
                "first_sequence",
                "last_sequence",
                "event_count",
                "file_bytes",
            ):
                if _optional_nonnegative_int(raw_entry.get(key)) is None:
                    self._issue(
                        f"incident_segment_{key}_invalid",
                        f"Incident segment {raw_name!r} has invalid {key}",
                        path,
                    )
                    valid = False
            digest = raw_entry.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                self._issue(
                    "incident_segment_sha256_invalid",
                    f"Incident segment {raw_name!r} has invalid sha256",
                    path,
                )
                valid = False
            if not valid:
                continue
            entry = dict(raw_entry)
            self._incident_segments[raw_name] = entry
            event_total += int(entry["event_count"])
        if self._retained_events is not None and event_total != self._retained_events:
            self._issue(
                "incident_window_count_mismatch",
                f"Incident segment entries total {event_total} events but "
                f"window_event_count is {self._retained_events}",
                path,
            )

    def _ingest_final_quality(self, metadata: Mapping[str, Any], path: Path) -> None:
        self._final_quality_loaded = True
        if metadata.get("backend") != "cpp":
            self._issue(
                "final_quality_backend_invalid",
                "Final capture quality backend must be 'cpp'",
                path,
            )
        if self._session_id is not None and metadata.get("session_id") != self._session_id:
            self._issue(
                "final_quality_session_id_mismatch",
                f"Final capture quality session_id {metadata.get('session_id')!r} "
                f"does not match {self._session_id!r}",
                path,
            )

        integers: dict[str, int | None] = {}
        for key in (
            "received",
            "admitted",
            "committed",
            "durable",
            "dropped",
            "bytes_captured",
            "bytes_dropped",
            "storage_errors",
            "clock_anomalies",
            "graph_wait_faults",
            "graph_coverage_faults",
            "graph_snapshot_failures",
            "node_snapshot_failures",
            "endpoint_query_failures",
            "subscription_failures",
            "runtime_callback_faults",
            "rmw_messages_lost",
            "rmw_event_callbacks_unavailable",
            "incompatible_qos_events",
            "ambiguous_topic_types",
            "best_effort_topics",
            "peak_queue_depth",
            "queue_capacity",
            "retained_segments",
            "retained_events",
            "retained_bytes",
            "retention_evicted_segments",
            "retention_evicted_events",
            "retention_evicted_bytes",
            "retention_max_segments",
            "retention_max_bytes",
            "monotonic_start_ns",
            "monotonic_end_ns",
            "capture_memory_budget_bytes",
            "configured_memory_budget_bytes",
            "capture_started_monotonic_ns",
            "capture_ended_monotonic_ns",
        ):
            value = _optional_nonnegative_int(metadata.get(key))
            integers[key] = value
            if value is None:
                self._issue(
                    f"final_quality_{key}_invalid",
                    f"Final capture quality {key} must be a nonnegative integer",
                    path,
                )

        self._retained_events = integers["retained_events"]
        self._retention_evicted_segments = integers["retention_evicted_segments"] or 0
        self._retention_evicted_events = integers["retention_evicted_events"] or 0
        self._retention_evicted_bytes = integers["retention_evicted_bytes"] or 0
        self._durable = integers["durable"]
        self._capture_started_monotonic_ns = integers["capture_started_monotonic_ns"]
        self._capture_ended_monotonic_ns = integers["capture_ended_monotonic_ns"]
        clean = _optional_bool(metadata.get("clean"))
        if clean is None:
            self._issue(
                "final_quality_clean_invalid",
                "Final capture quality clean must be a boolean",
                path,
            )
        else:
            self._clean_values.append(clean)
        for target, source in {
            "received": "received",
            "captured": "admitted",
            "committed": "committed",
            "dropped": "dropped",
            "bytes_captured": "bytes_captured",
            "bytes_dropped": "bytes_dropped",
        }.items():
            value = integers[source]
            if value is None:
                continue
            self._counts[target] = value
            self._count_seen.add(target)
        errors = integers["storage_errors"]
        if errors is not None:
            self._storage_error_count = max(self._storage_error_count, errors)
        anomalies = integers["clock_anomalies"]
        if anomalies is not None:
            self._clock_anomalies = max(self._clock_anomalies, anomalies)
        self._graph_coverage_faults = sum(
            int(integers[key] or 0)
            for key in (
                "graph_wait_faults",
                "graph_coverage_faults",
                "graph_snapshot_failures",
                "node_snapshot_failures",
                "endpoint_query_failures",
                "ambiguous_topic_types",
            )
        )
        self._subscription_failures = int(integers["subscription_failures"] or 0)
        self._runtime_callback_faults = int(integers["runtime_callback_faults"] or 0)
        self._rmw_messages_lost = int(integers["rmw_messages_lost"] or 0)
        self._delivery_observability_faults = int(
            integers["rmw_event_callbacks_unavailable"] or 0
        ) + int(integers["incompatible_qos_events"] or 0)
        self._best_effort_topics = int(integers["best_effort_topics"] or 0)
        for key, attribute in (
            ("topic_coverage_truncated", "_topic_coverage_truncated"),
            ("node_coverage_truncated", "_node_coverage_truncated"),
        ):
            value = _optional_bool(metadata.get(key))
            if value is None:
                self._issue(
                    f"final_quality_{key}_invalid",
                    f"Final capture quality {key} must be a boolean",
                    path,
                )
            else:
                setattr(self, attribute, value)
        delivery_scope = metadata.get("delivery_scope")
        if delivery_scope != "callback_received":
            self._issue(
                "final_quality_delivery_scope_invalid",
                "Final capture quality delivery_scope must be 'callback_received'",
                path,
            )
        else:
            self._delivery_scope = delivery_scope
        graph_scope = metadata.get("graph_scope")
        if graph_scope not in {"configured", "all_bounded"}:
            self._issue(
                "final_quality_graph_scope_invalid",
                "Final capture quality graph_scope is invalid",
                path,
            )
        peak_depth = integers["peak_queue_depth"]
        capacity = integers["queue_capacity"]
        if peak_depth is not None and capacity:
            self._peak_queue = min(1.0, peak_depth / capacity)
        elif capacity == 0:
            self._issue(
                "final_quality_queue_capacity_invalid",
                "Final capture quality queue_capacity must be greater than zero",
                path,
            )
        breakdown = metadata.get("drop_breakdown")
        if isinstance(breakdown, list):
            valid_breakdown: list[dict[str, Any]] = []
            for index, item in enumerate(breakdown):
                if not isinstance(item, dict) or any(
                    _optional_nonnegative_int(item.get(key)) is None
                    for key in (
                        "topic_id",
                        "reason",
                        "count",
                        "bytes",
                        "first_monotonic_ns",
                        "last_monotonic_ns",
                        "first_sequence",
                        "last_sequence",
                    )
                ):
                    self._issue(
                        "final_quality_drop_breakdown_invalid",
                        f"Final capture quality drop_breakdown entry {index} is invalid",
                        path,
                    )
                    continue
                valid_breakdown.append(dict(item))
            self._drop_breakdown = valid_breakdown
            self._final_drop_breakdown_loaded = True
        else:
            self._issue(
                "final_quality_drop_breakdown_invalid",
                "Final capture quality drop_breakdown must be an array",
                path,
            )

    def _segment_from_path(self, path: Path) -> _Segment:
        sidecar_path = _sidecar_path(path)
        sidecar = self._load_json(sidecar_path, "invalid_segment_sidecar")
        first: int | None = None
        last: int | None = None
        if sidecar is None:
            recovery_path = Path(str(path) + ".recovery.json")
            recovery = self._load_json(recovery_path, "invalid_recovery_metadata")
            if recovery is not None:
                self._ingest_recovery_metadata(path, recovery, recovery_path)
            self._issue(
                "segment_sidecar_missing",
                "Segment sidecar is missing or unreadable",
                sidecar_path,
            )
        else:
            self._validate_schema(sidecar, SEGMENT_SCHEMA, sidecar_path, "segment")
            first = _optional_nonnegative_int(sidecar.get("first_sequence"))
            last = _optional_nonnegative_int(sidecar.get("last_sequence"))
            self._ingest_sidecar(path, sidecar)
            self._validate_segment_identity(path, sidecar)

        relative = _relative_segment_path(path, self.path)
        return _Segment(path, relative, sidecar, first, last)

    def _ingest_recovery_metadata(
        self, segment_path: Path, metadata: Mapping[str, Any], metadata_path: Path
    ) -> None:
        self._validate_schema(metadata, "blackboxrs.capture_recovery.v1", metadata_path, "recovery")
        if metadata.get("output") != segment_path.name:
            self._issue(
                "recovery_output_mismatch",
                "Recovery metadata output does not identify the recovered segment",
                metadata_path,
            )
        discarded = _optional_nonnegative_int(metadata.get("discarded_tail_bytes"))
        if discarded is None:
            self._issue(
                "recovery_discarded_tail_invalid",
                "Recovery discarded_tail_bytes must be a nonnegative integer",
                metadata_path,
            )
        else:
            self._recovery_discarded_tail_bytes = discarded
        reason = metadata.get("corruption_reason")
        if not isinstance(reason, str):
            self._issue(
                "recovery_corruption_reason_invalid",
                "Recovery corruption_reason must be a string",
                metadata_path,
            )
        else:
            self._recovery_corruption_reason = reason
        declared_size = _optional_nonnegative_int(metadata.get("file_bytes"))
        if declared_size is None or declared_size != segment_path.stat().st_size:
            self._issue(
                "recovery_size_mismatch",
                "Recovery metadata file_bytes does not match recovered segment",
                metadata_path,
            )
        declared_sha = metadata.get("sha256")
        if not isinstance(declared_sha, str) or declared_sha.lower() != _sha256_file(segment_path):
            self._issue(
                "recovery_checksum_mismatch",
                "Recovery metadata SHA-256 does not match recovered segment",
                metadata_path,
            )
        self._recovered = True

    def _ingest_sidecar(self, segment_path: Path, sidecar: dict[str, Any]) -> None:
        for key in (
            "session_id",
            "segment_index",
            "path",
            "first_sequence",
            "last_sequence",
        ):
            if key not in sidecar:
                self._issue(
                    f"segment_{key}_missing",
                    f"Segment sidecar has no {key}",
                    segment_path,
                )
        if self._session_id is None and sidecar.get("session_id") is not None:
            self._session_id = str(sidecar["session_id"])
        elif self._session_id is not None and sidecar.get("session_id") != self._session_id:
            self._issue(
                "segment_session_id_mismatch",
                f"Segment session_id {sidecar.get('session_id')!r} does not match "
                f"{self._session_id!r}",
                segment_path,
            )

        clean = sidecar.get("clean")
        if isinstance(clean, bool):
            self._clean_values.append(clean)
        else:
            self._issue("segment_clean_missing", "Segment clean state is missing", segment_path)
        recovered = sidecar.get("recovered")
        if isinstance(recovered, bool):
            self._recovered = self._recovered or recovered

        count_sources = sidecar.get("counts")
        counts = count_sources if isinstance(count_sources, dict) else sidecar
        aliases = {
            "received": "received",
            "captured": "admitted",
            "committed": "committed",
            "dropped": "dropped",
            "bytes_captured": "bytes_captured",
            "bytes_dropped": "bytes_dropped",
        }
        byte_counts = sidecar.get("bytes")
        if not isinstance(byte_counts, dict):
            byte_counts = {}
        cumulative_accounting = sidecar.get("accounting_scope") == "session_cumulative"
        for target, source in aliases.items():
            raw_value = counts.get(source, sidecar.get(source))
            if raw_value is None and target.startswith("bytes_"):
                raw_value = byte_counts.get(target.removeprefix("bytes_"))
            value = _optional_nonnegative_int(raw_value)
            if value is None:
                self._issue(
                    f"{target}_count_missing",
                    f"Segment {target} accounting is missing",
                    segment_path,
                )
                continue
            if cumulative_accounting:
                self._counts[target] = max(int(self._counts[target] or 0), value)
            else:
                self._counts[target] = int(self._counts[target] or 0) + value
            self._count_seen.add(target)

        peak = sidecar.get("peak_queue_utilization")
        if isinstance(peak, (int, float)) and not isinstance(peak, bool):
            normalized = float(peak)
            if normalized > 1.0 and normalized <= 100.0:
                normalized /= 100.0
            if 0.0 <= normalized <= 1.0:
                self._peak_queue = max(self._peak_queue or 0.0, normalized)
            else:
                self._issue(
                    "invalid_queue_utilization", "Queue utilization is out of range", segment_path
                )
        else:
            self._issue(
                "queue_utilization_missing", "Peak queue utilization is missing", segment_path
            )

        storage_errors = sidecar.get("storage_errors")
        if isinstance(storage_errors, list):
            self._storage_errors.extend(str(item) for item in storage_errors)
        elif isinstance(storage_errors, int) and storage_errors >= 0:
            if cumulative_accounting:
                self._storage_error_count = max(self._storage_error_count, storage_errors)
            else:
                self._storage_error_count += storage_errors
        elif storage_errors is None:
            self._issue(
                "storage_error_state_missing", "Storage error state is missing", segment_path
            )

        anomalies = _optional_nonnegative_int(sidecar.get("clock_anomalies"))
        if anomalies is None:
            self._issue(
                "clock_anomaly_count_missing", "Clock anomaly count is missing", segment_path
            )
        else:
            if cumulative_accounting:
                self._clock_anomalies = max(self._clock_anomalies, anomalies)
            else:
                self._clock_anomalies += anomalies

        start = _optional_nonnegative_int(sidecar.get("monotonic_start_ns"))
        end = _optional_nonnegative_int(sidecar.get("monotonic_end_ns"))
        if start is None or end is None:
            self._issue(
                "monotonic_bounds_missing", "Monotonic segment bounds are missing", segment_path
            )
        else:
            self._monotonic_start = (
                start if self._monotonic_start is None else min(self._monotonic_start, start)
            )
            self._monotonic_end = (
                end if self._monotonic_end is None else max(self._monotonic_end, end)
            )

        expected_sha = sidecar.get("sha256")
        if not isinstance(expected_sha, str):
            self._issue("segment_checksum_missing", "Segment SHA-256 is missing", segment_path)
        else:
            try:
                actual_sha = _sha256_file(segment_path)
            except OSError as exc:
                self._issue("segment_checksum_unreadable", str(exc), segment_path)
            else:
                if actual_sha != expected_sha.lower():
                    self._issue(
                        "segment_checksum_mismatch",
                        "Segment SHA-256 does not match sidecar",
                        segment_path,
                    )

    def _validate_segment_identity(self, segment_path: Path, sidecar: Mapping[str, Any]) -> None:
        declared_path = sidecar.get("path")
        actual_relative = _relative_segment_path(segment_path, self.path)
        if not isinstance(declared_path, str) or declared_path not in {
            segment_path.name,
            actual_relative,
        }:
            self._issue(
                "segment_path_mismatch",
                f"Segment sidecar path {declared_path!r} does not identify {actual_relative!r}",
                segment_path,
            )

        segment_index = _optional_nonnegative_int(sidecar.get("segment_index"))
        try:
            filename_index = int(segment_path.stem)
        except ValueError:
            filename_index = None
        if segment_index is None or filename_index != segment_index:
            self._issue(
                "segment_index_mismatch",
                f"Segment index {segment_index!r} does not match filename {segment_path.name!r}",
                segment_path,
            )

        declared_size = _optional_nonnegative_int(sidecar.get("file_bytes"))
        try:
            actual_size = segment_path.stat().st_size
        except OSError as exc:
            self._issue("segment_size_unreadable", str(exc), segment_path)
        else:
            if declared_size is None or declared_size != actual_size:
                self._issue(
                    "segment_size_mismatch",
                    f"Segment sidecar file_bytes={declared_size!r}, actual={actual_size}",
                    segment_path,
                )

        manifest = self._incident_segments.get(segment_path.name)
        if manifest is None:
            return
        comparisons = {
            "segment_index": segment_index,
            "first_sequence": _optional_nonnegative_int(sidecar.get("first_sequence")),
            "last_sequence": _optional_nonnegative_int(sidecar.get("last_sequence")),
            "first_monotonic_ns": _optional_nonnegative_int(sidecar.get("monotonic_start_ns")),
            "last_monotonic_ns": _optional_nonnegative_int(sidecar.get("monotonic_end_ns")),
            "event_count": _optional_nonnegative_int(sidecar.get("event_count")),
            "file_bytes": declared_size,
        }
        for key, sidecar_value in comparisons.items():
            if sidecar_value != manifest.get(key):
                self._issue(
                    f"incident_segment_{key}_mismatch",
                    f"Incident manifest {key}={manifest.get(key)!r}, sidecar={sidecar_value!r}",
                    segment_path,
                )
        if str(sidecar.get("sha256", "")).lower() != str(manifest.get("sha256", "")).lower():
            self._issue(
                "incident_segment_sha256_mismatch",
                "Incident manifest SHA-256 does not match the segment sidecar",
                segment_path,
            )

    def _iter_segment(self, segment: _Segment, api: Any) -> Iterator[NativeCaptureEvent]:
        reference_sequence = segment.first_sequence
        record_count = 0
        first_sequence: int | None = None
        last_sequence: int | None = None
        try:
            with segment.path.open("rb") as stream:
                # A forward reader is intentional. Seeking readers consult the
                # footer before yielding and cannot recover a valid prefix from
                # an abruptly truncated segment.
                reader = api.nonseeking_reader(stream, validate_crcs=self.validate_crcs)
                for schema, channel, message in reader.iter_messages(log_time_order=False):
                    record_count += 1
                    try:
                        event = self._decode_record(
                            segment,
                            schema,
                            channel,
                            message,
                            reference_sequence,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        self._malformed_records += 1
                        self._issue("malformed_record", str(exc), segment.path)
                        continue

                    reference_sequence = event.sequence + 1
                    if first_sequence is None:
                        first_sequence = event.sequence
                    last_sequence = event.sequence
                    if not self._admit_event(event, segment.path):
                        continue
                    self._observe_event(event)
                    self._records_read += 1
                    yield event
        except (OSError, api.mcap_error, EOFError, RuntimeError, struct.error) as exc:
            self._issue("truncated_or_invalid_segment", str(exc), segment.path)
        self._validate_segment_records(segment, record_count, first_sequence, last_sequence)

    def _validate_segment_records(
        self,
        segment: _Segment,
        record_count: int,
        first_sequence: int | None,
        last_sequence: int | None,
    ) -> None:
        if segment.sidecar is None:
            return
        expected_count = _optional_nonnegative_int(segment.sidecar.get("event_count"))
        if expected_count is None or expected_count != record_count:
            self._issue(
                "segment_event_count_mismatch",
                f"Segment sidecar event_count={expected_count!r}, decoded={record_count}",
                segment.path,
            )
        if segment.first_sequence != first_sequence:
            self._issue(
                "segment_first_sequence_mismatch",
                f"Segment sidecar first_sequence={segment.first_sequence!r}, "
                f"decoded={first_sequence!r}",
                segment.path,
            )
        if segment.last_sequence != last_sequence:
            self._issue(
                "segment_last_sequence_mismatch",
                f"Segment sidecar last_sequence={segment.last_sequence!r}, "
                f"decoded={last_sequence!r}",
                segment.path,
            )

    def _decode_record(
        self,
        segment: _Segment,
        schema: Any,
        channel: Any,
        message: Any,
        reference_sequence: int | None,
    ) -> NativeCaptureEvent:
        if channel.topic == CONTROL_TOPIC:
            if channel.message_encoding != "json":
                raise ValueError("control channel message encoding is not json")
            if schema is None or schema.name != CONTROL_SCHEMA:
                raise ValueError("control channel schema is missing or unsupported")
            envelope = json.loads(message.data.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("control envelope must be a JSON object")
            _validate_control_envelope(envelope)
            payload = dict(envelope["payload"])
            sequence = int(envelope["sequence"])
            kind = str(envelope["kind"])
            topic_id = int(envelope["topic_id"])
            payload_topic = payload.get("topic")
            if topic_id > 0 and isinstance(payload_topic, str):
                self._topics_by_id[topic_id] = payload_topic
            elif topic_id in self._topics_by_id:
                payload.setdefault("topic", self._topics_by_id[topic_id])
            if kind == "drop":
                self._remember_drop_range(payload)
            return NativeCaptureEvent(
                kind=kind,
                monotonic_ns=int(envelope["monotonic_ns"]),
                ros_time_ns=envelope["ros_time_ns"],
                sequence=sequence,
                topic_id=topic_id,
                flags=int(envelope["flags"]),
                topic=channel.topic,
                message_type=schema.name,
                serialization_format="json",
                payload_size=len(message.data),
                evidence_ref=_evidence_ref(segment.relative_path, sequence, channel.id),
                segment_path=segment.path,
                channel_metadata=dict(channel.metadata or {}),
                control_payload=payload,
            )

        metadata = channel.metadata or {}
        topic_id = _metadata_int(metadata, "blackboxrs.topic_id", "topic_id")
        if topic_id is None:
            topic_id = 0
            self._issue("topic_id_missing", "Raw channel has no topic ID", segment.path)
        elif topic_id > 0:
            self._topics_by_id[topic_id] = channel.topic
        sequence = _expand_sequence(
            int(message.sequence),
            reference_sequence,
            segment.first_sequence,
            segment.last_sequence,
        )
        ros_time = int(message.publish_time) if int(message.publish_time) != 0 else None
        ros_type = metadata.get("blackboxrs.ros_type")
        if not ros_type and schema is not None:
            ros_type = schema.name
        serialization = metadata.get("blackboxrs.serialization_format")
        if not serialization:
            serialization = channel.message_encoding
        flags = SERIALIZED_MESSAGE | (ROS_TIME_VALID if ros_time is not None else 0)
        return NativeCaptureEvent(
            kind="serialized_message",
            monotonic_ns=int(message.log_time),
            ros_time_ns=ros_time,
            sequence=sequence,
            topic_id=topic_id,
            flags=flags,
            topic=channel.topic,
            message_type=str(ros_type) if ros_type else None,
            serialization_format=str(serialization) if serialization else None,
            payload_size=len(message.data),
            evidence_ref=_evidence_ref(segment.relative_path, sequence, channel.id),
            segment_path=segment.path,
            channel_metadata=dict(metadata),
        )

    def _admit_event(self, event: NativeCaptureEvent, path: Path) -> bool:
        sequence = event.sequence
        previous = self._last_yielded_sequence
        if previous is not None:
            if sequence <= previous:
                self._malformed_records += 1
                self._issue(
                    "nonmonotonic_sequence",
                    f"Sequence {sequence} follows {previous}; record skipped",
                    path,
                )
                return False
            if sequence > previous + 1:
                self._sequence_gaps.append((previous + 1, sequence - 1))
        previous_monotonic = self._last_yielded_monotonic
        if previous_monotonic is not None and event.monotonic_ns < previous_monotonic:
            self._malformed_records += 1
            self._issue(
                "nonmonotonic_capture_time",
                f"Monotonic time {event.monotonic_ns} follows {previous_monotonic}; record skipped",
                path,
            )
            return False
        self._last_yielded_sequence = sequence
        self._last_yielded_monotonic = event.monotonic_ns
        return True

    def _validate_accounting(self) -> None:
        received = self._value("received")
        dropped = self._value("dropped")
        committed = self._value("committed")
        if (
            self._final_quality_loaded
            and received is not None
            and committed is not None
            and dropped is not None
            and received != committed + dropped
        ):
            self._issue(
                "capture_count_mismatch",
                f"received={received}, committed={committed}, dropped={dropped}",
                self.path,
            )
        captured = self._value("captured")
        if captured is not None and committed is not None and committed > captured:
            self._issue(
                "commit_count_exceeds_capture",
                f"captured={captured}, committed={committed}",
                self.path,
            )
        if (
            captured is not None
            and committed is not None
            and captured != committed
            and not self._is_incident_window
            and self._clean_values
            and all(self._clean_values)
        ):
            self._issue(
                "clean_segment_uncommitted_events",
                f"captured={captured}, committed={committed}",
                self.path,
            )

    def _observe_event(self, event: NativeCaptureEvent) -> None:
        self._monotonic_start = (
            event.monotonic_ns
            if self._monotonic_start is None
            else min(self._monotonic_start, event.monotonic_ns)
        )
        self._monotonic_end = (
            event.monotonic_ns
            if self._monotonic_end is None
            else max(self._monotonic_end, event.monotonic_ns)
        )
        ordered_ns: int | None = None
        if self._monotonic_anchor_ns is not None and self._system_time_anchor_ns is not None:
            ordered_ns = (
                event.monotonic_ns + self._system_time_anchor_ns - self._monotonic_anchor_ns
            )
        elif event.ros_time_ns is not None:
            ordered_ns = event.ros_time_ns
        if ordered_ns is not None:
            try:
                moment = _ns_to_datetime(ordered_ns)
            except (OverflowError, OSError, ValueError):
                self._issue(
                    "invalid_ros_time", "ROS time is outside datetime range", event.segment_path
                )
            else:
                self._capture_start = (
                    moment if self._capture_start is None else min(self._capture_start, moment)
                )
                self._capture_end = (
                    moment if self._capture_end is None else max(self._capture_end, moment)
                )

    def _remember_drop_range(self, payload: Mapping[str, Any]) -> None:
        if not self._final_drop_breakdown_loaded:
            self._drop_breakdown.append(dict(payload))
        first = payload.get("first_sequence", payload.get("first_dropped_sequence"))
        last = payload.get("last_sequence", payload.get("last_dropped_sequence"))
        start = _optional_nonnegative_int(first)
        end = _optional_nonnegative_int(last)
        if start is not None and end is not None and end >= start:
            self._drop_ranges.append((start, end))

    def _gap_accounted(self, gap: tuple[int, int]) -> bool:
        return any(start <= gap[0] and end >= gap[1] for start, end in self._drop_ranges)

    def _value(self, name: str) -> int | None:
        return int(self._counts[name] or 0) if name in self._count_seen else None

    def _load_json(self, path: Path, code: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._issue(code, str(exc), path)
            return None
        if not isinstance(raw, dict):
            self._issue(code, "Metadata root must be a JSON object", path)
            return None
        return raw

    def _validate_schema(
        self,
        metadata: Mapping[str, Any],
        expected: str,
        path: Path,
        label: str,
    ) -> None:
        actual = metadata.get("schema", metadata.get("schema_version"))
        if actual != expected:
            self._issue(
                f"unsupported_{label}_schema",
                f"Expected {expected!r}, got {actual!r}",
                path,
            )

    def _issue(self, code: str, message: str, path: Path | None = None) -> None:
        issue = NativeCaptureIssue(code, message, str(path) if path else None)
        self._issues.append(issue)
        if self.strict:
            location = f" ({path})" if path else ""
            raise NativeCaptureFormatError(f"{code}{location}: {message}")
        logger.warning(
            "Native capture issue %s%s: %s", code, f" in {path}" if path else "", message
        )


@dataclass(frozen=True, slots=True)
class _McapApi:
    nonseeking_reader: Any
    mcap_error: type[Exception]


def _load_mcap() -> _McapApi:
    try:
        reader_module = importlib.import_module("mcap.reader")
        exceptions_module = importlib.import_module("mcap.exceptions")
    except ImportError as exc:
        raise NativeCaptureDependencyError(
            "Native capture reading requires the optional 'mcap' package. "
            "Install BlackBoxRS with the replay extra: pip install 'blackboxrs[replay]'."
        ) from exc
    return _McapApi(reader_module.NonSeekingReader, exceptions_module.McapError)


def _validate_control_envelope(envelope: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "monotonic_ns",
        "ros_time_ns",
        "sequence",
        "topic_id",
        "flags",
        "payload",
    }
    missing = sorted(required.difference(envelope))
    if missing:
        raise ValueError(f"control envelope missing fields: {', '.join(missing)}")
    version = envelope["schema_version"]
    if version not in (1, "1", "1.0", CONTROL_SCHEMA):
        raise ValueError(f"unsupported control schema version {version!r}")
    if not isinstance(envelope["kind"], str):
        raise TypeError("control kind must be a string")
    if envelope["kind"] not in _KNOWN_CONTROL_KINDS:
        logger.info("Reading forward-compatible native control kind %r", envelope["kind"])
    for key in ("monotonic_ns", "sequence", "topic_id", "flags"):
        value = envelope[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TypeError(f"control {key} must be a nonnegative integer")
    ros_time = envelope["ros_time_ns"]
    if ros_time is not None and (not isinstance(ros_time, int) or isinstance(ros_time, bool)):
        raise TypeError("control ros_time_ns must be an integer or null")
    if not isinstance(envelope["payload"], dict):
        raise TypeError("control payload must be an object")


def _control_mapping(kind: str) -> tuple[str, str]:
    return {
        "graph": ("ros_monitor", "ros.graph"),
        "trigger": ("anomaly_engine", "anomaly.native_trigger"),
        "clock": ("ros_monitor", "ros.clock"),
        "drop": ("native_capture", "capture.drop"),
        "status": ("native_capture", "capture.status"),
        "storage": ("native_capture", "capture.storage"),
    }.get(kind, ("native_capture", f"capture.{kind}"))


def _control_severity(kind: str) -> str:
    if kind == "storage":
        return "error"
    if kind in {"drop", "trigger", "clock"}:
        return "warning"
    return "info"


def _ns_to_datetime(value: int) -> datetime:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=nanoseconds // 1_000
    )


def _metadata_int(metadata: Mapping[str, str], *keys: str) -> int | None:
    for key in keys:
        if key not in metadata:
            continue
        try:
            value = int(metadata[key])
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _optional_nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _expand_sequence(
    low: int,
    reference: int | None,
    segment_first: int | None,
    segment_last: int | None,
) -> int:
    if segment_first is not None and segment_last is not None:
        if segment_first <= low <= segment_last:
            return low
    anchor = reference if reference is not None else segment_first
    if anchor is None:
        return low
    candidate = (anchor & ~0xFFFFFFFF) | (low & 0xFFFFFFFF)
    if candidate < anchor:
        candidate += _UINT32_MODULUS
    return candidate


def _sidecar_path(segment: Path) -> Path:
    name = segment.name
    if name.endswith(".partial.mcap"):
        return segment.with_name(name[: -len(".partial.mcap")] + ".json")
    return segment.with_suffix(".json")


def _relative_segment_path(segment: Path, root: Path) -> str:
    if root.is_dir():
        try:
            return segment.relative_to(root).as_posix()
        except ValueError:
            pass
    return segment.name


def _evidence_ref(relative_path: str, sequence: int, channel_id: int) -> str:
    return f"native_capture/{relative_path}#sequence={sequence}&channel={channel_id}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CONTROL_SCHEMA",
    "CONTROL_TOPIC",
    "NativeCaptureDependencyError",
    "NativeCaptureError",
    "NativeCaptureEvent",
    "NativeCaptureFormatError",
    "NativeCaptureIssue",
    "NativeCaptureReader",
    "SEGMENT_SCHEMA",
    "SESSION_SCHEMA",
]
