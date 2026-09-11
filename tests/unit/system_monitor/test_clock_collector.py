"""Unit tests for ClockSkewCollector and its pure-function NTP parsers.

All tests are offline: no subprocesses, no ROS, no real NTP queries.
Subprocess calls are patched at the ``subprocess.run`` boundary.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from blackboxrs.core.config import ClockProducerConfig
from blackboxrs.system_monitor.collectors.clock import (
    ClockSkewCollector,
    parse_chronyc_tracking,
    parse_ntpq_peers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHRONYC_SINGLE_PEER = (
    "7F7F0101,ntp.ubuntu.com,2,1715290000.000000000,0.000123456,"
    "0.000045678,0.001,0.002,0.010,0.020,64.0\n"
)

CHRONYC_AHEAD = (
    # offset is positive (system is ahead of NTP)
    "C0A80101,192.168.1.1,1,1715290100.000000000,-0.000500000,"
    "0.000100000,0.001,0.002,0.005,0.010,32.0\n"
)

CHRONYC_UNSYNC = (
    # Ref-ID is 0.0.0.0: no peer selected
    "00000000,0.0.0.0,0,0.000000000,0.000000000,"
    "0.000000000,0.000,0.000,0.000,0.000,0.0\n"
)

CHRONYC_MALFORMED = "not,enough\n"

NTPQ_SELECTED = """\
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*pool.ntp.org    .POOL.          16 p    -  256    0    0.000    0.412  0.000
 192.168.1.254   .LOCAL.          0 l  888   64    0    0.000    0.000  0.000
"""

NTPQ_PREFERRED_ONLY = """\
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
+ntp1.example.com .POOL.         2 u   10   64  377    1.234   -0.200  0.100
 192.168.1.1      .LOCAL.         0 l  888   64    0    0.000    0.000  0.000
"""

NTPQ_NO_PEER = """\
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
 192.168.1.1      .LOCAL.         0 l  888   64    0    0.000    0.000  0.000
"""

NTPQ_MALFORMED = "no useful lines here\n"


# ---------------------------------------------------------------------------
# parse_chronyc_tracking
# ---------------------------------------------------------------------------


class TestParseChronyc:
    def test_single_peer_returns_one_entry(self):
        result = parse_chronyc_tracking(CHRONYC_SINGLE_PEER)
        assert result is not None
        assert len(result) == 1
        name, epoch = result[0]
        assert name == "ntp:ntp.ubuntu.com"
        # epoch_sec should be close to current time (within a few seconds
        # of test execution: the offset in the fixture is tiny)
        assert abs(epoch - time.time()) < 5.0

    def test_peer_name_prefixed_with_ntp(self):
        result = parse_chronyc_tracking(CHRONYC_SINGLE_PEER)
        assert result is not None
        assert result[0][0].startswith("ntp:")

    def test_negative_offset_applied_correctly(self):
        """offset = -0.0005 means system is behind NTP; NTP is ahead."""
        result = parse_chronyc_tracking(CHRONYC_AHEAD)
        assert result is not None
        name, epoch = result[0]
        assert name == "ntp:192.168.1.1"
        # NTP epoch = system_now - offset = system_now - (-0.0005)
        # so ntp_epoch > system_now (NTP is 0.5 ms ahead of system)
        assert epoch > time.time() - 1.0  # sanity check

    def test_unsynchronised_returns_none(self):
        result = parse_chronyc_tracking(CHRONYC_UNSYNC)
        assert result is None

    def test_malformed_line_returns_none(self):
        result = parse_chronyc_tracking(CHRONYC_MALFORMED)
        assert result is None

    def test_empty_input_returns_none(self):
        result = parse_chronyc_tracking("")
        assert result is None


# ---------------------------------------------------------------------------
# parse_ntpq_peers
# ---------------------------------------------------------------------------


class TestParseNtpq:
    def test_selected_peer_star_prefix(self):
        result = parse_ntpq_peers(NTPQ_SELECTED)
        assert result is not None
        assert len(result) == 1
        name, epoch = result[0]
        assert name == "ntp:pool.ntp.org"
        # offset 0.412 ms → ~0.000412 s; epoch ≈ time.time() - 0.000412
        assert abs(epoch - time.time()) < 5.0

    def test_preferred_peer_plus_prefix_when_no_star(self):
        result = parse_ntpq_peers(NTPQ_PREFERRED_ONLY)
        assert result is not None
        name, epoch = result[0]
        assert name == "ntp:ntp1.example.com"

    def test_no_usable_peer_returns_none(self):
        result = parse_ntpq_peers(NTPQ_NO_PEER)
        assert result is None

    def test_malformed_input_returns_none(self):
        result = parse_ntpq_peers(NTPQ_MALFORMED)
        assert result is None

    def test_empty_input_returns_none(self):
        result = parse_ntpq_peers("")
        assert result is None


# ---------------------------------------------------------------------------
# ClockSkewCollector: helper builders
# ---------------------------------------------------------------------------


def _make_collector(
    *,
    include_ntp: bool = False,
    include_ros_clock: str = "false",
    ntp_tool: str = "auto",
    bus: object | None = None,
) -> ClockSkewCollector:
    cfg = ClockProducerConfig(
        enabled=True,
        sample_hz=1.0,
        include_ntp=include_ntp,
        include_ros_clock=include_ros_clock,
        ntp_tool=ntp_tool,
    )
    bus = bus or MagicMock()
    collector = ClockSkewCollector(
        config=cfg,
        event_bus=bus,
        session_meta={"session_id": "test-sess"},
    )
    # Tight budget so read-window tests work without real delay
    collector.set_window_budget(0.05)
    return collector


# ---------------------------------------------------------------------------
# ClockSkewCollector: single source: no event emitted
# ---------------------------------------------------------------------------


class TestClockCollectorSingleSource:
    def test_no_ntp_no_ros_single_source_no_event(self):
        """With only 'system' source, tick() should return None."""
        bus = MagicMock()
        collector = _make_collector(include_ntp=False, bus=bus)
        result = collector.tick()
        assert result is None
        bus.publish.assert_not_called()

    def test_no_ntp_tool_available_skips_cleanly(self):
        """When NTP tool is not on PATH and ros is disabled, only system
        source is gathered: no event."""
        bus = MagicMock()
        with (
            patch(
                "blackboxrs.system_monitor.collectors.clock.shutil.which",
                return_value=None,
            ),
        ):
            collector = _make_collector(include_ntp=True, bus=bus)
            # reset warned flag so warning fires fresh
            import blackboxrs.system_monitor.collectors.clock as clk_mod

            clk_mod._CHRONYC_WARNED = False
            clk_mod._NTPQ_WARNED = False
            result = collector.tick()
        assert result is None
        bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# ClockSkewCollector: two sources: event emitted
# ---------------------------------------------------------------------------


class TestClockCollectorTwoSources:
    def test_system_plus_chronyc_emits_event(self):
        bus = MagicMock()
        fake_proc = MagicMock()
        fake_proc.stdout = CHRONYC_SINGLE_PEER
        with (
            patch("shutil.which", return_value="/usr/bin/chronyc"),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(
                include_ntp=True, ntp_tool="chronyc", bus=bus
            )
            result = collector.tick()

        assert result is not None
        assert "sources" in result
        names = [s["name"] for s in result["sources"]]
        assert "system" in names
        assert any(n.startswith("ntp:") for n in names)
        bus.publish.assert_called_once()

    def test_published_event_has_correct_event_type(self):
        bus = MagicMock()
        fake_proc = MagicMock()
        fake_proc.stdout = CHRONYC_SINGLE_PEER
        with (
            patch("shutil.which", return_value="/usr/bin/chronyc"),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(
                include_ntp=True, ntp_tool="chronyc", bus=bus
            )
            collector.tick()

        event = bus.publish.call_args[0][0]
        assert event.event_type == "system.clock_skew"
        assert event.source == "system_monitor"

    def test_epoch_sec_fields_are_floats(self):
        bus = MagicMock()
        fake_proc = MagicMock()
        fake_proc.stdout = CHRONYC_SINGLE_PEER
        with (
            patch("shutil.which", return_value="/usr/bin/chronyc"),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(
                include_ntp=True, ntp_tool="chronyc", bus=bus
            )
            result = collector.tick()

        assert result is not None
        for src in result["sources"]:
            assert isinstance(src["epoch_sec"], float)

    def test_ntpq_fallback_used_when_chronyc_absent(self):
        bus = MagicMock()
        fake_proc = MagicMock()
        fake_proc.stdout = NTPQ_SELECTED

        def which_side_effect(cmd: str) -> str | None:
            return None if cmd == "chronyc" else "/usr/bin/ntpq"

        import blackboxrs.system_monitor.collectors.clock as clk_mod

        clk_mod._CHRONYC_WARNED = False
        clk_mod._NTPQ_WARNED = False

        with (
            patch(
                "blackboxrs.system_monitor.collectors.clock.shutil.which",
                side_effect=which_side_effect,
            ),
            patch("subprocess.run", return_value=fake_proc),
        ):
            collector = _make_collector(
                include_ntp=True, ntp_tool="auto", bus=bus
            )
            result = collector.tick()

        assert result is not None
        names = [s["name"] for s in result["sources"]]
        assert any(n.startswith("ntp:") for n in names)


# ---------------------------------------------------------------------------
# ClockSkewCollector: read-window guard
# ---------------------------------------------------------------------------


class TestReadWindowGuard:
    def test_slow_collection_drops_snapshot(self):
        """If _collect_sources takes longer than the budget, tick() must
        return None and not publish."""
        bus = MagicMock()
        collector = _make_collector(include_ntp=False, bus=bus)
        collector.set_window_budget(0.0)  # zero budget: always fails

        # We need at least 2 sources to trigger the publish path, but with
        # zero budget the window guard fires first.  Patch _collect_sources
        # to return a synthetic 2-source list with an artificially large
        # window value.
        with patch.object(
            collector,
            "_collect_sources",
            return_value=(
                [
                    {"name": "system", "epoch_sec": 1000.0},
                    {"name": "ntp:fake", "epoch_sec": 1000.5},
                ],
                0.200,  # 200 ms window: always exceeds a zero budget
            ),
        ):
            result = collector.tick()

        assert result is None
        bus.publish.assert_not_called()
