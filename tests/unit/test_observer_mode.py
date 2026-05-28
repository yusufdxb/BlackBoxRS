"""Tests for runtime.role / observer-mode plumbing.

Covers the four seams introduced by the observer-mode pivot:

- ``RuntimeConfig`` defaults, validation, and YAML round-trip.
- ``BlackBoxConfig.apply_runtime_role`` flipping host-bound subsystems
  off when ``role: observer`` is declared.
- ``Session`` metadata + ``AnomalyEngine`` detector list reflecting
  observer mode.
- ``IncidentBuilder`` populating ``observer_host`` / ``observed_host``
  on the bundle and the rendered report calling them out separately.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from blackboxrs.anomaly_engine.engine import AnomalyEngine
from blackboxrs.core.config import (
    BlackBoxConfig,
    ConfigError,
    RuntimeConfig,
)
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session
from blackboxrs.incident.api import build_incident
from blackboxrs.incident.bundle import BundleReader


_START = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# RuntimeConfig
# ---------------------------------------------------------------------------


class TestRuntimeConfig:
    def test_defaults_are_onboard(self):
        rc = RuntimeConfig()
        assert rc.role == "onboard"
        assert rc.observed_host is None
        assert rc.is_observer is False

    def test_observer_role(self):
        rc = RuntimeConfig(role="observer", observed_host="go2-edu-01")
        assert rc.is_observer is True
        assert rc.observed_host == "go2-edu-01"

    def test_invalid_role_raises(self):
        with pytest.raises(ConfigError):
            RuntimeConfig(role="server")

    def test_top_level_default_includes_runtime(self):
        cfg = BlackBoxConfig.default()
        assert isinstance(cfg.runtime, RuntimeConfig)
        assert cfg.runtime.role == "onboard"


# ---------------------------------------------------------------------------
# apply_runtime_role policy
# ---------------------------------------------------------------------------


class TestApplyRuntimeRole:
    def test_onboard_is_no_op(self):
        cfg = BlackBoxConfig.default()
        cfg.apply_runtime_role()
        assert cfg.system_monitor.enabled is True
        assert cfg.anomaly_engine.observer_mode is False

    def test_observer_disables_system_monitor(self):
        cfg = BlackBoxConfig.default()
        cfg.runtime = RuntimeConfig(role="observer", observed_host="go2-edu-01")
        cfg.apply_runtime_role()
        assert cfg.system_monitor.enabled is False
        assert cfg.anomaly_engine.observer_mode is True

    def test_apply_runtime_role_is_idempotent(self):
        cfg = BlackBoxConfig.default()
        cfg.runtime = RuntimeConfig(role="observer")
        cfg.apply_runtime_role()
        cfg.apply_runtime_role()
        assert cfg.system_monitor.enabled is False
        assert cfg.anomaly_engine.observer_mode is True

    def test_load_applies_observer_policy(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {"runtime": {"role": "observer", "observed_host": "go2-edu-01"}},
                fh,
            )

        cfg = BlackBoxConfig.load(path)
        assert cfg.runtime.role == "observer"
        assert cfg.runtime.observed_host == "go2-edu-01"
        assert cfg.system_monitor.enabled is False
        assert cfg.anomaly_engine.observer_mode is True

    def test_load_invalid_role_raises(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"runtime": {"role": "bogus"}}, fh)

        with pytest.raises(ConfigError):
            BlackBoxConfig.load(path)

    def test_yaml_roundtrip_preserves_runtime(self, tmp_path: Path):
        cfg = BlackBoxConfig.default()
        cfg.runtime = RuntimeConfig(role="observer", observed_host="go2-edu-01")
        cfg.apply_runtime_role()

        path = tmp_path / "out.yaml"
        cfg.save(path)

        loaded = BlackBoxConfig.load(path)
        assert loaded.runtime.role == "observer"
        assert loaded.runtime.observed_host == "go2-edu-01"
        assert loaded.system_monitor.enabled is False
        assert loaded.anomaly_engine.observer_mode is True


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class TestSession:
    def test_onboard_session_metadata_unchanged(self):
        sess = Session()
        meta = sess.metadata()
        assert "role" not in meta
        assert "observed_host" not in meta
        assert meta["hostname"] == socket.gethostname()

    def test_observer_session_metadata_carries_target(self):
        sess = Session(role="observer", observed_host="go2-edu-01")
        meta = sess.metadata()
        assert meta["role"] == "observer"
        assert meta["observed_host"] == "go2-edu-01"

    def test_observer_session_without_target(self):
        # Observed-host is optional; bundle should still call out that
        # the data came from an observer.
        sess = Session(role="observer")
        meta = sess.metadata()
        assert meta["role"] == "observer"
        assert "observed_host" not in meta


# ---------------------------------------------------------------------------
# AnomalyEngine detector list
# ---------------------------------------------------------------------------


class TestAnomalyEngineObserverMode:
    """Engine detector list is the same in onboard and observer mode today.

    The three detectors whose live wiring observer-mode would have to
    *skip* (process_signals, host-cpu threshold variants, etc.) are not
    yet wired in either mode — they will be added back when producers
    ship in system_monitor / ros_monitor. Once they are, observer-mode
    must skip them; the test here will grow at that point.
    """

    _CURRENTLY_WIRED = {"threshold", "frequency", "dead_topic", "qos_mismatch", "tf_topology"}

    def _make_engine(self, observer_mode: bool) -> AnomalyEngine:
        cfg = BlackBoxConfig.default()
        cfg.anomaly_engine.observer_mode = observer_mode
        bus = EventBus()
        sess = Session(role="observer" if observer_mode else "onboard")
        return AnomalyEngine(bus, cfg.anomaly_engine, sess)

    def test_onboard_engine_wired_set(self):
        engine = self._make_engine(observer_mode=False)
        names = {d.name for d in engine._detectors}
        assert names == self._CURRENTLY_WIRED

    def test_observer_engine_keeps_dds_bound_detectors(self):
        engine = self._make_engine(observer_mode=True)
        names = {d.name for d in engine._detectors}
        # Every detector currently wired is DDS-bound, so observer mode
        # is a no-op for the live list today.
        assert names == self._CURRENTLY_WIRED

    def test_orphan_detectors_not_wired(self):
        # Guard against silent re-introduction of detectors without
        # producers. If you wire a producer, add the detector name here
        # AND to _CURRENTLY_WIRED above.
        # tf_topology has been removed from this list because TfSnapshotter
        # (commits 0ec7a41, 1481949) now ships as its live producer.
        for orphan in ("process_signals", "clock_skew"):
            for mode in (False, True):
                engine = self._make_engine(observer_mode=mode)
                names = {d.name for d in engine._detectors}
                assert orphan not in names, (
                    f"{orphan} re-introduced into engine without a producer "
                    f"in system_monitor — see engine.py header."
                )


# ---------------------------------------------------------------------------
# IncidentBuilder + report
# ---------------------------------------------------------------------------


def _ev(t, source, event_type, data, severity="info", metadata=None):
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "sess_obs"},
    }


def _seed_jsonl(path: Path) -> None:
    events = [
        _ev(
            _START,
            "ros_monitor",
            "ros.frequency",
            {"topic": "/scan", "frequency_hz": 10.0},
        ),
        _ev(
            _START + timedelta(seconds=5),
            "anomaly_engine",
            "anomaly.dead_topic",
            {
                "detector": "DeadTopicDetector",
                "topic": "/scan",
                "metric": "/scan",
                "value": 0.0,
                "threshold": 5.0,
                "message": "Topic /scan silent for 5s.",
            },
            severity="error",
            metadata={
                "session_id": "sess_obs",
                "detector_class": (
                    "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector"
                ),
                "signature_fields": ["topic"],
            },
        ),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")


class TestObserverBundle:
    def test_observer_bundle_carries_both_hosts(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _seed_jsonl(log_dir / "blackboxrs_20260517_120000_000000.jsonl")

        cfg = BlackBoxConfig.default()
        cfg.log_dir = str(log_dir)
        cfg.runtime = RuntimeConfig(role="observer", observed_host="go2-edu-01")
        cfg.apply_runtime_role()

        bundle = build_incident(
            _START,
            _START + timedelta(seconds=10),
            config=cfg,
            incidents_dir=tmp_path / "incidents",
        )

        reader = BundleReader(bundle)
        incident = reader.load_incident()

        assert incident.observer_host == socket.gethostname()
        assert incident.observed_host == "go2-edu-01"

        report = (bundle / "report.md").read_text(encoding="utf-8")
        assert "Observer" in report
        assert "Observed" in report
        assert "go2-edu-01" in report
        # The old single-line Host: header should not appear when
        # observer/observed are present.
        host_lines = [
            line for line in report.splitlines() if line.startswith("- **Host**:")
        ]
        assert host_lines == []

    def test_onboard_bundle_unchanged(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _seed_jsonl(log_dir / "blackboxrs_20260517_120000_000000.jsonl")

        cfg = BlackBoxConfig.default()
        cfg.log_dir = str(log_dir)

        bundle = build_incident(
            _START,
            _START + timedelta(seconds=10),
            config=cfg,
            incidents_dir=tmp_path / "incidents",
        )

        reader = BundleReader(bundle)
        incident = reader.load_incident()

        assert incident.observer_host is None
        assert incident.observed_host is None

        report = (bundle / "report.md").read_text(encoding="utf-8")
        assert "**Observer**" not in report
        assert "**Observed**" not in report
        assert "**Host**" in report
