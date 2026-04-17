"""Per-topic message frequency tracking for BlackBoxRS.

Uses a sliding time-window to estimate publication rates without
requiring unbounded memory.
"""

from __future__ import annotations

import time
from collections import deque


class FrequencyTracker:
    """Tracks per-topic message frequency using a sliding window.

    Timestamps are recorded in a bounded deque and pruned on every
    read so that only the most recent *window_sec* seconds of data
    are retained.

    Args:
        window_sec: Width of the sliding window in seconds.  Older
            timestamps are discarded automatically.
    """

    def __init__(self, window_sec: float = 5.0) -> None:
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._window_sec = window_sec
        self._windows: dict[str, deque[float]] = {}

    # -- recording ----------------------------------------------------------

    def record(self, topic: str) -> None:
        """Record a message arrival for *topic*.

        Args:
            topic: Fully-qualified ROS 2 topic name.
        """
        now = time.monotonic()
        if topic not in self._windows:
            self._windows[topic] = deque()
        self._windows[topic].append(now)

    # -- queries ------------------------------------------------------------

    def get_frequency(self, topic: str) -> float | None:
        """Return the estimated publication rate for *topic* in Hz.

        Returns ``None`` when fewer than two messages have been recorded
        within the current window (a meaningful rate cannot be computed).

        Args:
            topic: Fully-qualified ROS 2 topic name.

        Returns:
            Estimated frequency in Hz, or ``None``.
        """
        window = self._windows.get(topic)
        if window is None:
            return None

        self._prune(window)

        if len(window) < 2:
            return None

        span = window[-1] - window[0]
        if span <= 0:
            return None

        return (len(window) - 1) / span

    def get_all_frequencies(self) -> dict[str, float]:
        """Return ``{topic: hz}`` for every tracked topic with valid data.

        Topics with insufficient data are omitted from the result.

        Returns:
            Dictionary mapping topic names to estimated Hz values.
        """
        result: dict[str, float] = {}
        for topic in list(self._windows):
            freq = self.get_frequency(topic)
            if freq is not None:
                result[topic] = freq
        return result

    def get_latency_estimate(self, topic: str) -> float | None:
        """Return the estimated inter-message interval for *topic* in **ms**.

        This is simply the reciprocal of the frequency, converted to
        milliseconds.  Returns ``None`` when insufficient data is
        available.

        Args:
            topic: Fully-qualified ROS 2 topic name.

        Returns:
            Estimated interval in milliseconds, or ``None``.
        """
        freq = self.get_frequency(topic)
        if freq is None or freq <= 0:
            return None
        return (1.0 / freq) * 1000.0

    def forget(self, topic: str) -> None:
        """Drop all tracking state for *topic*.

        Called when a topic disappears from the ROS graph so stale
        frequency windows do not keep emitting rate events for a
        publisher that no longer exists.  Silently ignores unknown
        topics so callers don't have to bookkeep twice.
        """
        self._windows.pop(topic, None)

    # -- internals ----------------------------------------------------------

    def _prune(self, window: deque[float]) -> None:
        """Remove entries older than the sliding window."""
        cutoff = time.monotonic() - self._window_sec
        while window and window[0] < cutoff:
            window.popleft()
