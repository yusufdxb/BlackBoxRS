"""Disk usage and I/O metrics collector for BlackBoxRS.

Uses psutil to read root-partition usage and system-wide disk I/O counters.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False
    logger.warning("psutil not installed: DiskCollector will return empty results")

_GB = 1024 ** 3
_MB = 1024 ** 2


class DiskCollector:
    """Collects disk usage and I/O metrics."""

    def __init__(self) -> None:
        """Initialise the collector with baseline I/O counters for rate calculation."""
        self._prev_read_bytes: int | None = None
        self._prev_write_bytes: int | None = None
        self._prev_time: float | None = None

    def collect(self) -> dict:
        """Return current disk usage and I/O rate metrics.

        Usage is measured on the root (``/``) partition.  I/O rates are
        computed as a delta since the previous call; the first call returns
        ``0.0`` for both rates.

        Returns:
            A dictionary with the following keys:

            - ``disk_percent``: root partition utilization (0–100).
            - ``disk_used_gb``: space used on root partition (GiB).
            - ``disk_total_gb``: total size of root partition (GiB).
            - ``disk_read_mb_s``: read throughput in MiB/s since last call.
            - ``disk_write_mb_s``: write throughput in MiB/s since last call.

            Returns an empty dict if *psutil* is not available.
        """
        if not _HAS_PSUTIL:
            return {}

        try:
            usage = psutil.disk_usage("/")
            read_rate, write_rate = self._io_rates()

            return {
                "disk_percent": round(usage.percent, 1),
                "disk_used_gb": round(usage.used / _GB, 2),
                "disk_total_gb": round(usage.total / _GB, 2),
                "disk_read_mb_s": round(read_rate, 2),
                "disk_write_mb_s": round(write_rate, 2),
            }
        except Exception:
            logger.exception("Failed to collect disk metrics")
            return {}

    def _io_rates(self) -> tuple[float, float]:
        """Compute disk I/O rates since the previous sample.

        Returns:
            A ``(read_mb_s, write_mb_s)`` tuple.  Returns ``(0.0, 0.0)``
            on the first invocation or if counters are unavailable.
        """
        try:
            counters = psutil.disk_io_counters()
        except Exception:
            return 0.0, 0.0

        if counters is None:
            return 0.0, 0.0

        now = time.monotonic()
        read_bytes = counters.read_bytes
        write_bytes = counters.write_bytes

        if self._prev_time is None:
            self._prev_read_bytes = read_bytes
            self._prev_write_bytes = write_bytes
            self._prev_time = now
            return 0.0, 0.0

        elapsed = now - self._prev_time
        if elapsed <= 0:
            return 0.0, 0.0

        read_rate = (read_bytes - (self._prev_read_bytes or 0)) / _MB / elapsed
        write_rate = (write_bytes - (self._prev_write_bytes or 0)) / _MB / elapsed

        self._prev_read_bytes = read_bytes
        self._prev_write_bytes = write_bytes
        self._prev_time = now

        return max(read_rate, 0.0), max(write_rate, 0.0)
