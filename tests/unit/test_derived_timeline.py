"""Tests for derived timeline events: silence_intervals, graph_deltas,
resource_excursions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackboxrs.core.config import AnomalyThresholds
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.incident.derived import (
    graph_deltas,
    resource_excursions,
    silence_intervals,
)
from blackboxrs.incident.models import (
    SystemSnapshot,
    TopicSnapshot,
)


_T0 = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _freq(t_offset: float, topic: str, hz: float = 10.0) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=_T0 + timedelta(seconds=t_offset),
        source="ros_monitor",
        event_type="ros.frequency",
        data={"topic": topic, "frequency_hz": hz},
        metadata={},
    )


def _cpu(t_offset: float, value: float) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=_T0 + timedelta(seconds=t_offset),
        source="system_monitor",
        event_type="system.cpu",
        data={"cpu_percent": value},
        metadata={},
    )


# ---------------------------------------------------------------------------
# silence_intervals
# ---------------------------------------------------------------------------


def test_silence_emits_when_gap_exceeds_timeout():
    events = [_freq(0, "/scan"), _freq(8, "/scan")]
    out = silence_intervals(events, timeout_sec=5.0)
    assert len(out) == 1
    de = out[0]
    assert de.kind == "derived"
    assert de.subsystem == "ros"
    assert "silence interval" in de.summary
    assert de.confidence == 0.95
    assert de.data["topic"] == "/scan"
    assert de.data["gap_sec"] == 8.0
    assert de.evidence_ref == "events.jsonl#L1"
    assert de.correlated_event_refs == ["events.jsonl#L1", "events.jsonl#L2"]


def test_silence_no_event_when_within_tolerance():
    events = [_freq(0, "/scan"), _freq(2, "/scan"), _freq(4, "/scan")]
    out = silence_intervals(events, timeout_sec=5.0)
    assert out == []


def test_silence_per_topic_independent():
    events = [
        _freq(0, "/a"), _freq(10, "/a"),
        _freq(0, "/b"), _freq(2, "/b"),
    ]
    out = silence_intervals(events, timeout_sec=5.0)
    assert len(out) == 1
    assert out[0].data["topic"] == "/a"


def test_silence_zero_timeout_returns_empty():
    events = [_freq(0, "/scan"), _freq(10, "/scan")]
    out = silence_intervals(events, timeout_sec=0.0)
    assert out == []


# ---------------------------------------------------------------------------
# graph_deltas
# ---------------------------------------------------------------------------


def _snapshot(t_offset: float, topics: list[tuple[str, int]] | None = None,
              nodes: list[str] | None = None) -> SystemSnapshot:
    return SystemSnapshot(
        t=_T0 + timedelta(seconds=t_offset),
        host="h",
        topics=[
            TopicSnapshot(name=name, pub_count=pubs)
            for name, pubs in (topics or [])
        ],
        nodes=nodes or [],
    )


def test_graph_delta_topic_appeared_disappeared():
    snaps = [
        _snapshot(0, topics=[("/a", 1)]),
        _snapshot(1, topics=[("/a", 1), ("/b", 1)]),
        _snapshot(2, topics=[("/b", 1)]),
    ]
    out = graph_deltas(snaps)
    summaries = [e.summary for e in out]
    assert any("/b appeared" in s for s in summaries)
    assert any("/a disappeared" in s for s in summaries)


def test_graph_delta_pub_count_change():
    snaps = [
        _snapshot(0, topics=[("/a", 1)]),
        _snapshot(1, topics=[("/a", 0)]),
    ]
    out = graph_deltas(snaps)
    assert len(out) == 1
    assert out[0].data == {
        "topic": "/a", "delta": "pub_count_changed", "from": 1, "to": 0,
    }
    assert out[0].confidence == 0.85


def test_graph_delta_nodes():
    snaps = [
        _snapshot(0, nodes=["/a", "/b"]),
        _snapshot(1, nodes=["/a"]),
    ]
    out = graph_deltas(snaps)
    assert any("/b disappeared" in e.summary for e in out)


def test_graph_delta_no_snapshots():
    assert graph_deltas([]) == []
    assert graph_deltas([_snapshot(0)]) == []


# ---------------------------------------------------------------------------
# resource_excursions
# ---------------------------------------------------------------------------


def test_resource_excursion_sustained_emits():
    thresholds = AnomalyThresholds(cpu_percent=50.0)
    events = [_cpu(i, 90.0) for i in range(5)]  # 0..4s sustained
    out = resource_excursions(events, thresholds, sustain_sec=3.0)
    assert len(out) == 1
    de = out[0]
    assert de.kind == "derived"
    assert de.subsystem == "system"
    assert "excursion" in de.summary
    assert de.data["peak"] == 90.0
    assert de.data["duration_sec"] >= 3.0


def test_resource_excursion_isolated_spike_does_not_emit():
    thresholds = AnomalyThresholds(cpu_percent=50.0)
    events = [
        _cpu(0, 30.0),
        _cpu(1, 90.0),  # single spike
        _cpu(2, 30.0),
    ]
    out = resource_excursions(events, thresholds, sustain_sec=3.0)
    assert out == []


def test_resource_excursion_under_threshold_silent():
    thresholds = AnomalyThresholds(cpu_percent=99.0)
    events = [_cpu(i, 50.0) for i in range(5)]
    out = resource_excursions(events, thresholds, sustain_sec=3.0)
    assert out == []


def test_resource_excursion_multiple_runs():
    thresholds = AnomalyThresholds(cpu_percent=50.0)
    events = [
        _cpu(0, 60.0), _cpu(1, 70.0), _cpu(2, 80.0), _cpu(3, 75.0),
        _cpu(4, 30.0),
        _cpu(5, 90.0), _cpu(6, 92.0), _cpu(7, 91.0), _cpu(8, 88.0),
    ]
    out = resource_excursions(events, thresholds, sustain_sec=2.0)
    assert len(out) == 2
    assert out[0].data["peak"] == 80.0
    assert out[1].data["peak"] == 92.0


def test_resource_excursion_gpu_subsystem():
    thresholds = AnomalyThresholds(gpu_temp_c=70.0)
    events = [
        BlackBoxEvent(
            timestamp=_T0 + timedelta(seconds=i),
            source="system_monitor",
            event_type="system.gpu",
            data={"gpu_temp_c": 85.0},
            metadata={},
        )
        for i in range(5)
    ]
    out = resource_excursions(events, thresholds, sustain_sec=3.0)
    assert len(out) == 1
    assert out[0].subsystem == "gpu"
