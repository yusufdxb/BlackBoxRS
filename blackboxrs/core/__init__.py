"""Core module for BlackBoxRS.

Re-exports the primary classes used across the platform so that
downstream code can do::

    from blackboxrs.core import BlackBoxEvent, EventBus, BlackBoxConfig, Clock, Session
"""

from blackboxrs.core.clock import Clock
from blackboxrs.core.config import (
    AnomalyEngineConfig,
    AnomalyThresholds,
    BlackBoxConfig,
    DeadTopicConfig,
    FrequencyConfig,
    RosMonitorConfig,
    SystemMonitorConfig,
)
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import (
    AnomalyData,
    BlackBoxEvent,
    QoSProfileData,
    SystemMetricData,
    TopicFrequencyData,
)
from blackboxrs.core.session import Session

__all__ = [
    "AnomalyData",
    "AnomalyEngineConfig",
    "AnomalyThresholds",
    "BlackBoxConfig",
    "BlackBoxEvent",
    "Clock",
    "DeadTopicConfig",
    "EventBus",
    "FrequencyConfig",
    "QoSProfileData",
    "RosMonitorConfig",
    "Session",
    "SystemMetricData",
    "SystemMonitorConfig",
    "TopicFrequencyData",
]
