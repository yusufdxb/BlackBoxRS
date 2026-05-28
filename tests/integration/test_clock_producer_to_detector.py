"""Integration test: ClockSkewCollector → EventBus → ClockSkewDetector.

Injects synthetic source lists directly through the bus and asserts that
the detector fires with the correct worst-pair signature and that
observer-mode producer output (no ``peer:`` source) does not break
either end of the pipeline.
"""

from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

import pytest

from blackboxrs.anomaly_engine.detectors.clock_skew import ClockSkewDetector
from blackboxrs.core.config import ClockProducerConfig, ClockSkewConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.system_monitor.collectors.clock import ClockSkewCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHRONYC_LARGE_OFFSET = (
    "7F7F0101,pool.ntp.org,2,1715290000.000000000,0.500000000,"
    "0.000001,0.001,0.002,0.010,0.020,64.0\n"
)
# offset = +0.5 s  →  ntp_epoch = system_now - 0.5  →  |system - ntp| = 0.5 s


def _make_collector(
    bus: EventBus,
    *,
    chronyc_output: str = CHRONYC_LARGE_OFFSET,
) -> ClockSkewCollector:
    cfg = ClockProducerConfig(
        enabled=True,
        sample_hz=1.0,
        include_ntp=True,
        include_ros_clock="false",
        ntp_tool="chronyc",
    )
    collector = ClockSkewCollector(
        config=cfg,
        event_bus=bus,
        session_meta={"session_id": "integ-sess"},
    )
    collector.set_window_budget(1.0)  # generous for integration tests
    return collector


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClockProducerToDetector:
    """End-to-end: producer tick → bus → detector check."""

    def _run_pipeline(
        self,
        chronyc_output: str = CHRONYC_LARGE_OFFSET,
        max_skew_sec: float = 0.1,
    ) -> BlackBoxEvent | None:
        """Drive one producer tick and pass the resulting event to the
        detector.  Returns the detector output (anomaly or None)."""
        bus = EventBus()
        sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        fake_proc = MagicMock()
        fake_proc.stdout = chronyc_output

        with (
            patch("shutil.which", return_value="/usr/bin/chronyc"),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(bus, chronyc_output=chronyc_output)
            collector.tick()

        bus.unsubscribe(sub)

        # Pull the event from the bus queue.
        try:
            event = sub.get_nowait()
        except queue.Empty:
            return None

        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=max_skew_sec))
        return detector.check(event)

    def test_large_skew_fires_anomaly(self):
        """0.5 s skew with 0.1 s tolerance must produce an anomaly."""
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        assert anomaly is not None
        assert anomaly.event_type == "anomaly.clock_skew"

    def test_anomaly_carries_source_names(self):
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        assert anomaly is not None
        assert "source_a" in anomaly.data
        assert "source_b" in anomaly.data
        pair = {anomaly.data["source_a"], anomaly.data["source_b"]}
        assert "system" in pair
        assert any(n.startswith("ntp:") for n in pair)

    def test_worst_pair_lexical_order(self):
        """source_a <= source_b (detector stores in lexical order)."""
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        assert anomaly is not None
        assert anomaly.data["source_a"] <= anomaly.data["source_b"]

    def test_anomaly_skew_value_matches_offset(self):
        """The reported skew must be ~0.5 s (chronyc fixture offset)."""
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        assert anomaly is not None
        assert abs(anomaly.data["value"] - 0.5) < 0.05

    def test_small_skew_does_not_fire(self):
        """With a 10 s tolerance, 0.5 s skew is within bounds."""
        anomaly = self._run_pipeline(max_skew_sec=10.0)
        assert anomaly is None

    def test_all_sources_present_in_anomaly(self):
        """anomaly.data['all_sources'] must list the two sources."""
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        assert anomaly is not None
        all_s = anomaly.data.get("all_sources", [])
        assert len(all_s) == 2
        names = {s["name"] for s in all_s}
        assert "system" in names

    def test_fingerprint_stability_across_ticks(self):
        """Two ticks with the same sources must produce the same pair."""
        bus = EventBus()
        fake_proc = MagicMock()
        fake_proc.stdout = CHRONYC_LARGE_OFFSET
        detector = ClockSkewDetector(ClockSkewConfig(max_skew_sec=0.1))

        anomalies = []
        with (
            patch("shutil.which", return_value="/usr/bin/chronyc"),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(bus, chronyc_output=CHRONYC_LARGE_OFFSET)
            sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)
            for _ in range(2):
                collector.tick()
            bus.unsubscribe(sub)

        events = []
        while True:
            try:
                events.append(sub.get_nowait())
            except queue.Empty:
                break

        assert len(events) == 2
        for ev in events:
            anomalies.append(detector.check(ev))

        pairs = [
            (a.data["source_a"], a.data["source_b"])
            for a in anomalies
            if a is not None
        ]
        assert len(pairs) == 2
        assert pairs[0] == pairs[1], "fingerprint must be stable across ticks"

    def test_observer_mode_no_peer_source_does_not_crash(self):
        """Observer-mode: no peer: source is present. Pipeline must still
        work (system + ntp:* is enough to emit and detect)."""
        anomaly = self._run_pipeline(max_skew_sec=0.1)
        # Anomaly is still expected — we just confirm no crash and event fires.
        assert anomaly is not None

    def test_single_source_snapshot_not_emitted(self):
        """When only 'system' is available (NTP absent, ROS disabled),
        the collector must not emit any event."""
        bus = EventBus()
        sub: queue.Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        import blackboxrs.system_monitor.collectors.clock as clk_mod

        clk_mod._CHRONYC_WARNED = False
        clk_mod._NTPQ_WARNED = False

        cfg = ClockProducerConfig(
            enabled=True,
            sample_hz=1.0,
            include_ntp=True,
            include_ros_clock="false",
            ntp_tool="auto",
        )
        collector = ClockSkewCollector(
            config=cfg,
            event_bus=bus,
            session_meta={"session_id": "integ-sess"},
        )
        collector.set_window_budget(1.0)

        with patch(
            "blackboxrs.system_monitor.collectors.clock.shutil.which",
            return_value=None,
        ):
            collector.tick()

        bus.unsubscribe(sub)
        with pytest.raises(queue.Empty):
            sub.get_nowait()
