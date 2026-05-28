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
class TfProducerConfig:
    """Settings for the TF snapshot producer.

    The producer subscribes to ``/tf`` and ``/tf_static``, maintains a
    last-seen map per ``(parent, child)`` edge, and emits one
    ``ros.tf`` snapshot event per tick. ``expected_frames`` is the
    operator-declared frame set the TF topology detector uses to fire
    ``orphan_frame`` anomalies; an empty list silently disables
    orphan-frame detection at the detector side.
    """

    snapshot_hz: float = 1.0
    expected_frames: list[str] = field(default_factory=list)
    gc_age_sec: float = 60.0


@dataclass
class RosMonitorConfig:
    """Settings for the ROS 2 topic/node monitor."""

    enabled: bool = True
    poll_interval_sec: float = 1.0
    track_latency: bool = True
    topic_filters: list[str] = field(default_factory=list)
    tf: TfProducerConfig = field(default_factory=TfProducerConfig)


@dataclass
class ClockProducerConfig:
    """Settings for the clock-skew producer.

    The producer samples ``time.time()`` (``system`` source), optionally
    an NTP peer via ``chronyc`` or ``ntpq``, and optionally the ROS
    ``/clock`` topic if sim-time is active.

    ``include_ros_clock="auto"`` mirrors ``runtime.use_sim_time`` — in
    practice this means the producer checks whether ``rclpy`` is
    importable and whether a ``/clock`` subscriber actually receives
    messages before adding the source to a snapshot.

    ``ntp_tool="auto"`` tries ``chronyc`` first and falls back to
    ``ntpq``; if neither binary is found the NTP source is skipped for
    that tick (a single WARN is logged).
    """

    enabled: bool = True
    sample_hz: float = 1.0
    include_ntp: bool = True
    include_ros_clock: str = "auto"  # "auto" | "true" | "false"
    ntp_tool: str = "auto"  # "auto" | "chronyc" | "ntpq"


@dataclass
class ProcessSignalsCollectorConfig:
    """Settings for the per-process CPU/RSS signals producer.

    The producer calls ``psutil.process_iter()`` on each tick and
    matches running processes against ``tracked_patterns`` (glob list
    against the full cmdline string).  The default patterns cover common
    ROS 2 and controller node invocations:

    - ``*ros2*``       — catches ``ros2 run foo bar`` and similar
    - ``*rclpy*``      — catches ``python3 -m rclpy.*`` style launches
    - ``*controller*`` — hardware-interface / ros2_control nodes
    - ``*nav2*``       — navigation stack nodes
    - ``*moveit*``     — MoveIt 2 nodes

    ``max_tracked`` caps the snapshot size so the payload stays bounded
    even on busy robot computers.

    In observer mode (``runtime.role="observer"``) the producer
    auto-disables and emits a single INFO log at startup.  psutil reports
    the observer workstation's processes, not the robot's.  A DDS bridge
    for remote process enumeration is deferred to v2.
    """

    enabled: bool = True
    sample_hz: float = 1.0
    tracked_patterns: list[str] = field(
        default_factory=lambda: [
            "*ros2*",
            "*rclpy*",
            "*controller*",
            "*nav2*",
            "*moveit*",
        ]
    )
    max_tracked: int = 64


@dataclass
class SystemMonitorConfig:
    """Settings for the system resource monitor."""

    enabled: bool = True
    interval_sec: float = 1.0
    gpu_backend: str = "auto"  # auto | tegrastats | nvidia-smi | none
    clock: ClockProducerConfig = field(default_factory=ClockProducerConfig)
    process_signals: ProcessSignalsCollectorConfig = field(
        default_factory=ProcessSignalsCollectorConfig
    )


@dataclass
class AnomalyThresholds:
    """Static threshold values that trigger anomaly events."""

    cpu_percent: float = 90.0
    memory_percent: float = 85.0
    gpu_temp_c: float = 80.0
    min_consecutive_samples: int = 2


@dataclass
class FrequencyConfig:
    """Configuration for the topic frequency anomaly detector."""

    tolerance_percent: float = 20.0
    min_consecutive_samples: int = 2


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
    min_consecutive_samples: int = 2


@dataclass
class ClockSkewConfig:
    """Configuration for the NTP/clock-skew detector."""

    max_skew_sec: float = 0.1
    min_consecutive_samples: int = 2


@dataclass
class AnomalyEngineConfig:
    """Settings for the anomaly detection engine.

    ``observer_mode`` is normally derived from the top-level
    :class:`RuntimeConfig` by :meth:`BlackBoxConfig.apply_runtime_role`
    just after loading. When True, detectors that read the local host
    (process_signals, threshold over host cpu/mem) are skipped because
    their numbers describe the observer workstation, not the robot.
    DDS-bound detectors (frequency, dead_topic, qos_mismatch,
    tf_topology, clock_skew) keep running.
    """

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
    observer_mode: bool = False


@dataclass
class RuntimeConfig:
    """Where BlackBoxRS is running relative to the robot.

    - ``role="onboard"`` (default): colocated with the ROS 2 node graph
      it is watching (e.g. on the robot's Jetson, or on a single-host
      bringup machine). All collectors and detectors are meaningful
      because the local host *is* the robot.
    - ``role="observer"``: BlackBoxRS runs on a separate workstation
      that reaches the robot's topic graph over DDS. Host-bound
      collectors (CPU, memory, disk, per-process CPU/RSS) describe the
      observer, not the robot, so they are skipped by default.
      DDS-bound detectors continue to run.

    ``observed_host`` is a free-form label for the robot/host being
    watched (e.g. ``"go2-edu-01"``). It is recorded in session
    metadata and in every incident bundle so a report can say
    "captured by *observer* watching *observed_host*" instead of
    pretending the observer's hostname is the robot's.
    """

    role: str = "onboard"
    observed_host: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("onboard", "observer"):
            raise ConfigError(
                f"runtime.role must be 'onboard' or 'observer', got {self.role!r}"
            )

    @property
    def is_observer(self) -> bool:
        return self.role == "observer"


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
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
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

    # -- Runtime-role policy ------------------------------------------------

    def apply_runtime_role(self) -> BlackBoxConfig:
        """Apply observer-mode policy implied by ``runtime.role``.

        When the operator declares ``runtime.role: observer`` they are
        saying "this process is on a workstation, not the robot." We
        translate that one declaration into the concrete behavioural
        flips required to keep the bundle honest:

        - ``system_monitor.enabled`` is forced to False (CPU / mem /
          disk / GPU readings would describe the observer host).
        - ``anomaly_engine.observer_mode`` is set True so the engine
          skips the per-process signals detector.

        Onboard mode is a no-op so existing deployments are unaffected.
        Returns ``self`` so the call can be chained after ``load()``.
        """
        if self.runtime.is_observer:
            self.system_monitor.enabled = False
            self.anomaly_engine.observer_mode = True
        return self

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

        cfg = _dict_to_config(raw, strict=strict, source=str(path))
        return cfg.apply_runtime_role()

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
    "runtime": RuntimeConfig,
    "ros_monitor": RosMonitorConfig,
    "system_monitor": SystemMonitorConfig,
    "anomaly_engine": AnomalyEngineConfig,
    "rosbag2": Rosbag2RecorderConfig,
    "prometheus": PrometheusConfig,
}

_ROS_MONITOR_NESTED_MAP: dict[str, type] = {
    "tf": TfProducerConfig,
}

_SYSTEM_MONITOR_NESTED_MAP: dict[str, type] = {
    "clock": ClockProducerConfig,
    "process_signals": ProcessSignalsCollectorConfig,
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
            elif key == "ros_monitor":
                ros_valid = {f.name for f in fields(nested_cls)}
                ros_unknown = [k for k in value if k not in ros_valid]
                _report_unknown_keys(
                    ros_unknown,
                    context="ros_monitor",
                    strict=strict,
                    source=source,
                )
                inner_ros: dict[str, Any] = {}
                for k, v in value.items():
                    if k not in ros_valid:
                        continue
                    if k in _ROS_MONITOR_NESTED_MAP and isinstance(v, dict):
                        inner_ros[k] = _merge_dataclass(
                            _ROS_MONITOR_NESTED_MAP[k],
                            v,
                            context=f"ros_monitor.{k}",
                            strict=strict,
                            source=source,
                        )
                    else:
                        inner_ros[k] = v
                kwargs[key] = nested_cls(**inner_ros)
            elif key == "system_monitor":
                sm_valid = {f.name for f in fields(nested_cls)}
                sm_unknown = [k for k in value if k not in sm_valid]
                _report_unknown_keys(
                    sm_unknown,
                    context="system_monitor",
                    strict=strict,
                    source=source,
                )
                inner_sm: dict[str, Any] = {}
                for k, v in value.items():
                    if k not in sm_valid:
                        continue
                    if k in _SYSTEM_MONITOR_NESTED_MAP and isinstance(v, dict):
                        inner_sm[k] = _merge_dataclass(
                            _SYSTEM_MONITOR_NESTED_MAP[k],
                            v,
                            context=f"system_monitor.{k}",
                            strict=strict,
                            source=source,
                        )
                    else:
                        inner_sm[k] = v
                kwargs[key] = nested_cls(**inner_sm)
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
