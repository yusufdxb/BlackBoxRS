"""Offline replay of a recorded rosbag2 (``.mcap``) through the detectors.

Where :mod:`blackboxrs.recording.rosbag2` *records* a bounded capture when
an anomaly fires live, this module goes the other way: it takes a bag that
was already recorded and replays it through the **real**
:class:`~blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector`
so an incident bundle can be built entirely offline, with no running robot
or DDS graph.

The two ingredients that make the replay meaningful:

* **Bag-time clock.** Each message is replayed as a ``ros.frequency``
  topic-arrival event stamped at the message's recorded timestamp, and the
  central :class:`~blackboxrs.core.clock.Clock` is pinned to that same
  instant.  Time-based detectors therefore measure elapsed intervals
  against recorded time, not wall time, so a replay that finishes in
  milliseconds still reproduces a multi-second silence.

* **Fault injection.** A real recorded bag is usually healthy.  To exercise
  the detector, one topic can be *silenced* after a cutoff (``drop_topic`` /
  ``drop_after_sec``): its messages past the cutoff are dropped while every
  other topic keeps publishing and keeps advancing the clock.  The dropout
  is injected into otherwise-real data; the emitted anomaly is genuinely
  produced by the detector, not hand-written.  Callers are expected to say
  so in the bundle notes.

The output is a JSONL event log (healthy topic-arrival samples plus any
detected anomaly) that :func:`blackboxrs.incident.api.build_incident`
consumes directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.anomaly_engine.detectors.dead_topic import DeadTopicDetector
from blackboxrs.core.clock import Clock
from blackboxrs.core.config import DeadTopicConfig
from blackboxrs.core.schemas import BlackBoxEvent

_NS_PER_SEC = 1_000_000_000


@dataclass
class ReplayResult:
    """Summary of a single bag replay."""

    log_path: Path
    window_start: datetime
    window_end: datetime
    event_count: int
    topics: dict[str, int]
    dropped_topic: str | None = None
    drop_time: datetime | None = None
    anomalies: list[BlackBoxEvent] = field(default_factory=list)


def _read_mcap_arrivals(path: Path) -> list[tuple[str, int]]:
    """Read ``(topic, log_time_ns)`` from a rosbag2 ``.mcap`` file."""
    from mcap.reader import make_reader  # local import: optional dependency

    arrivals: list[tuple[str, int]] = []
    with open(path, "rb") as fh:
        reader = make_reader(fh)
        for _schema, channel, message in reader.iter_messages():
            if channel.topic is None:
                continue
            arrivals.append((channel.topic, message.log_time))
    return arrivals


def _read_db3_arrivals(path: Path) -> list[tuple[str, int]]:
    """Read ``(topic, timestamp_ns)`` from a rosbag2 sqlite3 ``.db3`` file.

    Uses only stdlib :mod:`sqlite3`; the rosbag2 sqlite schema stores the
    topic name in ``topics.name`` and the per-message nanosecond timestamp
    in ``messages.timestamp``, so no ROS packages or deserialization are
    needed.
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT t.name, m.timestamp "
            "FROM messages m JOIN topics t ON m.topic_id = t.id"
        ).fetchall()
    finally:
        conn.close()
    return [(name, int(ts)) for name, ts in rows if name is not None]


def _split_db3_files(dir_path: Path) -> list[Path]:
    """Return the ``.db3`` files that make up a rosbag2 recording directory.

    A long or large recording is split by ``ros2 bag record`` into several
    sequentially-numbered ``.db3`` files (``<name>_0.db3``, ``<name>_1.db3``,
    ...) once a size/duration threshold is hit; all of them belong to the
    same bag and must be read together or messages are silently dropped.
    ``metadata.yaml``'s ``relative_file_paths`` is the authoritative list
    and is preferred; a sorted glob is the fallback when metadata is
    missing or unreadable.
    """
    metadata_path = dir_path / "metadata.yaml"
    if metadata_path.exists():
        import yaml

        with open(metadata_path, "r", encoding="utf-8") as fh:
            metadata = yaml.safe_load(fh)
        try:
            rel_paths = metadata["rosbag2_bagfile_information"][
                "relative_file_paths"
            ]
        except (KeyError, TypeError):
            rel_paths = None
        if rel_paths:
            return [dir_path / rel for rel in rel_paths]
    return sorted(dir_path.glob("*.db3"))


def read_bag_arrivals(bag_path: str | Path) -> list[tuple[str, int]]:
    """Return ``(topic, timestamp_ns)`` for every message in a rosbag2 bag.

    Accepts either a single storage file or a rosbag2 recording directory:

    * ``.mcap`` file: read directly (needs the optional ``mcap``
      dependency).
    * ``.db3`` file: read directly with stdlib ``sqlite3``.
    * Directory: treated as a rosbag2 recording. If it contains an
      ``.mcap`` file, that is read. Otherwise every ``.db3`` split file
      listed in ``metadata.yaml`` (or found by glob) is read and merged,
      so a bag that ``ros2 bag record`` split across multiple ``.db3``
      files is replayed in full rather than truncated to the first file.

    Only the topic name and message timestamp are read, so no message
    deserialization (and no ROS message packages) is required. The
    returned list is sorted ascending by timestamp.

    Args:
        bag_path: Path to a rosbag2 ``.mcap``/``.db3`` file, or a rosbag2
            recording directory.

    Returns:
        Arrival tuples sorted by ``timestamp_ns``.

    Raises:
        ValueError: If the path is not a supported storage format, or a
            directory with no readable ``.mcap``/``.db3`` bag files.
    """
    bag_path = Path(bag_path)

    if bag_path.is_dir():
        mcap_files = sorted(bag_path.glob("*.mcap"))
        if mcap_files:
            arrivals = _read_mcap_arrivals(mcap_files[0])
        else:
            db3_files = _split_db3_files(bag_path)
            if not db3_files:
                raise ValueError(
                    f"No .mcap or .db3 bag files found under {bag_path}"
                )
            arrivals = []
            for db3_file in db3_files:
                arrivals.extend(_read_db3_arrivals(db3_file))
    else:
        suffix = bag_path.suffix.lower()
        if suffix == ".mcap":
            arrivals = _read_mcap_arrivals(bag_path)
        elif suffix == ".db3":
            arrivals = _read_db3_arrivals(bag_path)
        else:
            raise ValueError(
                f"Unsupported bag format {suffix!r} (expected .mcap or .db3)"
            )

    arrivals.sort(key=lambda item: item[1])
    return arrivals


def _ns_to_dt(ns: int) -> datetime:
    """Convert integer nanoseconds since the epoch to a UTC datetime."""
    return datetime.fromtimestamp(ns / _NS_PER_SEC, tz=timezone.utc)


def replay_bag(
    mcap_path: str | Path,
    log_path: str | Path,
    *,
    session_id: str,
    dead_topic_timeout_sec: float = 2.0,
    drop_topic: str | None = None,
    drop_after_sec: float | None = None,
    ignore_topics: tuple[str, ...] = ("/parameter_events", "/rosout"),
    max_duration_sec: float | None = None,
) -> ReplayResult:
    """Replay a bag through the real dead-topic detector and write a log.

    Args:
        mcap_path: Path to the recorded bag (``.mcap``/``.db3`` file or a
            rosbag2 recording directory).
        log_path: Destination JSONL log (created/overwritten).
        session_id: Session id stamped on every emitted event.
        dead_topic_timeout_sec: Silence threshold for the detector.
        drop_topic: If set, silence this topic after ``drop_after_sec``
            (fault injection).  Messages on the topic past the cutoff are
            not replayed.
        drop_after_sec: Seconds after bag start at which ``drop_topic``
            goes silent.  Required when ``drop_topic`` is set.
        ignore_topics: Topics not treated as liveness signals (bag/rosout
            bookkeeping), skipped from replay.
        max_duration_sec: If set, only replay messages within this many
            seconds of the bag's first message; every later message is
            dropped from the replay entirely (not just the faulted topic).
            This trims a long real-world recording down to a short,
            reviewable window without altering the timing or content of
            the messages that remain; it is a size/readability cut on the
            checked-in demo artifact, not a fabrication of any kind.

    Returns:
        A :class:`ReplayResult` describing the window and detected anomalies.

    Raises:
        ValueError: If the bag has no replayable messages, or if
            ``drop_topic`` is given without ``drop_after_sec``.
    """
    if drop_topic is not None and drop_after_sec is None:
        raise ValueError("drop_after_sec is required when drop_topic is set")

    arrivals = read_bag_arrivals(mcap_path)
    arrivals = [(t, ns) for t, ns in arrivals if t not in ignore_topics]
    if not arrivals:
        raise ValueError(f"No replayable messages in {mcap_path}")

    start_ns = arrivals[0][1]
    if max_duration_sec is not None:
        end_cutoff_ns = start_ns + int(max_duration_sec * _NS_PER_SEC)
        arrivals = [(t, ns) for t, ns in arrivals if ns <= end_cutoff_ns]
    window_start = _ns_to_dt(start_ns)
    drop_dt: datetime | None = None
    cutoff_ns: int | None = None
    if drop_topic is not None:
        cutoff_ns = start_ns + int(drop_after_sec * _NS_PER_SEC)  # type: ignore[arg-type]
        drop_dt = _ns_to_dt(cutoff_ns)

    detector = DeadTopicDetector(DeadTopicConfig(timeout_sec=dead_topic_timeout_sec))

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    topics: dict[str, int] = {}
    last_seen_ns: dict[str, int] = {}
    anomalies: list[BlackBoxEvent] = []
    event_count = 0
    last_ns = start_ns

    Clock.use_wall_clock()
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            for topic, ns in arrivals:
                # Fault injection: silence the dropped topic past the cutoff.
                if (
                    drop_topic is not None
                    and topic == drop_topic
                    and cutoff_ns is not None
                    and ns > cutoff_ns
                ):
                    continue

                last_ns = ns
                Clock.set_virtual_time(_ns_to_dt(ns))

                # Instantaneous frequency from the previous arrival on this
                # topic (real bag intervals, not a fabricated rate).
                data: dict[str, object] = {"topic": topic, "source": "bag_replay"}
                prev = last_seen_ns.get(topic)
                if prev is not None:
                    interval_ms = (ns - prev) / 1_000_000
                    data["interval_ms"] = interval_ms
                    # Report a rate only for physically meaningful intervals.
                    # Sub-millisecond spacing is the recorder flushing its
                    # initial buffer, not a real topic rate, so we leave
                    # frequency_hz unset rather than emit e.g. 150 kHz.
                    if interval_ms >= 1.0:
                        data["frequency_hz"] = 1000.0 / interval_ms
                last_seen_ns[topic] = ns

                arrival = BlackBoxEvent.ros_event(
                    "ros.frequency", data, session_id=session_id,
                )
                fh.write(arrival.to_jsonl())
                fh.write("\n")
                event_count += 1
                topics[topic] = topics.get(topic, 0) + 1

                anomaly = detector.check(arrival)
                if anomaly is not None:
                    fh.write(anomaly.to_jsonl())
                    fh.write("\n")
                    anomalies.append(anomaly)
    finally:
        Clock.use_wall_clock()

    window_end = _ns_to_dt(last_ns) + timedelta(seconds=1)
    return ReplayResult(
        log_path=log_path,
        window_start=window_start,
        window_end=window_end,
        event_count=event_count,
        topics=topics,
        dropped_topic=drop_topic,
        drop_time=drop_dt,
        anomalies=anomalies,
    )
