"""Tests for the CLI log-reading and log-following helpers.

Focuses on the rotation-aware behaviour of ``_iter_tail_events`` and
the streaming ``_read_recent_events`` path.  The click entry points
themselves are thin shells over these helpers, so the helpers are the
interesting surface.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blackboxrs.cli import app as cli_app
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.logging.writer import RotatingJsonlWriter


def _ev(event_type: str = "system.cpu") -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=datetime.now(timezone.utc),
        source="system_monitor",
        event_type=event_type,
        severity="info",
        data={"cpu_percent": 1.0},
    )


# ---------------------------------------------------------------------------
# _latest_log_file_in
# ---------------------------------------------------------------------------


class TestLatestLogFileIn:
    def test_empty_dir_returns_none(self, tmp_path: Path):
        assert cli_app._latest_log_file_in(tmp_path) is None

    def test_picks_newest_by_filename_order(self, tmp_path: Path):
        a = tmp_path / "blackboxrs_20260401_120000_000000.jsonl"
        b = tmp_path / "blackboxrs_20260402_120000_000000.jsonl"
        a.write_text("", encoding="utf-8")
        b.write_text("", encoding="utf-8")
        assert cli_app._latest_log_file_in(tmp_path) == b

    def test_ignores_unrelated_jsonl_files(self, tmp_path: Path):
        """The glob must match our writer's pattern, not any *.jsonl.

        Previously the CLI used a looser glob that could pick up an
        arbitrary user-dropped file and tail it instead of our logs.
        """
        (tmp_path / "unrelated.jsonl").write_text("garbage\n", encoding="utf-8")
        mine = tmp_path / "blackboxrs_20260401_120000_000000.jsonl"
        mine.write_text("", encoding="utf-8")
        assert cli_app._latest_log_file_in(tmp_path) == mine


# ---------------------------------------------------------------------------
# _iter_tail_events — rotation-aware follower
# ---------------------------------------------------------------------------


class TestIterTailEvents:
    def test_yields_events_appended_after_start(self, tmp_path: Path):
        writer = RotatingJsonlWriter(tmp_path, max_file_mb=10, max_files=5)
        # Seed one event so there is an active log file to tail.
        writer.write(_ev())

        stop = threading.Event()
        seen: list[BlackBoxEvent] = []

        def _run() -> None:
            for ev in cli_app._iter_tail_events(
                tmp_path, idle_sleep=0.05, stop_event=stop
            ):
                seen.append(ev)
                if len(seen) >= 3:
                    stop.set()
                    return

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # Give the tail thread a moment to open the file and seek to EOF.
        time.sleep(0.2)

        # Now write three events that the tail must pick up.
        for _ in range(3):
            writer.write(_ev())
            time.sleep(0.05)

        thread.join(timeout=3.0)
        writer.close()

        assert not thread.is_alive(), "tail thread did not terminate"
        assert len(seen) >= 3

    def test_follows_rotation_to_newer_file(self, tmp_path: Path):
        """Regression: the original _follow_log resolved the 'latest'
        file once at start and kept tailing it after rotation.  Events
        appended to the new file were never delivered."""
        writer = RotatingJsonlWriter(tmp_path, max_file_mb=10, max_files=5)
        writer.write(_ev("system.cpu"))
        files_before = set(tmp_path.glob("blackboxrs_*.jsonl"))

        stop = threading.Event()
        seen: list[BlackBoxEvent] = []

        def _run() -> None:
            for ev in cli_app._iter_tail_events(
                tmp_path, idle_sleep=0.05, stop_event=stop
            ):
                seen.append(ev)
                if any(e.event_type == "system.memory" for e in seen):
                    stop.set()
                    return

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.2)  # tail attaches to first file and seeks to EOF

        # Force a rotation, then write into the new file.
        writer._rotate()
        files_after = set(tmp_path.glob("blackboxrs_*.jsonl"))
        assert files_after - files_before, (
            "rotation did not create a new file — test setup invalid"
        )
        writer.write(_ev("system.memory"))

        thread.join(timeout=3.0)
        writer.close()

        assert not thread.is_alive(), "tail thread did not terminate"
        assert any(
            e.event_type == "system.memory" for e in seen
        ), "tail did not follow the rotation to the new file"

    def test_empty_dir_yields_nothing_and_returns(self, tmp_path: Path):
        out = list(cli_app._iter_tail_events(tmp_path))
        assert out == []


# ---------------------------------------------------------------------------
# _read_recent_events — streaming tail of the last N events
# ---------------------------------------------------------------------------


class TestReadRecentEvents:
    def test_returns_empty_when_log_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "does_not_exist"
        monkeypatch.setattr(
            cli_app.BlackBoxConfig,
            "load",
            classmethod(
                lambda cls, *a, **kw: cls(log_dir=str(missing))
            ),
        )
        assert cli_app._read_recent_events(count=5) == []

    def test_returns_last_n_in_chronological_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        writer = RotatingJsonlWriter(log_dir)
        for i in range(10):
            writer.write(
                BlackBoxEvent(
                    timestamp=datetime(
                        2026, 4, 5, 12, 0, i, tzinfo=timezone.utc
                    ),
                    source="system_monitor",
                    event_type="system.cpu",
                    severity="info",
                    data={"cpu_percent": float(i)},
                )
            )
        writer.close()

        monkeypatch.setattr(
            cli_app.BlackBoxConfig,
            "load",
            classmethod(
                lambda cls, *a, **kw: cls(log_dir=str(log_dir))
            ),
        )

        result = cli_app._read_recent_events(count=3)
        assert len(result) == 3
        assert [e.data["cpu_percent"] for e in result] == [7.0, 8.0, 9.0]
