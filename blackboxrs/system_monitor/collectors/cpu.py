"""CPU metrics collector for BlackBoxRS.

Uses psutil to gather CPU utilization, core counts, per-core usage,
and system load averages.
"""

from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False
    logger.warning("psutil not installed — CpuCollector will return empty results")


class CpuCollector:
    """Collects CPU utilization metrics."""

    def collect(self) -> dict:
        """Return current CPU metrics.

        Returns:
            A dictionary with the following keys:

            - ``cpu_percent`` — overall CPU utilization as a percentage (0–100).
            - ``cpu_count`` — number of logical CPUs.
            - ``per_cpu_percent`` — list of per-core utilization percentages.
            - ``load_avg_1m`` — 1-minute load average.
            - ``load_avg_5m`` — 5-minute load average.
            - ``load_avg_15m`` — 15-minute load average.

            Returns an empty dict if *psutil* is not available.
        """
        if not _HAS_PSUTIL:
            return {}

        try:
            per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)
            overall = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
            load1, load5, load15 = os.getloadavg()

            return {
                "cpu_percent": round(overall, 1),
                "cpu_count": psutil.cpu_count(logical=True) or 0,
                "per_cpu_percent": [round(v, 1) for v in per_cpu],
                "load_avg_1m": round(load1, 2),
                "load_avg_5m": round(load5, 2),
                "load_avg_15m": round(load15, 2),
            }
        except Exception:
            logger.exception("Failed to collect CPU metrics")
            return {}
