"""Tests for blackboxrs.core.config."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from blackboxrs.core.config import (
    AnomalyEngineConfig,
    AnomalyThresholds,
    BlackBoxConfig,
    ConfigError,
    DeadTopicConfig,
    FrequencyConfig,
    Rosbag2RecorderConfig,
    RosMonitorConfig,
    SystemMonitorConfig,
)


class TestDefaultConfig:
    """Test BlackBoxConfig.default() returns sensible defaults."""

    def test_default_creates_config(self):
        cfg = BlackBoxConfig.default()
        assert isinstance(cfg, BlackBoxConfig)

    def test_default_log_dir(self):
        cfg = BlackBoxConfig.default()
        assert cfg.log_dir == "~/.blackboxrs/logs"

    def test_default_rotation_settings(self):
        cfg = BlackBoxConfig.default()
        assert cfg.log_rotation_mb == 50
        assert cfg.log_max_files == 20

    def test_default_ros_monitor_enabled(self):
        cfg = BlackBoxConfig.default()
        assert cfg.ros_monitor.enabled is True
        assert cfg.ros_monitor.poll_interval_sec == 1.0

    def test_default_system_monitor(self):
        cfg = BlackBoxConfig.default()
        assert cfg.system_monitor.enabled is True
        assert cfg.system_monitor.gpu_backend == "auto"

    def test_default_anomaly_thresholds(self):
        cfg = BlackBoxConfig.default()
        assert cfg.anomaly_engine.thresholds.cpu_percent == 90.0
        assert cfg.anomaly_engine.thresholds.memory_percent == 85.0
        assert cfg.anomaly_engine.thresholds.gpu_temp_c == 80.0

    def test_default_dead_topic_timeout(self):
        cfg = BlackBoxConfig.default()
        assert cfg.anomaly_engine.dead_topic.timeout_sec == 5.0

    def test_default_frequency_tolerance(self):
        cfg = BlackBoxConfig.default()
        assert cfg.anomaly_engine.frequency.tolerance_percent == 20.0

    def test_default_rosbag2_settings(self):
        cfg = BlackBoxConfig.default()
        assert cfg.rosbag2.enabled is False
        assert cfg.rosbag2.output_dir == "~/.blackboxrs/bags"
        assert cfg.rosbag2.record_duration_sec == 30.0
        assert cfg.rosbag2.executable == "ros2"
        assert cfg.rosbag2.storage_id == "sqlite3"
        assert cfg.rosbag2.trigger_event_types == [
            "anomaly.threshold",
            "anomaly.frequency",
            "anomaly.dead_topic",
            "anomaly.qos_mismatch",
            "anomaly.tf_topology",
            "anomaly.process_signals",
            "anomaly.clock_skew",
        ]


class TestConfigSaveLoad:
    """Test YAML save/load round-trip."""

    def test_save_and_load_round_trip(self, tmp_path: Path):
        cfg = BlackBoxConfig.default()
        cfg_path = tmp_path / "config.yaml"
        cfg.save(cfg_path)

        loaded = BlackBoxConfig.load(cfg_path)
        assert loaded.log_dir == cfg.log_dir
        assert loaded.log_rotation_mb == cfg.log_rotation_mb
        assert loaded.ros_monitor.enabled == cfg.ros_monitor.enabled
        assert loaded.anomaly_engine.thresholds.cpu_percent == cfg.anomaly_engine.thresholds.cpu_percent
        assert loaded.rosbag2.trigger_event_types == cfg.rosbag2.trigger_event_types

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        cfg = BlackBoxConfig.default()
        nested = tmp_path / "deep" / "nested" / "config.yaml"
        cfg.save(nested)
        assert nested.exists()

    def test_load_nonexistent_returns_default(self, tmp_path: Path):
        cfg = BlackBoxConfig.load(tmp_path / "nonexistent.yaml")
        default = BlackBoxConfig.default()
        assert cfg.log_dir == default.log_dir

    def test_custom_values_survive_round_trip(self, tmp_path: Path):
        cfg = BlackBoxConfig(
            log_dir="/custom/logs",
            log_rotation_mb=100,
            log_max_files=10,
            anomaly_engine=AnomalyEngineConfig(
                thresholds=AnomalyThresholds(cpu_percent=75.0),
                dead_topic=DeadTopicConfig(timeout_sec=10.0),
            ),
            rosbag2=Rosbag2RecorderConfig(
                enabled=True,
                record_duration_sec=12.0,
                topics=["/cmd_vel"],
            ),
        )
        cfg_path = tmp_path / "custom.yaml"
        cfg.save(cfg_path)

        loaded = BlackBoxConfig.load(cfg_path)
        assert loaded.log_dir == "/custom/logs"
        assert loaded.log_rotation_mb == 100
        assert loaded.anomaly_engine.thresholds.cpu_percent == 75.0
        assert loaded.anomaly_engine.dead_topic.timeout_sec == 10.0
        assert loaded.rosbag2.enabled is True
        assert loaded.rosbag2.topics == ["/cmd_vel"]


class TestConfigFieldAccess:
    """Test individual nested field access."""

    def test_ros_monitor_config_fields(self):
        cfg = RosMonitorConfig(enabled=False, poll_interval_sec=2.0)
        assert cfg.enabled is False
        assert cfg.poll_interval_sec == 2.0
        assert cfg.topic_filters == []

    def test_system_monitor_config_fields(self):
        cfg = SystemMonitorConfig(interval_sec=5.0, gpu_backend="nvidia-smi")
        assert cfg.interval_sec == 5.0
        assert cfg.gpu_backend == "nvidia-smi"

    def test_anomaly_thresholds_fields(self):
        t = AnomalyThresholds(cpu_percent=50.0, memory_percent=60.0, gpu_temp_c=70.0)
        assert t.cpu_percent == 50.0
        assert t.memory_percent == 60.0
        assert t.gpu_temp_c == 70.0

    def test_frequency_config_fields(self):
        f = FrequencyConfig(tolerance_percent=30.0)
        assert f.tolerance_percent == 30.0

    def test_rosbag2_config_fields(self):
        cfg = Rosbag2RecorderConfig(
            enabled=True,
            cooldown_sec=15.0,
            storage_id="mcap",
            topics=["/scan"],
        )
        assert cfg.enabled is True
        assert cfg.cooldown_sec == 15.0
        assert cfg.storage_id == "mcap"
        assert cfg.topics == ["/scan"]


class TestUnknownKeys:
    """Unknown keys used to be silently dropped. They must now produce
    a warning in lenient mode and a ConfigError in strict mode so
    operators see typos during deployment instead of at 3am."""

    def _write(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        return path

    def test_unknown_top_level_key_warns_by_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        cfg_path = self._write(
            tmp_path / "cfg.yaml",
            "log_dir: /tmp/x\nunknown_top: true\n",
        )
        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            cfg = BlackBoxConfig.load(cfg_path)
        assert cfg.log_dir == "/tmp/x"
        assert any(
            "unknown_top" in rec.message and "top-level" in rec.message
            for rec in caplog.records
        ), f"Expected a warning mentioning unknown_top; got {caplog.records!r}"

    def test_unknown_nested_key_warns_by_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        cfg_path = self._write(
            tmp_path / "cfg.yaml",
            "ros_monitor:\n  enabled: true\n  bogus_key: 5\n",
        )
        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            cfg = BlackBoxConfig.load(cfg_path)
        # The valid field still loads correctly.
        assert cfg.ros_monitor.enabled is True
        assert any(
            "bogus_key" in rec.message and "ros_monitor" in rec.message
            for rec in caplog.records
        )

    def test_unknown_anomaly_inner_key_warns_with_scoped_context(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        cfg_path = self._write(
            tmp_path / "cfg.yaml",
            "anomaly_engine:\n"
            "  thresholds:\n"
            "    cpu_percent: 50\n"
            "    made_up: 1\n",
        )
        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            cfg = BlackBoxConfig.load(cfg_path)
        assert cfg.anomaly_engine.thresholds.cpu_percent == 50
        assert any(
            "made_up" in rec.message
            and "anomaly_engine.thresholds" in rec.message
            for rec in caplog.records
        ), (
            "Inner unknown keys must be reported under their nested "
            "context, not just 'top-level'."
        )

    def test_strict_mode_raises_on_unknown_key(self, tmp_path: Path):
        cfg_path = self._write(
            tmp_path / "cfg.yaml",
            "log_dir: /tmp/y\nbogus: true\n",
        )
        with pytest.raises(ConfigError) as excinfo:
            BlackBoxConfig.load(cfg_path, strict=True)
        assert "bogus" in str(excinfo.value)
        # Source path is included so operators can find the offender.
        assert str(cfg_path) in str(excinfo.value)

    def test_strict_mode_raises_on_nested_unknown_key(self, tmp_path: Path):
        cfg_path = self._write(
            tmp_path / "cfg.yaml",
            "system_monitor:\n  enabled: true\n  weird: 42\n",
        )
        with pytest.raises(ConfigError):
            BlackBoxConfig.load(cfg_path, strict=True)

    def test_known_keys_do_not_emit_warnings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A well-formed config must not log any unknown-key warnings —
        otherwise the signal gets drowned out by false positives."""
        cfg = BlackBoxConfig(
            log_dir="/tmp/ok",
            anomaly_engine=AnomalyEngineConfig(
                thresholds=AnomalyThresholds(cpu_percent=77.0),
                dead_topic=DeadTopicConfig(timeout_sec=3.0),
            ),
        )
        cfg_path = tmp_path / "cfg.yaml"
        cfg.save(cfg_path)

        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            loaded = BlackBoxConfig.load(cfg_path)
        assert loaded.log_dir == "/tmp/ok"
        assert not [
            rec for rec in caplog.records if "Unknown config key" in rec.message
        ], "Round-tripped config produced spurious unknown-key warnings"


class TestCustomDetectorsConfig:
    """Test custom_detectors config field."""

    def test_custom_detectors_default_empty(self):
        cfg = BlackBoxConfig.default()
        assert cfg.anomaly_engine.custom_detectors == []

    def test_custom_detectors_round_trips_through_load(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'anomaly_engine:\n'
            '  custom_detectors:\n'
            '    - class: "mymod.MyDetector"\n'
            '      params:\n'
            '        threshold: 42.0\n'
            '    - class: "othermod.OtherDetector"\n'
        )
        cfg = BlackBoxConfig.load(config_file)
        assert len(cfg.anomaly_engine.custom_detectors) == 2
        assert cfg.anomaly_engine.custom_detectors[0]["class"] == "mymod.MyDetector"
        assert cfg.anomaly_engine.custom_detectors[0]["params"] == {"threshold": 42.0}
        assert cfg.anomaly_engine.custom_detectors[1]["class"] == "othermod.OtherDetector"

    def test_custom_detectors_no_unknown_key_warning(self, tmp_path, caplog):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            'anomaly_engine:\n'
            '  custom_detectors:\n'
            '    - class: "mymod.MyDetector"\n'
        )
        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            BlackBoxConfig.load(config_file)
        assert "Unknown config key" not in caplog.text
