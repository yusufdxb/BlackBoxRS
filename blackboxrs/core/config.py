"""YAML-based configuration for BlackBoxRS.

All tunables are expressed as plain dataclasses with sensible defaults.
Configuration is loaded from ``~/.blackboxrs/config.yaml`` (or a
user-specified path) and merged on top of the defaults so that any
missing keys simply fall back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


@dataclass
class RosMonitorConfig:
    """Settings for the ROS 2 topic/node monitor."""

    enabled: bool = True
    poll_interval_sec: float = 1.0
    track_latency: bool = True
    topic_filters: list[str] = field(default_factory=list)


@dataclass
class SystemMonitorConfig:
    """Settings for the system resource monitor."""

    enabled: bool = True
    interval_sec: float = 1.0
    gpu_backend: str = "auto"  # auto | tegrastats | nvidia-smi | none


@dataclass
class AnomalyThresholds:
    """Static threshold values that trigger anomaly events."""

    cpu_percent: float = 90.0
    memory_percent: float = 85.0
    gpu_temp_c: float = 80.0


@dataclass
class FrequencyConfig:
    """Configuration for the topic frequency anomaly detector."""

    tolerance_percent: float = 20.0


@dataclass
class DeadTopicConfig:
    """Configuration for the dead-topic detector."""

    timeout_sec: float = 5.0


@dataclass
class AnomalyEngineConfig:
    """Settings for the anomaly detection engine."""

    enabled: bool = True
    thresholds: AnomalyThresholds = field(default_factory=AnomalyThresholds)
    frequency: FrequencyConfig = field(default_factory=FrequencyConfig)
    dead_topic: DeadTopicConfig = field(default_factory=DeadTopicConfig)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path("~/.blackboxrs/config.yaml")


@dataclass
class BlackBoxConfig:
    """Root configuration for the entire BlackBoxRS system.

    Provides :meth:`load` to read from YAML, :meth:`default` to get
    all-defaults, and :meth:`save` to persist the current state.

    The ``event_bus_queue_maxsize`` field controls the bounded capacity
    applied to every subscriber queue on the in-process event bus.  A
    larger value trades memory for tolerance of bursty producers; a
    smaller value bounds worst-case memory use at the cost of dropping
    events sooner when consumers fall behind.
    """

    log_dir: str = "~/.blackboxrs/logs"
    log_rotation_mb: int = 50
    log_max_files: int = 20
    event_bus_queue_maxsize: int = 1024
    ros_monitor: RosMonitorConfig = field(default_factory=RosMonitorConfig)
    system_monitor: SystemMonitorConfig = field(default_factory=SystemMonitorConfig)
    anomaly_engine: AnomalyEngineConfig = field(default_factory=AnomalyEngineConfig)

    # -- Factory methods ----------------------------------------------------

    @classmethod
    def default(cls) -> BlackBoxConfig:
        """Return a configuration populated entirely with default values.

        Returns:
            A new :class:`BlackBoxConfig` with every field at its default.
        """
        return cls()

    @classmethod
    def load(cls, path: Path | None = None) -> BlackBoxConfig:
        """Load configuration from a YAML file.

        Missing keys in the file are filled with defaults.  If the file
        does not exist the pure-default config is returned.

        Args:
            path: Path to the YAML configuration file.  Defaults to
                ``~/.blackboxrs/config.yaml``.

        Returns:
            A populated :class:`BlackBoxConfig`.
        """
        path = Path(os.path.expanduser(path or _DEFAULT_CONFIG_PATH))
        if not path.is_file():
            return cls.default()

        with open(path, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        return _dict_to_config(raw)

    # -- Persistence --------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the current configuration to a YAML file.

        Parent directories are created automatically if they don't exist.

        Args:
            path: Destination file path.
        """
        path = Path(os.path.expanduser(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(
                asdict(self),
                fh,
                default_flow_style=False,
                sort_keys=False,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NESTED_MAP: dict[str, type] = {
    "ros_monitor": RosMonitorConfig,
    "system_monitor": SystemMonitorConfig,
    "anomaly_engine": AnomalyEngineConfig,
}

_ANOMALY_NESTED_MAP: dict[str, type] = {
    "thresholds": AnomalyThresholds,
    "frequency": FrequencyConfig,
    "dead_topic": DeadTopicConfig,
}


def _merge_dataclass(dc_cls: type, data: dict[str, Any]) -> Any:
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    valid_keys = {f.name for f in fields(dc_cls)}
    return dc_cls(**{k: v for k, v in data.items() if k in valid_keys})


def _dict_to_config(raw: dict[str, Any]) -> BlackBoxConfig:
    """Convert a raw YAML dict into a fully typed :class:`BlackBoxConfig`."""
    kwargs: dict[str, Any] = {}

    for key, value in raw.items():
        if key in _NESTED_MAP and isinstance(value, dict):
            nested_cls = _NESTED_MAP[key]
            if key == "anomaly_engine":
                # Handle double-nested dataclasses inside AnomalyEngineConfig
                inner: dict[str, Any] = {}
                for k, v in value.items():
                    if k in _ANOMALY_NESTED_MAP and isinstance(v, dict):
                        inner[k] = _merge_dataclass(_ANOMALY_NESTED_MAP[k], v)
                    else:
                        inner[k] = v
                valid_keys = {f.name for f in fields(nested_cls)}
                kwargs[key] = nested_cls(
                    **{k: v for k, v in inner.items() if k in valid_keys}
                )
            else:
                kwargs[key] = _merge_dataclass(nested_cls, value)
        elif key in {f.name for f in fields(BlackBoxConfig)}:
            kwargs[key] = value

    return BlackBoxConfig(**kwargs)
