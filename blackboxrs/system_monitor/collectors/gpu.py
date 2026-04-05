"""GPU metrics collector for BlackBoxRS.

Supports two backends:

* **nvidia-smi** — desktop/server NVIDIA GPUs.
* **tegrastats** — NVIDIA Jetson (integrated GPU with sysfs nodes).

The ``"auto"`` mode probes for a Jetson sysfs path first, then falls back
to ``nvidia-smi``, and finally to ``None`` (no GPU).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Jetson sysfs paths (may vary across L4T versions)
_JETSON_GPU_LOAD = Path("/sys/devices/gpu.0/load")
_JETSON_THERMAL_BASE = Path("/sys/devices/virtual/thermal")


class GpuCollector:
    """Collects GPU metrics. Auto-detects Jetson vs desktop NVIDIA."""

    def __init__(self, backend: str = "auto") -> None:
        """Initialise the collector.

        Args:
            backend: One of ``"auto"``, ``"nvidia-smi"``, ``"tegrastats"``,
                     or ``"none"``.
        """
        self._backend: str | None = self._resolve_backend(backend)
        if self._backend:
            logger.info("GpuCollector using backend: %s", self._backend)
        else:
            logger.info("GpuCollector: no GPU backend available")

    # ------------------------------------------------------------------
    # Backend resolution
    # ------------------------------------------------------------------

    def _resolve_backend(self, requested: str) -> str | None:
        """Determine which GPU backend to use.

        Args:
            requested: The backend string from configuration.

        Returns:
            ``"nvidia-smi"``, ``"tegrastats"``, or ``None`` if no GPU is
            detected.
        """
        if requested == "none":
            return None

        if requested == "tegrastats":
            return "tegrastats" if self._tegrastats_available() else None

        if requested == "nvidia-smi":
            return "nvidia-smi" if self._nvidia_smi_available() else None

        # auto — prefer tegrastats (Jetson) then nvidia-smi
        if self._tegrastats_available():
            return "tegrastats"
        if self._nvidia_smi_available():
            return "nvidia-smi"
        return None

    @staticmethod
    def _tegrastats_available() -> bool:
        """Check whether Jetson GPU sysfs nodes are present."""
        return _JETSON_GPU_LOAD.exists()

    @staticmethod
    def _nvidia_smi_available() -> bool:
        """Check whether ``nvidia-smi`` is on ``PATH``."""
        return shutil.which("nvidia-smi") is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return ``True`` if a usable GPU backend was detected."""
        return self._backend is not None

    def collect(self) -> dict | None:
        """Return current GPU metrics, or ``None`` if no GPU is available.

        Returns:
            A dictionary with keys ``gpu_util_percent``, ``gpu_temp_c``,
            ``gpu_memory_used_mb``, ``gpu_memory_total_mb``, and ``power_w``.
            Any value that cannot be read is set to ``None``.
            Returns ``None`` entirely when no backend is available.
        """
        if self._backend is None:
            return None

        try:
            if self._backend == "nvidia-smi":
                return self._collect_nvidia_smi()
            return self._collect_tegrastats()
        except Exception:
            logger.exception("Failed to collect GPU metrics via %s", self._backend)
            return None

    # ------------------------------------------------------------------
    # nvidia-smi backend
    # ------------------------------------------------------------------

    def _collect_nvidia_smi(self) -> dict:
        """Query ``nvidia-smi`` for GPU 0 metrics.

        Returns:
            Dictionary of GPU metrics parsed from CSV output.
        """
        cmd = [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,temperature.gpu,"
            "memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            logger.error("nvidia-smi failed: %s", result.stderr.strip())
            return self._empty_gpu_dict()

        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 5:
            logger.error("nvidia-smi returned unexpected output: %s", result.stdout)
            return self._empty_gpu_dict()

        return {
            "gpu_util_percent": self._safe_float(parts[0]),
            "gpu_temp_c": self._safe_float(parts[1]),
            "gpu_memory_used_mb": self._safe_float(parts[2]),
            "gpu_memory_total_mb": self._safe_float(parts[3]),
            "power_w": self._safe_float(parts[4]),
        }

    # ------------------------------------------------------------------
    # tegrastats (Jetson sysfs) backend
    # ------------------------------------------------------------------

    def _collect_tegrastats(self) -> dict:
        """Read GPU metrics from Jetson sysfs nodes.

        Returns:
            Dictionary of GPU metrics.  Memory and power fields are ``None``
            as Jetson exposes those through different mechanisms.
        """
        gpu_util = self._read_jetson_gpu_load()
        gpu_temp = self._read_jetson_gpu_temp()

        return {
            "gpu_util_percent": gpu_util,
            "gpu_temp_c": gpu_temp,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "power_w": None,
        }

    @staticmethod
    def _read_jetson_gpu_load() -> float | None:
        """Read GPU utilization from ``/sys/devices/gpu.0/load``.

        The Jetson kernel reports load as a value in the range 0–1000,
        which is scaled to 0–100.
        """
        try:
            raw = _JETSON_GPU_LOAD.read_text().strip()
            return round(float(raw) / 10.0, 1)
        except Exception:
            logger.debug("Could not read Jetson GPU load", exc_info=True)
            return None

    @staticmethod
    def _read_jetson_gpu_temp() -> float | None:
        """Read GPU temperature from Jetson thermal zones.

        Searches ``/sys/devices/virtual/thermal/thermal_zone*`` for a zone
        whose type contains ``"GPU"`` (case-insensitive) and returns its
        temperature in degrees Celsius.
        """
        try:
            for zone_dir in sorted(_JETSON_THERMAL_BASE.glob("thermal_zone*")):
                type_path = zone_dir / "type"
                temp_path = zone_dir / "temp"
                if not type_path.exists() or not temp_path.exists():
                    continue
                zone_type = type_path.read_text().strip()
                if "gpu" in zone_type.lower():
                    raw = temp_path.read_text().strip()
                    return round(float(raw) / 1000.0, 1)
        except Exception:
            logger.debug("Could not read Jetson GPU temp", exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(value: str) -> float | None:
        """Convert a string to float, returning ``None`` on failure."""
        try:
            return round(float(value), 1)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _empty_gpu_dict() -> dict:
        """Return a GPU metrics dict with all values set to ``None``."""
        return {
            "gpu_util_percent": None,
            "gpu_temp_c": None,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "power_w": None,
        }
