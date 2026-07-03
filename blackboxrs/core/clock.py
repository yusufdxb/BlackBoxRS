"""Timestamp utilities for BlackBoxRS.

Provides a unified clock interface for consistent UTC timestamps
and monotonic timing across all components.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    """Centralized clock for consistent timestamps across BlackBoxRS.

    By default :meth:`now` returns wall-clock UTC.  For offline replay
    (see :mod:`blackboxrs.recording.bag_replay`) a *virtual* time source
    can be installed so that time-based detectors (e.g. dead-topic
    timeouts) compute elapsed intervals against recorded bag timestamps
    instead of wall time.  The virtual override is process-global and
    intended for single-threaded replay; production monitoring never
    touches it and keeps wall-clock behaviour byte-for-byte.
    """

    _virtual_now: datetime | None = None

    @staticmethod
    def now() -> datetime:
        """Return the current UTC datetime.

        Returns:
            The installed virtual time if one is set (see
            :meth:`set_virtual_time`), otherwise wall-clock UTC.
        """
        if Clock._virtual_now is not None:
            return Clock._virtual_now
        return datetime.now(timezone.utc)

    @staticmethod
    def set_virtual_time(dt: datetime) -> None:
        """Pin :meth:`now` to a fixed virtual instant (UTC).

        Args:
            dt: The virtual time to return from :meth:`now`.  A naive
                datetime is assumed to be UTC.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        Clock._virtual_now = dt

    @staticmethod
    def use_wall_clock() -> None:
        """Remove any virtual override and restore wall-clock time."""
        Clock._virtual_now = None

    @staticmethod
    def is_virtual() -> bool:
        """Return ``True`` while a virtual time override is installed."""
        return Clock._virtual_now is not None

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
