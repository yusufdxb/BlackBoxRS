"""Tests for blackboxrs.core.config."""

from __future__ import annotations

from pathlib import Path

from blackboxrs.core.config import (
    AnomalyEngineConfig,
    AnomalyThresholds,
    BlackBoxConfig,
    DeadTopicConfig,
    FrequencyConfig,
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
        )
        cfg_path = tmp_path / "custom.yaml"
        cfg.save(cfg_path)

        loaded = BlackBoxConfig.load(cfg_path)
        assert loaded.log_dir == "/custom/logs"
        assert loaded.log_rotation_mb == 100
        assert loaded.anomaly_engine.thresholds.cpu_percent == 75.0
        assert loaded.anomaly_engine.dead_topic.timeout_sec == 10.0


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
