"""Preflight check: local host resource metric is within bounds.

Uses :mod:`psutil` to sample a resource metric and compare it against
caller-supplied thresholds. Pure-Python; no ROS 2 dependency.

Parameters
----------
metric : str
    One of:

    * ``cpu_percent``    -- CPU utilisation across all cores (0-100 %).
    * ``memory_percent`` -- Virtual memory usage (0-100 %).
    * ``disk_percent``   -- Disk partition usage (0-100 %).
    * ``disk_free_gb``   -- Free space on a partition in GiB.

max : numeric, optional
    Fail when ``sampled_value > max``.
min : numeric, optional
    Fail when ``sampled_value < min``.
sample_seconds : float, default 1.0
    Passed to ``psutil.cpu_percent(interval=sample_seconds)`` for the
    ``cpu_percent`` metric. Ignored for instantaneous metrics.
path : str, default "/"
    Mount point / path for disk metrics.

Result
------
``("pass" | "fail" | "error", message)``
"""

from __future__ import annotations

from typing import Any

_VALID_METRICS = frozenset(
    {"cpu_percent", "memory_percent", "disk_percent", "disk_free_gb"}
)


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a resource_threshold preflight check."""
    try:
        import psutil  # type: ignore[import]
    except ImportError:
        return (
            "error",
            "psutil is not installed; resource_threshold checks unavailable.",
        )

    metric: Any = params.get("metric")
    if not isinstance(metric, str) or metric not in _VALID_METRICS:
        return (
            "error",
            f"resource_threshold requires 'metric' to be one of "
            f"{sorted(_VALID_METRICS)}; got {metric!r}.",
        )

    threshold_max: Any = params.get("max")
    threshold_min: Any = params.get("min")
    sample_seconds: float = float(params.get("sample_seconds", 1.0))
    path: str = str(params.get("path", "/"))

    # --- sample ---
    try:
        value = _sample(psutil, metric, sample_seconds=sample_seconds, path=path)
    except Exception as exc:
        return ("error", f"Failed to sample {metric!r}: {type(exc).__name__}: {exc}")

    # --- evaluate ---
    unit = "GiB" if metric == "disk_free_gb" else "%"
    display = f"{metric}={value:.2f}{unit} (path={path!r})" if "disk" in metric \
        else f"{metric}={value:.2f}{unit}"

    if threshold_max is not None and value > float(threshold_max):
        return (
            "fail",
            f"{display} exceeds max threshold of {threshold_max}{unit}.",
        )
    if threshold_min is not None and value < float(threshold_min):
        return (
            "fail",
            f"{display} is below min threshold of {threshold_min}{unit}.",
        )

    bounds: list[str] = []
    if threshold_max is not None:
        bounds.append(f"max={threshold_max}{unit}")
    if threshold_min is not None:
        bounds.append(f"min={threshold_min}{unit}")
    bounds_str = f" (thresholds: {', '.join(bounds)})" if bounds else ""
    return ("pass", f"{display} is within bounds{bounds_str}.")


def _sample(psutil: Any, metric: str, *, sample_seconds: float, path: str) -> float:
    if metric == "cpu_percent":
        return float(psutil.cpu_percent(interval=sample_seconds))
    if metric == "memory_percent":
        return float(psutil.virtual_memory().percent)
    if metric == "disk_percent":
        return float(psutil.disk_usage(path).percent)
    if metric == "disk_free_gb":
        return float(psutil.disk_usage(path).free) / (1024 ** 3)
    raise ValueError(f"Unknown metric: {metric!r}")  # unreachable; guarded above
