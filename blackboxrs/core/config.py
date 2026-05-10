"""YAML-based configuration for BlackBoxRS.

All tunables are expressed as plain dataclasses with sensible defaults.
Configuration is loaded from ``~/.blackboxrs/config.yaml`` (or a
user-specified path) and merged on top of the defaults so that any
missing keys simply fall back.

Unknown keys in the YAML file are not silently ignored.  By default
they produce a ``logging.WARNING`` so operators see typos instead of
the config quietly doing nothing.  Strict mode promotes unknown keys
to a :class:`ConfigError` instead, which is the recommended setting
for CI / deployment validation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised in strict mode when a config file contains unknown keys."""


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
class TfTopologyConfig:
    """Configuration for the TF topology detector.

    Mirrors :class:`blackboxrs.anomaly_engine.detectors.tf_topology.TfTopologyConfig`
    so that operators can tune the staleness threshold from YAML without
    having to import the detector module.
    """

    stale_timeout_sec: float = 5.0


@dataclass
class ProcessSignalsConfig:
    """Configuration for the per-process CPU/RSS signals detector."""

    cpu_percent: float = 90.0
    rss_mb: float = 1024.0


@dataclass
class ClockSkewConfig:
    """Configuration for the NTP/clock-skew detector."""

    max_skew_sec: float = 0.1


@dataclass
class AnomalyEngineConfig:
    """Settings for the anomaly detection engine."""

    enabled: bool = True
    thresholds: AnomalyThresholds = field(default_factory=AnomalyThresholds)
    frequency: FrequencyConfig = field(default_factory=FrequencyConfig)
    dead_topic: DeadTopicConfig = field(default_factory=DeadTopicConfig)
    tf_topology: TfTopologyConfig = field(default_factory=TfTopologyConfig)
    process_signals: ProcessSignalsConfig = field(
        default_factory=ProcessSignalsConfig
    )
    clock_skew: ClockSkewConfig = field(default_factory=ClockSkewConfig)
    custom_detectors: list[dict] = field(default_factory=list)


@dataclass
class PrometheusConfig:
    """Settings for optional Prometheus metrics export."""

    enabled: bool = False
    port: int = 9100
    host: str = "0.0.0.0"


@dataclass
class Rosbag2RecorderConfig:
    """Settings for anomaly-triggered rosbag2 recording."""

    enabled: bool = False
    output_dir: str = "~/.blackboxrs/bags"
    record_duration_sec: float = 30.0
    cooldown_sec: float = 60.0
    executable: str = "ros2"
    storage_id: str = "sqlite3"
    max_recordings_per_run: int = 10
    trigger_event_types: list[str] = field(
        default_factory=lambda: [
            "anomaly.threshold",
            "anomaly.frequency",
            "anomaly.dead_topic",
            "anomaly.qos_mismatch",
            "anomaly.tf_topology",
            "anomaly.process_signals",
            "anomaly.clock_skew",
        ]
    )
    topics: list[str] = field(default_factory=list)


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
    log_max_age_hours: float = 0
    event_bus_queue_maxsize: int = 1024
    ros_monitor: RosMonitorConfig = field(default_factory=RosMonitorConfig)
    system_monitor: SystemMonitorConfig = field(default_factory=SystemMonitorConfig)
    anomaly_engine: AnomalyEngineConfig = field(default_factory=AnomalyEngineConfig)
    rosbag2: Rosbag2RecorderConfig = field(default_factory=Rosbag2RecorderConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)

    # -- Factory methods ----------------------------------------------------

    @classmethod
    def default(cls) -> BlackBoxConfig:
        """Return a configuration populated entirely with default values.

        Returns:
            A new :class:`BlackBoxConfig` with every field at its default.
        """
        return cls()

    @classmethod
    def load(cls, path: Path | None = None, *, strict: bool = False) -> BlackBoxConfig:
        """Load configuration from a YAML file.

        Missing keys in the file are filled with defaults.  If the file
        does not exist the pure-default config is returned.

        Args:
            path: Path to the YAML configuration file.  Defaults to
                ``~/.blackboxrs/config.yaml``.
            strict: If ``True``, raise :class:`ConfigError` when the YAML
                contains keys that are not part of the schema.  In
                non-strict mode (default) unknown keys are logged as
                warnings and otherwise ignored.

        Returns:
            A populated :class:`BlackBoxConfig`.

        Raises:
            ConfigError: Only when ``strict=True`` and unknown keys are
                present in the YAML file.
        """
        path = Path(os.path.expanduser(path or _DEFAULT_CONFIG_PATH))
        if not path.is_file():
            return cls.default()

        with open(path, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        return _dict_to_config(raw, strict=strict, source=str(path))

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
    "rosbag2": Rosbag2RecorderConfig,
    "prometheus": PrometheusConfig,
}

_ANOMALY_NESTED_MAP: dict[str, type] = {
    "thresholds": AnomalyThresholds,
    "frequency": FrequencyConfig,
    "dead_topic": DeadTopicConfig,
    "tf_topology": TfTopologyConfig,
    "process_signals": ProcessSignalsConfig,
    "clock_skew": ClockSkewConfig,
}


def _report_unknown_keys(
    unknown: list[str], *, context: str, strict: bool, source: str | None
) -> None:
    """Warn or raise when unknown keys are encountered in config input.

    The caller provides a human-readable ``context`` describing where
    in the YAML tree the unknown keys were found (e.g. ``"top-level"``,
    ``"ros_monitor"``, ``"anomaly_engine.thresholds"``).  In non-strict
    mode we log a WARNING per context; in strict mode we raise a
    :class:`ConfigError` so CI / deployment validation surfaces typos.
    """
    if not unknown:
        return

    loc = f" in {source}" if source else ""
    joined = ", ".join(sorted(unknown))
    msg = (
        f"Unknown config key(s) under {context}{loc}: {joined}. "
        "Valid keys come from the BlackBoxConfig dataclass schema; "
        "unknown keys have no effect."
    )
    if strict:
        raise ConfigError(msg)
    logger.warning(msg)


def _merge_dataclass(
    dc_cls: type,
    data: dict[str, Any],
    *,
    context: str,
    strict: bool,
    source: str | None,
) -> Any:
    """Instantiate a dataclass from a dict; warn/raise on unknown keys."""
    valid_keys = {f.name for f in fields(dc_cls)}
    unknown = [k for k in data if k not in valid_keys]
    _report_unknown_keys(unknown, context=context, strict=strict, source=source)
    return dc_cls(**{k: v for k, v in data.items() if k in valid_keys})


def _dict_to_config(
    raw: dict[str, Any],
    *,
    strict: bool = False,
    source: str | None = None,
) -> BlackBoxConfig:
    """Convert a raw YAML dict into a fully typed :class:`BlackBoxConfig`.

    ``strict`` controls how unknown keys are handled:

    - ``False`` (default): log a WARNING per scope and ignore the key.
    - ``True``: raise :class:`ConfigError` on the first scope containing
      unknown keys.

    The ``source`` parameter is purely diagnostic — it's woven into the
    warning / error message so operators can trace which file caused
    the complaint.
    """
    top_valid = {f.name for f in fields(BlackBoxConfig)}
    top_unknown = [k for k in raw if k not in top_valid]
    _report_unknown_keys(top_unknown, context="top-level", strict=strict, source=source)

    kwargs: dict[str, Any] = {}

    for key, value in raw.items():
        if key not in top_valid:
            continue  # already reported above
        if key in _NESTED_MAP and isinstance(value, dict):
            nested_cls = _NESTED_MAP[key]
            if key == "anomaly_engine":
                # Handle double-nested dataclasses inside AnomalyEngineConfig.
                # Validate inner and outer keys separately so messages
                # point at the right scope.
                engine_valid = {f.name for f in fields(nested_cls)}
                engine_unknown = [k for k in value if k not in engine_valid]
                _report_unknown_keys(
                    engine_unknown,
                    context="anomaly_engine",
                    strict=strict,
                    source=source,
                )

                inner: dict[str, Any] = {}
                for k, v in value.items():
                    if k not in engine_valid:
                        continue
                    if k in _ANOMALY_NESTED_MAP and isinstance(v, dict):
                        inner[k] = _merge_dataclass(
                            _ANOMALY_NESTED_MAP[k],
                            v,
                            context=f"anomaly_engine.{k}",
                            strict=strict,
                            source=source,
                        )
                    else:
                        inner[k] = v
                kwargs[key] = nested_cls(**inner)
            else:
                kwargs[key] = _merge_dataclass(
                    nested_cls,
                    value,
                    context=key,
                    strict=strict,
                    source=source,
                )
        else:
            kwargs[key] = value

    return BlackBoxConfig(**kwargs)
