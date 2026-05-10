"""Derived timeline event detectors.

A *derived* timeline event is a :class:`TimelineEvent` whose ``kind``
is ``"derived"``: it does not correspond to a single raw event but is
synthesised from a sequence (or comparison) of them. Each derived
event must:

* set ``kind="derived"``;
* point ``evidence_ref`` at a concrete file/range inside the bundle;
* fill ``correlated_event_refs`` with every supporting reference;
* set ``confidence`` honestly (algorithm-specific, never 1.0).

Three derivers ship in v0.4.x:

- :func:`silence_intervals`: gaps in ``ros.frequency`` events on a
  topic that exceed ``timeout_sec``. Confidence 0.95 (the gap is
  observable in the raw stream; the only uncertainty is whether the
  topic was *expected* to be alive at all).
- :func:`graph_deltas`: appearance/disappearance of nodes or topics
  between consecutive snapshots. Confidence 0.85 (snapshot cadence
  may miss short-lived state, so absence is weaker than presence).
- :func:`resource_excursions`: sustained CPU/memory/GPU thermal above
  a configured threshold for at least ``sustain_sec``. Confidence
  0.85 (threshold sensitivity is operator-tuned).

Each deriver returns a list of :class:`TimelineEvent`. The builder
folds these into ``timeline.json`` after raw + trigger ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from blackboxrs.core.config import AnomalyThresholds
from blackboxrs.core.schemas import BlackBoxEvent

from .models import SystemSnapshot, TimelineEvent


# ---------------------------------------------------------------------------
# Silence intervals
# ---------------------------------------------------------------------------


def silence_intervals(
    events: Iterable[BlackBoxEvent],
    *,
    timeout_sec: float,
) -> list[TimelineEvent]:
    """Emit a derived event for each silence gap > ``timeout_sec`` per topic.

    A "gap" is the time between two consecutive ``ros.frequency`` events
    on the same topic. We do not emit gaps that begin before the window
    or extend past it; those are observable as missing snapshots, not
    as derivable intervals.

    Args:
        events: The window's raw events (line-indexed by the builder).
        timeout_sec: Silence threshold; ``DeadTopicConfig.timeout_sec``
            is the natural source.

    Returns:
        Zero or more :class:`TimelineEvent` with ``kind="derived"``.
    """
    if timeout_sec <= 0:
        return []

    # Pair each frequency event with its 1-indexed line number so the
    # evidence_ref points back into events.jsonl.
    rows: list[tuple[BlackBoxEvent, int]] = []
    for line_index, ev in enumerate(events, start=1):
        if ev.event_type == "ros.frequency":
            topic = ev.data.get("topic")
            if isinstance(topic, str):
                rows.append((ev, line_index))

    by_topic: dict[str, list[tuple[BlackBoxEvent, int]]] = {}
    for ev, line in rows:
        by_topic.setdefault(ev.data["topic"], []).append((ev, line))

    out: list[TimelineEvent] = []
    for topic, history in by_topic.items():
        history.sort(key=lambda r: r[0].timestamp)
        for (prev_ev, prev_line), (curr_ev, curr_line) in zip(history, history[1:]):
            gap = (curr_ev.timestamp - prev_ev.timestamp).total_seconds()
            if gap <= timeout_sec:
                continue
            out.append(TimelineEvent(
                t=prev_ev.timestamp + timedelta(seconds=timeout_sec),
                kind="derived",
                subsystem="ros",
                summary=(
                    f"silence interval on {topic}: "
                    f"{gap:.1f}s gap (timeout {timeout_sec:.1f}s)"
                ),
                confidence=0.95,
                evidence_ref=f"events.jsonl#L{prev_line}",
                data={"topic": topic, "gap_sec": gap, "timeout_sec": timeout_sec},
                correlated_event_refs=[
                    f"events.jsonl#L{prev_line}",
                    f"events.jsonl#L{curr_line}",
                ],
            ))
    return out


# ---------------------------------------------------------------------------
# Graph deltas
# ---------------------------------------------------------------------------


def graph_deltas(snapshots: list[SystemSnapshot]) -> list[TimelineEvent]:
    """Emit a derived event for each topic appearance/disappearance and
    each pub_count change between consecutive snapshots.

    Node-set deltas are emitted too. We do not emit events when only
    the publish frequency changes (that's a raw event already).
    """
    if len(snapshots) < 2:
        return []

    out: list[TimelineEvent] = []
    for idx, (prev, curr) in enumerate(zip(snapshots, snapshots[1:]), start=1):
        prev_topics = {t.name: t for t in prev.topics}
        curr_topics = {t.name: t for t in curr.topics}

        appeared = sorted(curr_topics.keys() - prev_topics.keys())
        disappeared = sorted(prev_topics.keys() - curr_topics.keys())

        for name in appeared:
            out.append(TimelineEvent(
                t=curr.t,
                kind="derived",
                subsystem="ros",
                summary=f"topic {name} appeared on the graph",
                confidence=0.85,
                evidence_ref=f"snapshots.json#{idx}",
                data={"topic": name, "delta": "appeared"},
                correlated_event_refs=[
                    f"snapshots.json#{idx - 1}",
                    f"snapshots.json#{idx}",
                ],
            ))
        for name in disappeared:
            out.append(TimelineEvent(
                t=curr.t,
                kind="derived",
                subsystem="ros",
                summary=f"topic {name} disappeared from the graph",
                confidence=0.85,
                evidence_ref=f"snapshots.json#{idx}",
                data={"topic": name, "delta": "disappeared"},
                correlated_event_refs=[
                    f"snapshots.json#{idx - 1}",
                    f"snapshots.json#{idx}",
                ],
            ))

        # Pub-count deltas on persisting topics.
        for name in sorted(curr_topics.keys() & prev_topics.keys()):
            pre = prev_topics[name].pub_count
            post = curr_topics[name].pub_count
            if pre == post:
                continue
            out.append(TimelineEvent(
                t=curr.t,
                kind="derived",
                subsystem="ros",
                summary=(
                    f"publisher count changed on {name}: {pre} -> {post}"
                ),
                confidence=0.85,
                evidence_ref=f"snapshots.json#{idx}",
                data={
                    "topic": name,
                    "delta": "pub_count_changed",
                    "from": pre,
                    "to": post,
                },
                correlated_event_refs=[
                    f"snapshots.json#{idx - 1}",
                    f"snapshots.json#{idx}",
                ],
            ))

        # Node-set deltas.
        prev_nodes = set(prev.nodes)
        curr_nodes = set(curr.nodes)
        added_nodes = sorted(curr_nodes - prev_nodes)
        removed_nodes = sorted(prev_nodes - curr_nodes)
        for n in added_nodes:
            out.append(TimelineEvent(
                t=curr.t,
                kind="derived",
                subsystem="ros",
                summary=f"node {n} appeared on the graph",
                confidence=0.85,
                evidence_ref=f"snapshots.json#{idx}",
                data={"node": n, "delta": "node_appeared"},
                correlated_event_refs=[
                    f"snapshots.json#{idx - 1}",
                    f"snapshots.json#{idx}",
                ],
            ))
        for n in removed_nodes:
            out.append(TimelineEvent(
                t=curr.t,
                kind="derived",
                subsystem="ros",
                summary=f"node {n} disappeared from the graph",
                confidence=0.85,
                evidence_ref=f"snapshots.json#{idx}",
                data={"node": n, "delta": "node_disappeared"},
                correlated_event_refs=[
                    f"snapshots.json#{idx - 1}",
                    f"snapshots.json#{idx}",
                ],
            ))

    return out


# ---------------------------------------------------------------------------
# Resource excursions
# ---------------------------------------------------------------------------


# Each metric we monitor: which event_type it lives in, which data key
# carries the value, which threshold attribute on AnomalyThresholds
# governs it, and which subsystem the excursion belongs to.
_RESOURCE_RULES = (
    ("system.cpu", "cpu_percent", "cpu_percent", "system", "%"),
    ("system.memory", "memory_percent", "memory_percent", "system", "%"),
    ("system.gpu", "gpu_temp_c", "gpu_temp_c", "gpu", "C"),
)


def resource_excursions(
    events: Iterable[BlackBoxEvent],
    thresholds: AnomalyThresholds,
    *,
    sustain_sec: float = 3.0,
) -> list[TimelineEvent]:
    """Emit a derived event for sustained excursions above a threshold.

    For each metric, walk the time-ordered samples; when a contiguous
    run of samples above the configured limit lasts at least
    ``sustain_sec``, emit one derived event covering the run.

    A single isolated spike that returns to baseline within the sustain
    window does NOT emit. The sustain criterion intentionally undercounts
    rather than overcounts: noisy peaks are not what the operator wants
    to see in a postmortem.
    """
    if sustain_sec < 0:
        sustain_sec = 0.0

    # Index events by 1-based line so derived events can hyperlink back.
    indexed = [(line, ev) for line, ev in enumerate(events, start=1)]

    out: list[TimelineEvent] = []
    for event_type, data_key, threshold_attr, subsystem, unit in _RESOURCE_RULES:
        threshold = getattr(thresholds, threshold_attr, None)
        if threshold is None:
            continue

        samples: list[tuple[int, datetime, float]] = []
        for line, ev in indexed:
            if ev.event_type != event_type:
                continue
            value = ev.data.get(data_key)
            if not isinstance(value, (int, float)):
                continue
            samples.append((line, ev.timestamp, float(value)))

        # Walk samples; collect contiguous above-threshold runs.
        run: list[tuple[int, datetime, float]] = []
        for line, t, v in samples + [(None, None, -float("inf"))]:  # sentinel
            if v > threshold:
                run.append((line, t, v))
                continue
            if run:
                out_event = _excursion_event(
                    run, threshold, threshold_attr, subsystem, unit, sustain_sec
                )
                if out_event is not None:
                    out.append(out_event)
                run = []

    return out


def _excursion_event(
    run: list[tuple[int, datetime, float]],
    threshold: float,
    metric: str,
    subsystem: str,
    unit: str,
    sustain_sec: float,
) -> TimelineEvent | None:
    """Build one derived event for a contiguous run, if it lasted long enough."""
    if not run:
        return None
    first_line, first_t, _ = run[0]
    last_line, last_t, _ = run[-1]
    duration = (last_t - first_t).total_seconds()
    if duration < sustain_sec:
        return None
    peak = max(v for _, _, v in run)
    return TimelineEvent(
        t=first_t,
        kind="derived",
        subsystem=subsystem,  # type: ignore[arg-type]
        summary=(
            f"{metric} excursion: peak {peak:.1f}{unit}, duration "
            f"{duration:.1f}s above {threshold:.1f}{unit}"
        ),
        confidence=0.85,
        evidence_ref=f"events.jsonl#L{first_line}",
        data={
            "metric": metric,
            "threshold": threshold,
            "peak": peak,
            "duration_sec": duration,
            "samples": len(run),
        },
        correlated_event_refs=[
            f"events.jsonl#L{first_line}",
            f"events.jsonl#L{last_line}",
        ],
    )
