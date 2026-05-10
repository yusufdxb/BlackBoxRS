"""SystemSnapshotter projection tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.snapshots import SystemSnapshotter


_T0 = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _cpu(t: datetime, value: float) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=t,
        source="system_monitor",
        event_type="system.cpu",
        data={"cpu_percent": value},
        metadata={},
    )


def _mem(t: datetime, value: float) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=t,
        source="system_monitor",
        event_type="system.memory",
        data={"memory_percent": value},
        metadata={},
    )


def _qos(t: datetime, topic: str, pubs: int = 1, subs: int = 1) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=t,
        source="ros_monitor",
        event_type="ros.qos",
        data={
            "topic": topic,
            "msg_type": "std_msgs/msg/String",
            "publisher_count": pubs,
            "subscriber_count": subs,
            "publisher_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "VOLATILE"},
            ],
            "subscriber_qos_profiles": [],
        },
        metadata={},
    )


def _freq(t: datetime, topic: str, hz: float) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=t,
        source="ros_monitor",
        event_type="ros.frequency",
        data={"topic": topic, "frequency_hz": hz},
        metadata={},
    )


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_project_empty_window():
    out = SystemSnapshotter().project(
        [], _T0, _T0 + timedelta(seconds=5)
    )
    assert out == []


def test_project_skips_pre_first_event():
    """Snapshots before the first observed event must NOT appear; we
    refuse to emit padded-empty rows that look like data."""
    snap = SystemSnapshotter(cadence_sec=1.0)
    events = [_cpu(_T0 + timedelta(seconds=3), 50.0)]
    out = snap.project(events, _T0, _T0 + timedelta(seconds=5))
    # 1.0s cadence => candidate ticks at 0,1,2,3,4,5. Only ticks at >=3
    # have data, so we get 3 snapshots.
    assert len(out) == 3
    assert all(s.cpu_percent == 50.0 for s in out)


def test_project_carries_latest_values_forward():
    snap = SystemSnapshotter(cadence_sec=1.0)
    events = [
        _cpu(_T0 + timedelta(seconds=0), 30.0),
        _cpu(_T0 + timedelta(seconds=2), 80.0),
        _mem(_T0 + timedelta(seconds=1), 40.0),
    ]
    out = snap.project(events, _T0, _T0 + timedelta(seconds=3))
    assert out[0].cpu_percent == 30.0
    assert out[0].mem_percent is None  # mem not yet seen at t=0
    assert out[1].mem_percent == 40.0
    assert out[2].cpu_percent == 80.0


def test_project_topic_summary_populated_from_qos_and_freq():
    snap = SystemSnapshotter(cadence_sec=1.0)
    events = [
        _qos(_T0, "/scan", pubs=1, subs=1),
        _freq(_T0 + timedelta(seconds=1), "/scan", 10.0),
    ]
    out = snap.project(events, _T0, _T0 + timedelta(seconds=2))
    last = out[-1]
    assert len(last.topics) == 1
    t = last.topics[0]
    assert t.name == "/scan"
    assert t.pub_count == 1
    assert t.sub_count == 1
    assert t.last_freq_hz == 10.0
    assert t.qos_summary == "reliable/volatile"


def test_project_cadence_clamped_low():
    """Cadence < 0.5s should be clamped to 0.5s."""
    snap = SystemSnapshotter(cadence_sec=0.001)
    events = [_cpu(_T0, 30.0)]
    out = snap.project(events, _T0, _T0 + timedelta(seconds=2))
    # ~5 ticks at cadence 0.5s in a 2s window.
    assert 4 <= len(out) <= 5


def test_project_caps_at_max_snapshots():
    """Long windows must not blow up the snapshot list."""
    snap = SystemSnapshotter(cadence_sec=0.5)
    # 1 hour window; 0.5s cadence would naively be 7200 rows.
    events = [_cpu(_T0, 30.0)]
    out = snap.project(events, _T0, _T0 + timedelta(hours=1))
    assert len(out) <= 1024


def test_project_deterministic_on_same_inputs():
    events = [
        _cpu(_T0 + timedelta(seconds=0), 30.0),
        _cpu(_T0 + timedelta(seconds=2), 80.0),
        _qos(_T0 + timedelta(seconds=1), "/scan"),
    ]
    a = SystemSnapshotter(cadence_sec=1.0).project(events, _T0, _T0 + timedelta(seconds=3))
    b = SystemSnapshotter(cadence_sec=1.0).project(events, _T0, _T0 + timedelta(seconds=3))
    # Compare model dumps; identity of datetime objects differs but
    # serialised content must match.
    assert [s.model_dump(mode="json") for s in a] == \
           [s.model_dump(mode="json") for s in b]


def test_topics_sorted_by_name():
    snap = SystemSnapshotter(cadence_sec=1.0)
    events = [
        _qos(_T0, "/zeta", pubs=1, subs=1),
        _qos(_T0, "/alpha", pubs=1, subs=1),
    ]
    out = snap.project(events, _T0, _T0 + timedelta(seconds=1))
    assert [t.name for t in out[0].topics] == ["/alpha", "/zeta"]
