"""Memory and swap metrics collector for BlackBoxRS.

Uses psutil to gather physical memory and swap utilization.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False
    logger.warning("psutil not installed: MemoryCollector will return empty results")

_MB = 1024 * 1024


class MemoryCollector:
    """Collects memory and swap metrics."""

    def collect(self) -> dict:
        """Return current memory and swap metrics.

        Returns:
            A dictionary with the following keys:

            - ``memory_percent``: physical memory utilization (0–100).
            - ``memory_used_mb``: physical memory in use (MiB).
            - ``memory_total_mb``: total physical memory (MiB).
            - ``swap_percent``: swap utilization (0–100).
            - ``swap_used_mb``: swap in use (MiB).

            Returns an empty dict if *psutil* is not available.
        """
        if not _HAS_PSUTIL:
            return {}

        try:
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()

            return {
                "memory_percent": round(mem.percent, 1),
                "memory_used_mb": round(mem.used / _MB, 1),
                "memory_total_mb": round(mem.total / _MB, 1),
                "swap_percent": round(swap.percent, 1),
                "swap_used_mb": round(swap.used / _MB, 1),
            }
        except Exception:
            logger.exception("Failed to collect memory metrics")
            return {}
