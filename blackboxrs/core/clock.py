"""Timestamp utilities for BlackBoxRS.

Provides a unified clock interface for consistent UTC timestamps
and monotonic timing across all components.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    """Centralized clock for consistent timestamps across BlackBoxRS."""

    @staticmethod
    def now() -> datetime:
        """Return the current UTC datetime.

        Returns:
            A timezone-aware datetime in UTC.
        """
        return datetime.now(timezone.utc)

    @staticmethod
    def monotonic_ns() -> int:
        """Return a monotonic clock value in nanoseconds.

        Suitable for measuring elapsed intervals without being affected
        by system clock adjustments.

        Returns:
            Monotonic time in nanoseconds.
        """
        return time.monotonic_ns()

    @staticmethod
    def format_iso(dt: datetime) -> str:
        """Format a datetime as an ISO 8601 string with UTC offset.

        Args:
            dt: The datetime to format. If naive (no tzinfo), UTC is assumed.

        Returns:
            ISO 8601 formatted string, e.g. ``2026-04-05T12:30:00+00:00``.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
