"""Tests for blackboxrs.metrics.prometheus_exporter."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from blackboxrs.core.config import BlackBoxConfig, PrometheusConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    source: str = "system_monitor",
    event_type: str = "system.cpu",
    data: dict | None = None,
) -> BlackBoxEvent:
    return BlackBoxEvent(
        timestamp=datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc),
        source=source,
        event_type=event_type,
        severity="info",
        data=data or {},
        metadata={"session_id": "test"},
    )


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestPrometheusConfig:
    """PrometheusConfig dataclass defaults and round-trip."""

    def test_defaults(self):
        cfg = PrometheusConfig()
        assert cfg.enabled is False
        assert cfg.port == 9100
        assert cfg.host == "0.0.0.0"

    def test_config_in_blackbox_config(self):
        cfg = BlackBoxConfig.default()
        assert cfg.prometheus.enabled is False
        assert cfg.prometheus.port == 9100

    def test_config_round_trip(self, tmp_path):
        cfg = BlackBoxConfig(prometheus=PrometheusConfig(enabled=True, port=9200))
        path = tmp_path / "cfg.yaml"
        cfg.save(path)
        loaded = BlackBoxConfig.load(path)
        assert loaded.prometheus.enabled is True
        assert loaded.prometheus.port == 9200

    def test_no_unknown_key_warning(self, tmp_path, caplog):
        cfg = BlackBoxConfig(prometheus=PrometheusConfig(enabled=True))
        path = tmp_path / "cfg.yaml"
        cfg.save(path)
        with caplog.at_level(logging.WARNING, logger="blackboxrs.core.config"):
            BlackBoxConfig.load(path)
        assert "Unknown config key" not in caplog.text


# ---------------------------------------------------------------------------
# Exporter unit tests (mock prometheus_client)
# ---------------------------------------------------------------------------


class _FakeMetric:
    """Minimal stand-in for Counter / Gauge with .labels() support."""

    def __init__(self):
        self._value = 0.0
        self._labels: dict[tuple, _FakeMetric] = {}

    def inc(self, amount: float = 1.0):
        self._value += amount

    def set(self, value: float):
        self._value = value

    def labels(self, **kwargs) -> _FakeMetric:
        key = tuple(sorted(kwargs.items()))
        if key not in self._labels:
            self._labels[key] = _FakeMetric()
        return self._labels[key]

    @property
    def value(self) -> float:
        return self._value


class _FakeRegistry:
    pass


@pytest.fixture
def _mock_prometheus(monkeypatch):
    """Patch prometheus_client so the exporter works without the real lib."""
    fake_mod = MagicMock()
    fake_mod.Counter = lambda name, doc, labels=None, registry=None: _FakeMetric()
    fake_mod.Gauge = lambda name, doc, labels=None, registry=None: _FakeMetric()
    fake_mod.CollectorRegistry = _FakeRegistry
    fake_mod.start_http_server = MagicMock(return_value=MagicMock())

    import sys

    monkeypatch.setitem(sys.modules, "prometheus_client", fake_mod)
    return fake_mod


@pytest.fixture
def exporter(_mock_prometheus):
    """Return a PrometheusExporter with metrics initialised (no HTTP server)."""
    from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

    bus = EventBus()
    cfg = PrometheusConfig(enabled=True, port=19100)
    exp = PrometheusExporter(bus, cfg)
    exp._metrics_initialized = False
    return exp


class TestExporterLifecycle:
    def test_disabled_by_default(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=False)
        exp = PrometheusExporter(bus, cfg)
        exp.start()
        assert not exp._running

    def test_start_stop(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19101)
        exp = PrometheusExporter(bus, cfg)
        exp.start()
        assert exp._running
        exp.stop()
        assert not exp._running
        assert exp._queue is None

    def test_stop_idempotent(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19102)
        exp = PrometheusExporter(bus, cfg)
        exp.start()
        exp.stop()
        exp.stop()


class TestExporterMetrics:
    def test_events_total_incremented(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19103)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        event = _make_event(event_type="system.cpu", data={"cpu_percent": 42.0})
        exp._process_event(event, seen)

        label_key = (("event_type", "system.cpu"),)
        assert exp._events_total._labels[label_key].value == 1.0

    def test_anomaly_counter(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19104)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        event = _make_event(
            source="anomaly_engine",
            event_type="anomaly.threshold",
            data={
                "detector": "threshold",
                "metric": "cpu_percent",
                "value": 95.0,
                "threshold": 90.0,
                "message": "high",
            },
        )
        exp._process_event(event, seen)

        label_key = (("detector_name", "threshold"),)
        assert exp._anomalies_total._labels[label_key].value == 1.0

    def test_cpu_gauge_updated(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19105)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        exp._process_event(
            _make_event(event_type="system.cpu", data={"cpu_percent": 73.5}),
            seen,
        )
        assert exp._cpu_percent.value == 73.5

    def test_memory_gauge_updated(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19106)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        exp._process_event(
            _make_event(event_type="system.memory", data={"memory_percent": 62.1}),
            seen,
        )
        assert exp._memory_percent.value == 62.1

    def test_gpu_temp_gauge_updated(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19107)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        exp._process_event(
            _make_event(event_type="system.gpu", data={"gpu_temp_c": 71.0}),
            seen,
        )
        assert exp._gpu_temp_c.value == 71.0

    def test_topics_monitored_gauge(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19108)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        exp._process_event(
            _make_event(
                source="ros_monitor",
                event_type="ros.frequency",
                data={"topic": "/cmd_vel", "frequency_hz": 10.0},
            ),
            seen,
        )
        assert exp._topics_monitored.value == 1.0

        exp._process_event(
            _make_event(
                source="ros_monitor",
                event_type="ros.frequency",
                data={"topic": "/scan", "frequency_hz": 5.0},
            ),
            seen,
        )
        assert exp._topics_monitored.value == 2.0

        # Same topic again should not increment
        exp._process_event(
            _make_event(
                source="ros_monitor",
                event_type="ros.frequency",
                data={"topic": "/cmd_vel", "frequency_hz": 10.0},
            ),
            seen,
        )
        assert exp._topics_monitored.value == 2.0

    def test_process_event_handles_missing_data_keys(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19109)
        exp = PrometheusExporter(bus, cfg)
        exp._init_metrics()

        seen: set[str] = set()
        exp._process_event(_make_event(event_type="system.cpu", data={}), seen)
        assert exp._cpu_percent.value == 0.0


class TestExporterImportGuard:
    """Verify the exporter does not crash when prometheus_client is absent."""

    def test_init_metrics_returns_false_without_lib(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "prometheus_client", None)

        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True)
        exp = PrometheusExporter(bus, cfg)
        exp._metrics_initialized = False
        assert exp._init_metrics() is False

    def test_start_graceful_without_lib(self, monkeypatch, caplog):
        import sys

        monkeypatch.setitem(sys.modules, "prometheus_client", None)

        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True)
        exp = PrometheusExporter(bus, cfg)
        exp._metrics_initialized = False

        with caplog.at_level(logging.WARNING):
            exp.start()

        assert not exp._running
        assert any("prometheus_client" in r.message for r in caplog.records)


class TestExporterIntegration:
    """End-to-end: publish events on the bus and verify metrics update."""

    def test_events_flow_through_bus(self, _mock_prometheus):
        from blackboxrs.metrics.prometheus_exporter import PrometheusExporter

        bus = EventBus()
        cfg = PrometheusConfig(enabled=True, port=19110)
        exp = PrometheusExporter(bus, cfg)
        exp.start()

        bus.publish(_make_event(event_type="system.cpu", data={"cpu_percent": 50.0}))
        bus.publish(_make_event(event_type="system.cpu", data={"cpu_percent": 60.0}))

        # Give the drain thread time to process
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            label_key = (("event_type", "system.cpu"),)
            if (
                label_key in exp._events_total._labels
                and exp._events_total._labels[label_key].value >= 2.0
            ):
                break
            time.sleep(0.05)

        label_key = (("event_type", "system.cpu"),)
        assert exp._events_total._labels[label_key].value >= 2.0
        assert exp._cpu_percent.value == 60.0

        exp.stop()
