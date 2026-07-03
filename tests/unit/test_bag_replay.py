"""Tests for the virtual clock and offline bag-replay path."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from blackboxrs.core.clock import Clock

pytest.importorskip("mcap")

from blackboxrs.recording.bag_replay import (  # noqa: E402
    read_bag_arrivals,
    replay_bag,
)

REPO = Path(__file__).resolve().parents[2]
BAG = REPO / "examples" / "bags" / "go2_sim_odom_imu.mcap"
DROP_TOPIC = "/source/utlidar/robot_odom"


# -- Virtual clock ---------------------------------------------------------


def test_clock_defaults_to_wall_time():
    Clock.use_wall_clock()
    assert not Clock.is_virtual()
    before = datetime.now(timezone.utc)
    now = Clock.now()
    assert (now - before).total_seconds() < 5.0


def test_clock_virtual_time_pins_now():
    pinned = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    try:
        Clock.set_virtual_time(pinned)
        assert Clock.is_virtual()
        assert Clock.now() == pinned
    finally:
        Clock.use_wall_clock()
    assert not Clock.is_virtual()
    assert Clock.now() != pinned


def test_clock_virtual_time_assumes_utc_for_naive():
    naive = datetime(2020, 1, 1, 0, 0, 0)
    try:
        Clock.set_virtual_time(naive)
        assert Clock.now().tzinfo is not None
    finally:
        Clock.use_wall_clock()


# -- read_bag_arrivals -----------------------------------------------------


def test_read_bag_arrivals_sorted_and_nonempty():
    arrivals = read_bag_arrivals(BAG)
    assert arrivals, "vendored bag should contain messages"
    times = [ns for _t, ns in arrivals]
    assert times == sorted(times)
    topics = {t for t, _ in arrivals}
    assert DROP_TOPIC in topics


# -- replay_bag ------------------------------------------------------------


def test_replay_without_injection_is_clean(tmp_path):
    """A healthy replay of the real bag raises no dead-topic anomaly."""
    result = replay_bag(
        BAG,
        tmp_path / "log.jsonl",
        session_id="clean",
        dead_topic_timeout_sec=3.0,
    )
    assert result.event_count > 0
    assert result.dropped_topic is None
    assert result.anomalies == []
    # Clock must be restored regardless of the replay.
    assert not Clock.is_virtual()


def test_injected_dropout_fires_real_detector(tmp_path):
    result = replay_bag(
        BAG,
        tmp_path / "log.jsonl",
        session_id="odom_dropout",
        dead_topic_timeout_sec=3.0,
        drop_topic=DROP_TOPIC,
        drop_after_sec=5.0,
    )
    assert len(result.anomalies) == 1
    anomaly = result.anomalies[0]
    assert anomaly.event_type == "anomaly.dead_topic"
    assert anomaly.data["topic"] == DROP_TOPIC
    assert anomaly.data["value"] >= 3.0
    # The anomaly carries real detector provenance for the incident builder.
    assert anomaly.metadata["signature_fields"] == ["topic"]
    assert "DeadTopicDetector" in anomaly.metadata["detector_class"]
    # Anomaly is stamped at bag time, not wall time.
    assert anomaly.timestamp.year == 2026
    assert not Clock.is_virtual()


def test_dropped_topic_messages_are_truncated(tmp_path):
    full = replay_bag(
        BAG, tmp_path / "a.jsonl", session_id="full",
        dead_topic_timeout_sec=3.0,
    )
    dropped = replay_bag(
        BAG, tmp_path / "b.jsonl", session_id="drop",
        dead_topic_timeout_sec=3.0,
        drop_topic=DROP_TOPIC, drop_after_sec=5.0,
    )
    assert dropped.topics[DROP_TOPIC] < full.topics[DROP_TOPIC]


def test_drop_topic_requires_drop_after(tmp_path):
    with pytest.raises(ValueError):
        replay_bag(
            BAG, tmp_path / "x.jsonl", session_id="bad",
            drop_topic=DROP_TOPIC,
        )


def test_unsupported_bag_format_rejected(tmp_path):
    bad = tmp_path / "bag.bag"
    bad.write_bytes(b"")
    with pytest.raises(ValueError):
        read_bag_arrivals(bad)


# -- .db3 (sqlite3) storage ------------------------------------------------


def _write_db3(path: Path, rows: list[tuple[str, int]]) -> None:
    """Build a minimal rosbag2 sqlite3 bag with the given (topic, ts) rows.

    Schema mirrors real rosbag2 db3 bags (verified against a recorded
    session): ``topics(id, name, ...)`` and ``messages(topic_id,
    timestamp, ...)`` with nanosecond timestamps.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT,"
        " serialization_format TEXT, offered_qos_profiles TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER,"
        " timestamp INTEGER, data BLOB);"
    )
    topic_ids: dict[str, int] = {}
    for topic, _ts in rows:
        if topic not in topic_ids:
            tid = len(topic_ids) + 1
            topic_ids[topic] = tid
            conn.execute(
                "INSERT INTO topics VALUES (?,?,?,?,?)",
                (tid, topic, "std_msgs/msg/Empty", "cdr", ""),
            )
    for topic, ts in rows:
        conn.execute(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
            (topic_ids[topic], ts, b""),
        )
    conn.commit()
    conn.close()


def test_read_db3_arrivals_sorted(tmp_path):
    db3 = tmp_path / "bag_0.db3"
    _write_db3(db3, [("/a", 300), ("/b", 100), ("/a", 200)])
    arrivals = read_bag_arrivals(db3)
    assert arrivals == [("/b", 100), ("/a", 200), ("/a", 300)]


def test_db3_injected_dropout_fires_detector(tmp_path):
    # /keep publishes at 10 Hz for 8 s; /die publishes for the first 3 s only.
    db3 = tmp_path / "bag_0.db3"
    rows: list[tuple[str, int]] = []
    step = 100_000_000  # 0.1 s in ns
    for i in range(80):
        rows.append(("/keep", i * step))
        if i < 30:
            rows.append(("/die", i * step))
    _write_db3(db3, rows)

    result = replay_bag(
        db3, tmp_path / "log.jsonl", session_id="db3",
        dead_topic_timeout_sec=2.0,
    )
    assert len(result.anomalies) == 1
    assert result.anomalies[0].data["topic"] == "/die"
    assert not Clock.is_virtual()
