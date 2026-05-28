"""Unit tests for :class:`ProcessSignalsDetector`.

Mirrors the style of ``test_detectors.py``: events constructed here use
the *real* ``system.process_signals`` event_type and data shape that
the detector consumes. ``psutil`` is not imported here either; the
detector is a pure event-stream consumer.
"""

from __future__ import annotations

from blackboxrs.anomaly_engine.detectors.process_signals import (
    ProcessSignalsDetector,
)
from blackboxrs.core.config import ProcessSignalsConfig
from blackboxrs.core.schemas import BlackBoxEvent


def _proc_event(processes: list[dict] | None) -> BlackBoxEvent:
    return BlackBoxEvent.system_event(
        event_type="system.process_signals",
        data={"sampling_interval_sec": 1.0, "processes": processes or []},
    )


class TestProcessSignalsDetector:
    """End-to-end behavioural tests for the process-signals detector."""

    # -- Empty / silent paths -------------------------------------------

    def test_empty_process_list_returns_none(self):
        detector = ProcessSignalsDetector()
        assert detector.check(_proc_event(processes=[])) is None

    def test_missing_processes_key_returns_none(self):
        detector = ProcessSignalsDetector()
        ev = BlackBoxEvent.system_event(
            event_type="system.process_signals",
            data={"sampling_interval_sec": 1.0},
        )
        assert detector.check(ev) is None

    def test_ignores_non_system_events(self):
        detector = ProcessSignalsDetector()
        ev = BlackBoxEvent.ros_event(
            event_type="system.process_signals",
            data={"processes": [{"pid": 1, "name": "x", "cpu_percent": 99.0}]},
        )
        assert detector.check(ev) is None

    def test_ignores_unrelated_event_type(self):
        detector = ProcessSignalsDetector()
        ev = BlackBoxEvent.system_event(
            event_type="system.cpu",
            data={"cpu_percent": 99.0},
        )
        assert detector.check(ev) is None

    def test_ignores_non_numeric_cpu(self):
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=10.0, rss_mb=10.0)
        )
        ev = _proc_event(processes=[
            {"pid": 1, "name": "x", "cpu_percent": "high", "rss_mb": 5.0},
        ])
        # Only entry has malformed cpu; nothing to score, no anomaly.
        assert detector.check(ev) is None

    # -- Normal, healthy snapshot ---------------------------------------

    def test_within_thresholds_is_silent(self):
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=90.0, rss_mb=1024.0)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 12.0, "rss_mb": 80.0},
            {"pid": 101, "name": "controller", "cpu_percent": 7.5, "rss_mb": 64.0},
        ]
        assert detector.check(_proc_event(processes=processes)) is None

    # -- CPU peak --------------------------------------------------------

    def test_cpu_peak_fires_with_worst_offender(self):
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0, min_consecutive_samples=1)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 12.0, "rss_mb": 80.0},
            {"pid": 200, "name": "runaway_planner",
             "cpu_percent": 195.0, "rss_mb": 200.0},
            {"pid": 300, "name": "controller",
             "cpu_percent": 95.0, "rss_mb": 100.0},
        ]
        result = detector.check(_proc_event(processes=processes))
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.process_signals"
        assert result.data["process_name"] == "runaway_planner"
        assert result.data["pid"] == 200
        assert result.data["metric"] == "process:cpu_percent:runaway_planner"
        # signature_fields exposes process_name + metric in data so the
        # fingerprint algorithm can read them.
        assert result.data["metric"].endswith("runaway_planner")
        assert result.metadata["signature_fields"] == ["process_name", "metric"]
        assert result.metadata["target_subsystem"] == "system"
        # Full snapshot preserved for the report renderer.
        assert len(result.data["all_processes"]) == 3

    def test_cpu_takes_priority_over_rss(self):
        """A CPU spike and an RSS spike in the same snapshot -> CPU wins."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=100.0, min_consecutive_samples=1)
        )
        processes = [
            # CPU offender
            {"pid": 100, "name": "burner", "cpu_percent": 120.0, "rss_mb": 50.0},
            # RSS offender
            {"pid": 200, "name": "leaker", "cpu_percent": 5.0, "rss_mb": 800.0},
        ]
        result = detector.check(_proc_event(processes=processes))
        assert result is not None
        assert result.data["process_name"] == "burner"
        # Anomaly reports the CPU metric; RSS leak still visible in
        # all_processes for the report.
        assert "cpu_percent" in result.data["metric"]

    # -- RSS peak --------------------------------------------------------

    def test_rss_peak_fires_when_no_cpu_offender(self):
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=99.0, rss_mb=200.0, min_consecutive_samples=1)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 5.0, "rss_mb": 80.0},
            {"pid": 200, "name": "leaker", "cpu_percent": 12.0, "rss_mb": 600.0},
        ]
        result = detector.check(_proc_event(processes=processes))
        assert result is not None
        assert result.data["process_name"] == "leaker"
        assert result.data["value"] == 600.0
        assert result.data["threshold"] == 200.0
        assert "rss_mb" in result.data["metric"]

    def test_rss_missing_does_not_crash(self):
        """An entry without ``rss_mb`` should be tolerated, not blow up."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=10.0, rss_mb=200.0)
        )
        processes = [
            {"pid": 100, "name": "x", "cpu_percent": 5.0},  # no rss_mb
            {"pid": 200, "name": "y", "cpu_percent": 1.0, "rss_mb": 30.0},
        ]
        # CPU well under limit; RSS well under limit. Healthy.
        assert detector.check(_proc_event(processes=processes)) is None

    # -- Misc ------------------------------------------------------------

    def test_name_property(self):
        assert ProcessSignalsDetector().name == "process_signals"

    # -- Hysteresis (min_consecutive_samples) ---------------------------

    def test_process_signals_single_sample_does_not_fire_with_default_min2(self):
        """First violating snapshot is silent with min_consecutive_samples=2."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 195.0, "rss_mb": 100.0},
        ]
        assert detector.check(_proc_event(processes=processes)) is None

    def test_process_signals_two_consecutive_violations_fire_on_second(self):
        """Two consecutive CPU violations fire on the second."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 195.0, "rss_mb": 100.0},
        ]
        assert detector.check(_proc_event(processes=processes)) is None
        result = detector.check(_proc_event(processes=processes))
        assert result is not None
        assert result.event_type == "anomaly.process_signals"

    def test_process_signals_healthy_snapshot_resets_counter(self):
        """A healthy snapshot resets the violation counter."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0)
        )
        violating = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 195.0, "rss_mb": 100.0},
        ]
        healthy = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 12.0, "rss_mb": 100.0},
        ]
        assert detector.check(_proc_event(processes=violating)) is None
        # Healthy resets counter.
        assert detector.check(_proc_event(processes=healthy)) is None
        # Violation again: counter = 1, not 2. Should not fire.
        assert detector.check(_proc_event(processes=violating)) is None

    def test_process_signals_sustained_violations_keep_firing(self):
        """After the initial fire, sustained violations keep firing."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 195.0, "rss_mb": 100.0},
        ]
        detector.check(_proc_event(processes=processes))  # count=1
        result2 = detector.check(_proc_event(processes=processes))
        assert result2 is not None
        result3 = detector.check(_proc_event(processes=processes))
        assert result3 is not None

    def test_process_signals_min_consecutive_1_reproduces_v040_behavior(self):
        """min_consecutive_samples=1 fires on the first violating snapshot."""
        detector = ProcessSignalsDetector(
            ProcessSignalsConfig(cpu_percent=80.0, rss_mb=10_000.0, min_consecutive_samples=1)
        )
        processes = [
            {"pid": 100, "name": "scan_node", "cpu_percent": 195.0, "rss_mb": 100.0},
        ]
        result = detector.check(_proc_event(processes=processes))
        assert result is not None

