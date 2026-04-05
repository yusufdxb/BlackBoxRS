"""JSONL log reader and replay utilities for BlackBoxRS.

Provides forward-streaming, time-filtered reads over the structured
JSONL files produced by :class:`RotatingJsonlWriter`, as well as
convenience helpers for tailing and filtering.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Iterator

from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)


class LogReader:
    """Reads and replays JSONL log files written by the logging pipeline.

    All methods stream lazily where possible so that arbitrarily large
    log sets can be processed without loading everything into memory.

    Args:
        log_dir: Directory containing ``blackboxrs_*.jsonl`` files.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir

    # -- Discovery ------------------------------------------------------------

    def list_logs(self) -> list[Path]:
        """Return available log files sorted oldest-first.

        The embedded UTC timestamp in each filename ensures that
        lexicographic order equals chronological order.

        Returns:
            A sorted list of :class:`Path` objects.
        """
        return sorted(self._log_dir.glob("blackboxrs_*.jsonl"))

    # -- Single-file reading --------------------------------------------------

    def read_log(
        self,
        path: Path,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[BlackBoxEvent]:
        """Stream events from a single log file, optionally filtered by time.

        Lines that fail to parse are silently skipped (a warning is
        logged).

        Args:
            path: Path to the JSONL file.
            start: If provided, only yield events at or after this time.
            end: If provided, only yield events at or before this time.

        Yields:
            :class:`BlackBoxEvent` instances in file order.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = BlackBoxEvent.from_jsonl(stripped)
                    except Exception:
                        logger.warning(
                            "Skipping malformed line in %s: %.80s", path, stripped
                        )
                        continue

                    if start is not None and event.timestamp < start:
                        continue
                    if end is not None and event.timestamp > end:
                        continue

                    yield event
        except OSError:
            logger.warning("Could not open log file: %s", path)

    # -- Multi-file reading ---------------------------------------------------

    def read_all(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[BlackBoxEvent]:
        """Stream events from all logs in chronological order.

        Iterates over every file returned by :meth:`list_logs` and
        applies the optional time-range filter.

        Args:
            start: If provided, only yield events at or after this time.
            end: If provided, only yield events at or before this time.

        Yields:
            :class:`BlackBoxEvent` instances in chronological order.
        """
        for path in self.list_logs():
            yield from self.read_log(path, start=start, end=end)

    # -- Tail -----------------------------------------------------------------

    def tail(self, n: int = 50) -> list[BlackBoxEvent]:
        """Return the last *n* events across all log files.

        Streams through files in reverse order, collecting events into
        a bounded buffer, then returns them in chronological order.

        Args:
            n: Number of trailing events to return.

        Returns:
            A list of up to *n* events, oldest first.
        """
        buffer: deque[BlackBoxEvent] = deque(maxlen=n)
        for path in self.list_logs():
            for event in self.read_log(path):
                buffer.append(event)
        return list(buffer)

    # -- Filters --------------------------------------------------------------

    @staticmethod
    def filter_by_source(
        events: Iterator[BlackBoxEvent],
        source: str,
    ) -> Iterator[BlackBoxEvent]:
        """Yield only events matching the given source.

        Args:
            events: An iterator of events to filter.
            source: The ``source`` field value to match
                (e.g. ``"ros_monitor"``).

        Yields:
            Events whose ``source`` equals *source*.
        """
        for event in events:
            if event.source == source:
                yield event

    @staticmethod
    def filter_by_severity(
        events: Iterator[BlackBoxEvent],
        severity: str,
    ) -> Iterator[BlackBoxEvent]:
        """Yield only events matching the given severity.

        Args:
            events: An iterator of events to filter.
            severity: The ``severity`` field value to match
                (e.g. ``"warning"``).

        Yields:
            Events whose ``severity`` equals *severity*.
        """
        for event in events:
            if event.severity == severity:
                yield event
