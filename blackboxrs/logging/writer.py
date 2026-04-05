"""Rotating JSONL log writer for BlackBoxRS.

Writes serialised :class:`BlackBoxEvent` instances to timestamped JSONL
files, automatically rotating when a file exceeds the configured size
and cleaning up the oldest files when the maximum count is reached.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path

from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)


def _log_filename() -> str:
    """Generate a log filename using the current UTC time.

    Returns:
        A filename in the format ``blackboxrs_YYYYMMDD_HHMMSS.jsonl``.
    """
    now = datetime.now(timezone.utc)
    return f"blackboxrs_{now.strftime('%Y%m%d_%H%M%S')}.jsonl"


class RotatingJsonlWriter:
    """Writes JSONL events to rotating log files.

    Each call to :meth:`write` appends a single JSON line to the
    current log file.  When the file exceeds *max_file_mb* megabytes
    a new file is opened.  When more than *max_files* log files exist
    the oldest are deleted.

    Args:
        log_dir: Directory where log files are written.  Created
            automatically if it does not exist.
        max_file_mb: Maximum size of a single log file in megabytes
            before rotation occurs.
        max_files: Maximum number of log files to retain.
    """

    def __init__(
        self,
        log_dir: Path,
        max_file_mb: int = 50,
        max_files: int = 20,
    ) -> None:
        self._log_dir = Path(os.path.expanduser(log_dir))
        self._max_bytes = max_file_mb * 1024 * 1024
        self._max_files = max_files
        self._current_file: TextIOWrapper | None = None
        self._current_size: int = 0

        self._log_dir.mkdir(parents=True, exist_ok=True)

    # -- Public API -----------------------------------------------------------

    def write(self, event: BlackBoxEvent) -> None:
        """Serialize and write an event to the current log file.

        Handles rotation transparently: if the current file would exceed
        the size limit after this write, a new file is opened first.

        Args:
            event: The event to persist.
        """
        line = event.to_jsonl() + "\n"
        line_bytes = len(line.encode("utf-8"))

        # Rotate if needed before writing.
        if self._current_file is not None and (
            self._current_size + line_bytes > self._max_bytes
        ):
            self._rotate()

        # Open a file if none is active.
        if self._current_file is None:
            self._open_new_file()

        assert self._current_file is not None  # for type checker
        self._current_file.write(line)
        self._current_file.flush()
        self._current_size += line_bytes

    def close(self) -> None:
        """Flush and close the current log file, if any."""
        if self._current_file is not None:
            try:
                self._current_file.flush()
                self._current_file.close()
            except Exception:
                logger.exception("Error closing log file")
            finally:
                self._current_file = None
                self._current_size = 0

    # -- Rotation internals ---------------------------------------------------

    def _open_new_file(self) -> None:
        """Open a fresh log file and reset the byte counter."""
        path = self._log_dir / _log_filename()
        self._current_file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._current_size = path.stat().st_size
        logger.info("Opened new log file: %s", path)

    def _rotate(self) -> None:
        """Close the current file and open a new one.

        After opening the new file, old files beyond the retention
        limit are removed.
        """
        logger.info("Rotating log file (current size: %d bytes)", self._current_size)
        self.close()
        self._open_new_file()
        self._cleanup_old_files()

    def _cleanup_old_files(self) -> None:
        """Delete the oldest log files when the count exceeds the limit.

        Files are sorted by name (which embeds a UTC timestamp) so the
        lexicographic order matches chronological order.
        """
        log_files = sorted(self._log_dir.glob("blackboxrs_*.jsonl"))
        excess = len(log_files) - self._max_files
        if excess <= 0:
            return

        for stale in log_files[:excess]:
            try:
                stale.unlink()
                logger.info("Deleted old log file: %s", stale)
            except OSError:
                logger.warning("Failed to delete old log file: %s", stale)
