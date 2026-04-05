"""System monitoring subsystem for BlackBoxRS.

Re-exports :class:`SystemMonitor` for convenient access::

    from blackboxrs.system_monitor import SystemMonitor
"""

from blackboxrs.system_monitor.monitor import SystemMonitor

__all__ = ["SystemMonitor"]
