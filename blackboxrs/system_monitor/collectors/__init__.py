"""System-metric collectors for BlackBoxRS.

Re-exports all collector classes for convenient imports::

    from blackboxrs.system_monitor.collectors import CpuCollector, MemoryCollector
"""

from blackboxrs.system_monitor.collectors.cpu import CpuCollector
from blackboxrs.system_monitor.collectors.disk import DiskCollector
from blackboxrs.system_monitor.collectors.gpu import GpuCollector
from blackboxrs.system_monitor.collectors.memory import MemoryCollector
from blackboxrs.system_monitor.collectors.thermal import ThermalCollector

__all__ = [
    "CpuCollector",
    "DiskCollector",
    "GpuCollector",
    "MemoryCollector",
    "ThermalCollector",
]
