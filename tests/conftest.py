"""Shared pytest fixtures for BlackBoxRS tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent


@pytest.fixture
def event_bus() -> EventBus:
    """Return a fresh EventBus instance."""
    return EventBus()


@pytest.fixture
def sample_ros_event() -> BlackBoxEvent:
    """A ROS frequency event matching what RosMonitor actually emits."""
    return BlackBoxEvent(
        timestamp=datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc),
        source="ros_monitor",
        event_type="ros.frequency",
        severity="info",
        data={"topic": "/cmd_vel", "frequency_hz": 10.0},
        metadata={"session_id": "test123"},
    )


@pytest.fixture
def sample_system_event() -> BlackBoxEvent:
    """A system CPU event matching what SystemMonitor actually emits."""
    return BlackBoxEvent(
        timestamp=datetime(2026, 4, 5, 12, 0, 1, tzinfo=timezone.utc),
        source="system_monitor",
        event_type="system.cpu",
        severity="info",
        data={"cpu_percent": 45.0, "cpu_count": 8, "per_cpu_percent": [40.0]},
        metadata={"session_id": "test123"},
    )


@pytest.fixture
def sample_anomaly_event() -> BlackBoxEvent:
    """An anomaly event matching what ThresholdDetector now emits."""
    return BlackBoxEvent(
        timestamp=datetime(2026, 4, 5, 12, 0, 2, tzinfo=timezone.utc),
        source="anomaly_engine",
        event_type="anomaly.threshold",
        severity="warning",
        data={
            "detector": "threshold",
            "metric": "cpu_percent",
            "value": 95.0,
            "threshold": 90.0,
            "message": "cpu_percent is 95.0%, exceeding threshold of 90.0%",
        },
        metadata={"session_id": "test123"},
    )


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for log files."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def config(tmp_log_dir: Path) -> BlackBoxConfig:
    """Return a BlackBoxConfig pointing at the temporary log directory."""
    return BlackBoxConfig(
        log_dir=str(tmp_log_dir),
        log_rotation_mb=1,
        log_max_files=5,
    )
