"""Unit tests for :class:`ClockSkewDetector`.

Mirrors the style of ``test_detectors.py``: events constructed here use
the *real* ``system.clock_skew`` event_type and data shape that the
detector consumes. No ``ntpq`` / ``rclpy`` imports are needed; the
detector is a pure event-stream consumer.
"""

from __future__ import annotations

from blackboxrs.anomaly_engine.detectors.clock_skew import (
    ClockSkewConfig,
    ClockSkewDetector,
)
from blackboxrs.core.schemas import BlackBoxEvent


def _skew_event(sources: list[dict] | None) -> BlackBoxEvent:
    return BlackBoxEvent.system_event(
        event_type="system.clock_skew",
        data={"sources": sources or []},
    )


class TestClockSkewDetector:
    """End-to-end behavioural tests for the clock-skew detector."""

    # -- Empty / silent paths -------------------------------------------

    def test_empty_sources_returns_none(self):
        detector = ClockSkewDetector()
        assert detector.check(_skew_event(sources=[])) is None

    def test_single_source_returns_none(self):
        """No pair to compare; nothing to do."""
        detector = ClockSkewDetector()
        assert detector.check(
            _skew_event(sources=[{"name": "system", "epoch_sec": 100.0}])
        ) is None

    def test_ignores_non_system_events(self):
        detector = ClockSkewDetector()
        ev = BlackBoxEvent.ros_event(
            event_type="system.clock_skew",
            data={"sources": [
                {"name": "a", "epoch_sec": 0.0},
                {"name": "b", "epoch_sec": 5.0},
            ]},
        )
        assert detector.check(ev) is None

    def test_ignores_unrelated_event_type(self):
        detector = ClockSkewDetector()
        ev = BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_percent": 50.0},
        )
        assert detector.check(ev) is None

    def test_skips_malformed_entries(self):
        """Entries missing name or epoch_sec must be silently dropped."""
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.05))
        sources = [
            {"name": "system", "epoch_sec": 100.0},
            {"name": "broken"},                 # no epoch
            {"epoch_sec": 200.0},               # no name
            {"name": "ntp", "epoch_sec": "high"},  # malformed
        ]
        # Only one usable source remains; below pair threshold.
        assert detector.check(_skew_event(sources=sources)) is None

    # -- Healthy snapshot -----------------------------------------------

    def test_within_tolerance_is_silent(self):
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.5))
        sources = [
            {"name": "system", "epoch_sec": 1_000.000},
            {"name": "ntp:pool", "epoch_sec": 1_000.020},
            {"name": "ros:/clock", "epoch_sec": 999.990},
        ]
        # max pair skew is 0.030s, well under 0.5s.
        assert detector.check(_skew_event(sources=sources)) is None

    # -- Anomaly: worst pair fires --------------------------------------

    def test_fires_on_worst_pair(self):
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.1))
        sources = [
            {"name": "system", "epoch_sec": 1_000.000},
            {"name": "ntp:pool", "epoch_sec": 1_000.050},
            {"name": "ros:/clock", "epoch_sec": 1_002.500},
        ]
        result = detector.check(_skew_event(sources=sources))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.clock_skew"
        # Worst pair is system <-> ros:/clock at 2.5s.
        assert result.data["source_a"] == "ros:/clock"  # lexical sort
        assert result.data["source_b"] == "system"
        assert abs(result.data["value"] - 2.5) < 1e-9
        assert result.data["threshold"] == 0.1
        assert result.metadata["signature_fields"] == [
            "source_a", "source_b",
        ]
        assert result.metadata["target_subsystem"] == "system"
        assert len(result.data["all_sources"]) == 3

    def test_pair_order_is_lexical_for_fingerprint_stability(self):
        """(a, b) and (b, a) must yield the same source_a/source_b ordering."""
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.1))
        # Submit b before a; expect lexical ordering in the output.
        sources = [
            {"name": "zeta", "epoch_sec": 100.0},
            {"name": "alpha", "epoch_sec": 105.0},
        ]
        result = detector.check(_skew_event(sources=sources))
        assert result is not None
        assert result.data["source_a"] == "alpha"
        assert result.data["source_b"] == "zeta"

    def test_negative_skew_is_absolute(self):
        """Skew is always a positive magnitude regardless of who's ahead."""
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.5))
        sources = [
            {"name": "a", "epoch_sec": 100.0},
            {"name": "b", "epoch_sec": 95.0},  # b is behind by 5s
        ]
        result = detector.check(_skew_event(sources=sources))
        assert result is not None
        assert abs(result.data["value"] - 5.0) < 1e-9

    # -- Misc ------------------------------------------------------------

    def test_name_property(self):
        assert ClockSkewDetector().name == "clock_skew"
