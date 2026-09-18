"""Host resource sampler for the flight recorder.

CPU and RAM come from psutil. GPU uses the existing ``GpuCollector`` for
desktop NVIDIA (nvidia-smi) and probes Jetson sysfs paths directly, because
the load node moved between L4T releases. Anything that cannot be read is
reported as unavailable with the reason, never as zero.
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Any

import psutil

# Candidate Jetson GPU load nodes (value 0..1000). Order: older L4T first,
# then the Orin devfreq layouts. Unverified on the GO2 payload until a
# capture there reports which one answered (recorded as gpu.backend).
_JETSON_LOAD_CANDIDATES = (
    "/sys/devices/gpu.0/load",
    "/sys/devices/platform/gpu.0/load",
    "/sys/devices/platform/17000000.ga10b/load",
    "/sys/devices/platform/bus@0/17000000.gpu/load",
    "/sys/devices/platform/17000000.gpu/load",
)
_DEVFREQ_GLOB = "/sys/class/devfreq/*gpu*/device/load"
_THERMAL_GLOB = "/sys/class/thermal/thermal_zone*"


def _jetson_load_path() -> str | None:
    for p in _JETSON_LOAD_CANDIDATES:
        if os.path.exists(p):
            return p
    hits = sorted(glob.glob(_DEVFREQ_GLOB))
    return hits[0] if hits else None


class SystemSampler:
    """Collects one ``sys`` record payload per call to :meth:`sample`."""

    def __init__(self, evidence_dir: Path, gpu: str = "auto") -> None:
        self._proc = psutil.Process()
        self._proc.cpu_percent(None)
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self._evidence_dir = evidence_dir
        self._jetson = _jetson_load_path() if gpu in ("auto", "jetson") else None
        self._nvsmi = None
        self._gpu_reason: str | None = None
        if self._jetson is None and gpu in ("auto", "nvidia-smi"):
            if shutil.which("nvidia-smi"):
                from blackboxrs.system_monitor.collectors.gpu import GpuCollector
                self._nvsmi = GpuCollector("nvidia-smi")
        if self._jetson is None and self._nvsmi is None:
            self._gpu_reason = ("gpu disabled by config" if gpu == "none" else
                                "no Jetson GPU load node and no nvidia-smi")
        self._zones = []
        for z in sorted(glob.glob(_THERMAL_GLOB)):
            t = Path(z) / "type"
            if (Path(z) / "temp").exists():
                try:
                    self._zones.append((t.read_text().strip() + ":" + Path(z).name, Path(z) / "temp"))
                except OSError:
                    continue

    @property
    def gpu_backend(self) -> str | None:
        if self._jetson:
            return f"jetson_sysfs:{self._jetson}"
        return "nvidia-smi" if self._nvsmi else None

    def _gpu(self) -> dict[str, Any] | None:
        if self._jetson:
            try:
                load = float(Path(self._jetson).read_text().strip()) / 10.0
            except (OSError, ValueError) as exc:
                self._gpu_reason = f"read failed: {exc}"
                return None
            temp = None
            for name, path in self._zones:
                if "gpu" in name.lower():
                    try:
                        temp = int(path.read_text().strip()) / 1000.0
                    except (OSError, ValueError):
                        pass
                    break
            return {"backend": self.gpu_backend, "load_percent": round(load, 1), "temp_c": temp}
        if self._nvsmi:
            d = self._nvsmi.collect()
            if not d or d.get("gpu_util_percent") is None:
                self._gpu_reason = "nvidia-smi returned no data"
                return None
            return {"backend": "nvidia-smi", "load_percent": d["gpu_util_percent"],
                    "temp_c": d["gpu_temp_c"], "mem_used_mb": d["gpu_memory_used_mb"]}
        return None

    def sample(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        with self._proc.oneshot():
            rss = self._proc.memory_info().rss / (1024 * 1024)
            pcpu = self._proc.cpu_percent(None)
            threads = self._proc.num_threads()
        thermal = {}
        for name, path in self._zones:
            try:
                thermal[name] = round(int(path.read_text().strip()) / 1000.0, 1)
            except (OSError, ValueError):
                continue
        try:
            free = shutil.disk_usage(self._evidence_dir).free / (1024 * 1024)
        except OSError:
            free = None
        gpu = self._gpu()
        out: dict[str, Any] = {
            "cpu_percent": psutil.cpu_percent(None),
            "per_cpu_percent": psutil.cpu_percent(None, percpu=True),
            "load_avg": list(os.getloadavg()),
            "mem_percent": vm.percent,
            "mem_used_mb": round((vm.total - vm.available) / (1024 * 1024), 1),
            "mem_total_mb": round(vm.total / (1024 * 1024), 1),
            "swap_percent": psutil.swap_memory().percent,
            "recorder": {"cpu_percent": pcpu, "rss_mb": round(rss, 1), "threads": threads},
            "thermal_c": thermal,
            "disk_free_mb": None if free is None else round(free, 1),
            "gpu": gpu,
        }
        if gpu is None:
            out["gpu_unavailable_reason"] = self._gpu_reason
        return out
