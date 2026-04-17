"""Tests for blackboxrs.logging.writer and blackboxrs.logging.reader."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.logging.writer import RotatingJsonlWriter
from blackboxrs.logging.reader import LogReader


def _make_event(
    ts: datetime | None = None,
    source: str = "system_monitor",
    event_type: str = "system.cpu",
) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=ts or datetime.now(timezone.utc),
        source=source,
        event_type=event_type,
        severity="info",
        data={"cpu_percent": 42.0, "cpu_count": 8},
    )


class TestRotatingJsonlWriter:
    """Test the JSONL writer."""

    def test_write_creates_file(self, tmp_log_dir: Path):
        writer = RotatingJsonlWriter(tmp_log_dir)
        writer.write(_make_event())
        writer.close()
        logs = list(tmp_log_dir.glob("blackboxrs_*.jsonl"))
        assert len(logs) == 1

    def test_write_multiple_events(self, tmp_log_dir: Path):
        writer = RotatingJsonlWriter(tmp_log_dir)
        for _ in range(5):
            writer.write(_make_event())
        writer.close()
        logs = list(tmp_log_dir.glob("blackboxrs_*.jsonl"))
        assert len(logs) == 1
        lines = logs[0].read_text().strip().split("\n")
        assert len(lines) == 5

    def test_written_events_are_valid_jsonl(self, tmp_log_dir: Path):
        writer = RotatingJsonlWriter(tmp_log_dir)
        original = _make_event()
        writer.write(original)
        writer.close()
        logs = list(tmp_log_dir.glob("blackboxrs_*.jsonl"))
        content = logs[0].read_text().strip()
        restored = BlackBoxEvent.from_jsonl(content)
        assert restored.event_type == original.event_type

    def test_close_is_idempotent(self, tmp_log_dir: Path):
        writer = RotatingJsonlWriter(tmp_log_dir)
        writer.write(_make_event())
        writer.close()
        writer.close()  # should not raise

    def test_rotations_within_same_second_do_not_collide(
        self, tmp_log_dir: Path
    ):
        """Regression: earlier filenames only had second resolution, so
        two or more rotations occurring in the same wall-clock second
        produced identical file paths and the second rotation silently
        appended to the first file.  The bug was reproducible locally
        by forcing repeated rotations in a tight loop.

        After the fix, every rotation must land in a distinct file."""
        writer = RotatingJsonlWriter(
            tmp_log_dir, max_file_mb=1, max_files=100
        )
        try:
            writer.write(_make_event())  # opens file #1
            for _ in range(4):
                writer._rotate()
                writer.write(_make_event())
        finally:
            writer.close()

        logs = sorted(tmp_log_dir.glob("blackboxrs_*.jsonl"))
        assert len(logs) == 5, (
            f"expected 5 distinct log files after 5 rotations, got {len(logs)}: "
            f"{[p.name for p in logs]}"
        )
        # Each file contains exactly one event line — if two rotations
        # had collided and appended to the same file we would see
        # multiples stacked up in one of them.
        for path in logs:
            lines = [
                ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            assert len(lines) == 1, (
                f"{path.name} has {len(lines)} lines, expected 1 "
                "(rotation collision reintroduced?)"
            )

    def test_rotation_preserves_size_accounting(self, tmp_log_dir: Path):
        """A fresh rotation must start with byte counter 0.  Under the
        previous bug the writer opened the existing (collided) file in
        append mode and then read ``st_size`` off the already-populated
        file, so ``_current_size`` came back non-zero on a 'new' file."""
        writer = RotatingJsonlWriter(
            tmp_log_dir, max_file_mb=1, max_files=100
        )
        try:
            writer.write(_make_event())
            writer._rotate()
            # Immediately after rotation the writer must have opened a
            # fresh, empty file.
            assert writer._current_size == 0
        finally:
            writer.close()


class TestLogReader:
    """Test the JSONL log reader."""

    def _write_events(
        self, log_dir: Path, events: list[BlackBoxEvent]
    ) -> None:
        writer = RotatingJsonlWriter(log_dir)
        for event in events:
            writer.write(event)
        writer.close()

    def test_read_all_returns_written_events(self, tmp_log_dir: Path):
        events = [_make_event() for _ in range(3)]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)
        read_back = list(reader.read_all())
        assert len(read_back) == 3

    def test_list_logs(self, tmp_log_dir: Path):
        self._write_events(tmp_log_dir, [_make_event()])
        reader = LogReader(tmp_log_dir)
        logs = reader.list_logs()
        assert len(logs) >= 1
        assert all(p.suffix == ".jsonl" for p in logs)

    def test_time_range_filtering(self, tmp_log_dir: Path):
        base = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        events = [
            _make_event(ts=base + timedelta(minutes=i))
            for i in range(5)
        ]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)

        # Only events from minute 1 to minute 3
        start = base + timedelta(minutes=1)
        end = base + timedelta(minutes=3)
        filtered = list(reader.read_all(start=start, end=end))
        assert len(filtered) == 3  # minutes 1, 2, 3

    def test_time_range_start_only(self, tmp_log_dir: Path):
        base = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        events = [
            _make_event(ts=base + timedelta(minutes=i))
            for i in range(5)
        ]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)
        start = base + timedelta(minutes=3)
        filtered = list(reader.read_all(start=start))
        assert len(filtered) == 2  # minutes 3 and 4

    def test_tail_returns_last_n_events(self, tmp_log_dir: Path):
        events = [_make_event() for _ in range(10)]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)
        tail = reader.tail(n=3)
        assert len(tail) == 3

    def test_tail_with_fewer_events_than_n(self, tmp_log_dir: Path):
        events = [_make_event() for _ in range(2)]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)
        tail = reader.tail(n=10)
        assert len(tail) == 2

    def test_filter_by_source(self, tmp_log_dir: Path):
        events = [
            _make_event(source="ros_monitor", event_type="ros.frequency"),
            _make_event(source="system_monitor", event_type="system.cpu"),
            _make_event(source="ros_monitor", event_type="ros.frequency"),
        ]
        self._write_events(tmp_log_dir, events)
        reader = LogReader(tmp_log_dir)
        ros_only = list(
            LogReader.filter_by_source(reader.read_all(), "ros_monitor")
        )
        assert len(ros_only) == 2
        assert all(e.source == "ros_monitor" for e in ros_only)

    def test_filter_by_severity(self, tmp_log_dir: Path):
        e1 = _make_event()
        e1_warning = BlackBoxEvent(
            timestamp=datetime.now(timezone.utc),
            source="anomaly_engine",
            event_type="anomaly.threshold",
            severity="warning",
            data={"detector": "threshold", "metric": "cpu_percent", "value": 95.0,
                  "threshold": 90.0, "message": "test"},
        )
        self._write_events(tmp_log_dir, [e1, e1_warning])
        reader = LogReader(tmp_log_dir)
        warnings = list(
            LogReader.filter_by_severity(reader.read_all(), "warning")
        )
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"

    def test_empty_log_dir(self, tmp_log_dir: Path):
        reader = LogReader(tmp_log_dir)
        assert list(reader.read_all()) == []
        assert reader.tail() == []
        assert reader.list_logs() == []
