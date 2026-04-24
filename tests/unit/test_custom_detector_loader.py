"""Tests for blackboxrs.anomaly_engine.detectors.loader."""

from __future__ import annotations

import logging

from blackboxrs.anomaly_engine.detectors.base import BaseDetector
from blackboxrs.core.schemas import BlackBoxEvent


# -- Test fixtures: fake detectors defined in this module --------------------

class _ValidDetector(BaseDetector):
    """A minimal valid detector for testing."""

    def __init__(self, threshold: float = 1.0):
        self._threshold = threshold

    @property
    def name(self) -> str:
        return "valid_test_detector"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        return None


class _ValidNoParams(BaseDetector):
    """A valid detector that takes no params."""

    @property
    def name(self) -> str:
        return "no_params_detector"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        return None


class _NotADetector:
    """A class that does NOT subclass BaseDetector."""
    pass


class _CrashingInit(BaseDetector):
    """A detector whose __init__ raises."""

    def __init__(self):
        raise RuntimeError("init exploded")

    @property
    def name(self) -> str:
        return "crasher"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        return None


# -- Tests -------------------------------------------------------------------

_THIS_MODULE = "tests.unit.test_custom_detector_loader"


class TestLoadCustomDetectors:

    def test_valid_import_with_params(self):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [{"class": f"{_THIS_MODULE}._ValidDetector", "params": {"threshold": 99.0}}]
        result = load_custom_detectors(entries)
        assert len(result) == 1
        assert result[0].name == "valid_test_detector"
        assert result[0]._threshold == 99.0

    def test_valid_import_without_params(self):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [{"class": f"{_THIS_MODULE}._ValidNoParams"}]
        result = load_custom_detectors(entries)
        assert len(result) == 1
        assert result[0].name == "no_params_detector"

    def test_bad_import_path_logs_warning(self, caplog):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [{"class": "nonexistent.module.Detector"}]
        with caplog.at_level(logging.WARNING):
            result = load_custom_detectors(entries)
        assert len(result) == 0
        assert "nonexistent.module" in caplog.text

    def test_not_a_subclass_logs_warning(self, caplog):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [{"class": f"{_THIS_MODULE}._NotADetector"}]
        with caplog.at_level(logging.WARNING):
            result = load_custom_detectors(entries)
        assert len(result) == 0
        assert "BaseDetector" in caplog.text

    def test_init_crash_logs_warning(self, caplog):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [{"class": f"{_THIS_MODULE}._CrashingInit"}]
        with caplog.at_level(logging.WARNING):
            result = load_custom_detectors(entries)
        assert len(result) == 0
        assert "init exploded" in caplog.text

    def test_mixed_valid_and_invalid(self, caplog):
        from blackboxrs.anomaly_engine.detectors.loader import load_custom_detectors

        entries = [
            {"class": f"{_THIS_MODULE}._ValidDetector", "params": {"threshold": 1.0}},
            {"class": "nonexistent.module.Bad"},
            {"class": f"{_THIS_MODULE}._ValidNoParams"},
        ]
        with caplog.at_level(logging.WARNING):
            result = load_custom_detectors(entries)
        assert len(result) == 2
        assert result[0].name == "valid_test_detector"
        assert result[1].name == "no_params_detector"
