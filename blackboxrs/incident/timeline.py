"""Timeline reconstruction.

v0.4 vertical slice: produce a strictly-ordered timeline from raw events
and triggers. Source-priority is used to break timestamp ties so the
order is deterministic.

Derived event detectors (silence interval, resource excursion, graph
delta) and causality annotation land in M3; this module is structured
so those pluggable derivers can drop in without changing callers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from blackboxrs.core.config import AnomalyThresholds
from blackboxrs.core.schemas import BlackBoxEvent

from . import derived as derived_mod
from .models import DetectorTrigger, SystemSnapshot, TimelineEvent


# Lower number = higher priority when timestamps tie.
_SOURCE_PRIORITY: dict[str, int] = {
    "anomaly_engine": 0,
    "ros_monitor": 1,
    "system_monitor": 2,
    "rosbag_recorder": 3,
}


def _subsystem_for(source: str) -> str:
    return {
        "ros_monitor": "ros",
        "system_monitor": "system",
        "anomaly_engine": "anomaly",
        "rosbag_recorder": "recorder",
    }.get(source, "external")


def _event_summary(ev: BlackBoxEvent) -> str:
    """One-line summary for an event, suitable for a timeline row."""
    if ev.source == "anomaly_engine":
        return f"anomaly: {ev.event_type}: {ev.data.get('message', '')}"
    if ev.event_type == "ros.frequency":
        topic = ev.data.get("topic", "?")
        hz = ev.data.get("frequency_hz", "?")
        return f"frequency on {topic}: {hz} Hz"
    if ev.event_type == "ros.qos_snapshot":
        return f"qos snapshot on {ev.data.get('topic', '?')}"
    if ev.event_type.startswith("system."):
        return (
            f"{ev.event_type}: {ev.data.get('value', '?')} "
            f"{ev.data.get('unit', '')}".strip()
        )
    return ev.event_type


def reconstruct(
    events: Iterable[BlackBoxEvent],
    triggers: list[DetectorTrigger] | None = None,
    *,
    snapshots: list[SystemSnapshot] | None = None,
    thresholds: AnomalyThresholds | None = None,
    dead_topic_timeout_sec: float | None = None,
) -> list[TimelineEvent]:
    """Build a strictly-ordered timeline.

    Args:
        events: Raw events sliced into the incident window.
        triggers: Promoted detector triggers (already constructed by the
            builder). Used to populate ``evidence_ref`` for anomaly rows.
        snapshots: Optional list of :class:`SystemSnapshot` (used by the
            graph-delta deriver). When None or empty, no graph-delta
            events are emitted.
        thresholds: Optional :class:`AnomalyThresholds` (used by the
            resource-excursion deriver). When None, no excursion events
            are emitted.
        dead_topic_timeout_sec: Optional silence-interval threshold.
            When None, no silence-interval events are emitted.

    Returns:
        A list of :class:`TimelineEvent` ordered by ``(t, source-priority)``,
        with derived events interleaved by timestamp.
    """
    triggers = triggers or []
    snapshots = snapshots or []
    triggers_by_source_ref: dict[str, DetectorTrigger] = {
        t.source_event_ref: t for t in triggers if t.source_event_ref
    }

    # We need a *list* (not iterable) for the derivers because they
    # iterate twice. Materialise once.
    events_list = list(events)

    rows: list[tuple[datetime, int, TimelineEvent]] = []
    for line_index, ev in enumerate(events_list, start=1):
        ref = f"events.jsonl#L{line_index}"
        kind = "trigger" if ev.source == "anomaly_engine" else "raw"
        if ref in triggers_by_source_ref:
            t = triggers_by_source_ref[ref]
            evidence_ref = f"triggers.json#{t.trigger_id}"
            kind = "trigger"
        else:
            evidence_ref = ref
        item = TimelineEvent(
            t=ev.timestamp,
            kind=kind,
            subsystem=_subsystem_for(ev.source),
            summary=_event_summary(ev),
            confidence=1.0,
            evidence_ref=evidence_ref,
            data=dict(ev.data),
        )
        priority = _SOURCE_PRIORITY.get(ev.source, 99)
        rows.append((ev.timestamp, priority, item))

    # Derived events. Each deriver is independent and can short-circuit
    # cheaply when its inputs are absent.
    derived_events: list[TimelineEvent] = []
    if dead_topic_timeout_sec is not None:
        derived_events.extend(derived_mod.silence_intervals(
            events_list, timeout_sec=dead_topic_timeout_sec,
        ))
    if snapshots:
        derived_events.extend(derived_mod.graph_deltas(snapshots))
    if thresholds is not None:
        derived_events.extend(derived_mod.resource_excursions(
            events_list, thresholds,
        ))

    # Derived events sort with the lowest source-priority bucket so they
    # appear *after* raw events with the same timestamp. This matches the
    # reading model: "the cause was the raw event; the derived row
    # explains it."
    for de in derived_events:
        rows.append((de.t, 90, de))

    rows.sort(key=lambda r: (r[0], r[1]))
    return [item for _, _, item in rows]
