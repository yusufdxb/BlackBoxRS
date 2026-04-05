"""ROS 2 monitoring subsystem for BlackBoxRS.

Re-exports :class:`RosMonitor` for convenience::

    from blackboxrs.ros_monitor import RosMonitor
"""

from blackboxrs.ros_monitor.monitor import RosMonitor

__all__ = ["RosMonitor"]
