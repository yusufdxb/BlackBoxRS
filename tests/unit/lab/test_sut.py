"""Reference arbitration models, one policy per test."""

from __future__ import annotations

import math

import pytest

from blackboxrs.lab.sut import (
    REASON_HOLD,
    REASON_MISSING,
    REASON_NO_INPUT,
    REASON_SILENT,
    REASON_SOURCE,
    REASON_STALE,
    ReferenceArbiter,
    build_config,
)

from .conftest import NS, hold, msg, twist

NAV, TELEOP = "/nav/cmd_vel", "/teleop/cmd_vel"


def arb(preset="helix_arbiter", **o):
    return ReferenceArbiter(build_config(preset, o))


def feed(a, e):
    a.on_event(e, e.t_ns)


def tick(a, t_s):
    return a.tick(int(round(t_s * NS)))


def test_missing_hold_state_forces_zero():
    a = arb()
    feed(a, msg(0.0, NAV, twist(0.3), seq=1))
    d = tick(a, 0.01)
    assert d.reason == REASON_MISSING and d.robot_cmd.is_zero()


def test_source_wins_when_released_and_fresh():
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=1))
    feed(a, msg(0.01, NAV, twist(0.3), seq=2))
    d = tick(a, 0.02)
    assert (d.reason, d.source, d.robot_cmd.vx) == (REASON_SOURCE, "nav", 0.3)
    assert d.cause == "r2"


def test_hold_dominates_every_priority():
    a = arb()
    feed(a, hold(0.0, True, 1, eseq=1))
    feed(a, msg(0.01, TELEOP, twist(0.9), seq=2))
    d = tick(a, 0.02)
    assert d.reason == REASON_HOLD and d.robot_cmd.is_zero()


def test_stale_hold_state_forces_zero_and_never_releases():
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=1))
    for k in range(1, 20):
        feed(a, msg(k * 0.05, NAV, twist(0.3), seq=k + 1))
    d = tick(a, 0.95)
    assert d.reason == REASON_STALE and d.robot_cmd.is_zero()


@pytest.mark.parametrize("bad", [math.nan, math.inf, "NaN", "fast", None, 5.0])
def test_invalid_input_rejected_and_older_value_discarded(bad):
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=1))
    feed(a, msg(0.01, NAV, twist(0.3), seq=2))
    assert tick(a, 0.02).robot_cmd.vx == 0.3
    feed(a, msg(0.03, NAV, twist(bad), seq=3))
    d = tick(a, 0.04)
    assert d.reason == REASON_NO_INPUT and d.robot_cmd.is_zero()
    assert d.cause == "r3"


def test_source_timeout_drops_it():
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=1))
    feed(a, msg(0.01, NAV, twist(0.3), seq=2))
    feed(a, hold(0.4, False, 3, eseq=2))
    assert tick(a, 0.50).robot_cmd.vx == 0.3
    d = tick(a, 0.52)
    assert d.reason == REASON_NO_INPUT and d.cause == "clock"


def test_resume_needs_a_new_command():
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=1))
    feed(a, msg(0.01, NAV, twist(0.3), seq=2))
    feed(a, hold(0.02, True, 3, eseq=2))
    tick(a, 0.03)
    feed(a, hold(0.04, False, 4, eseq=3))
    d = tick(a, 0.05)
    assert d.reason == REASON_NO_INPUT, "a pre-hold command must not resume motion (P7)"


def test_older_hold_state_is_dropped():
    a = arb()
    feed(a, hold(0.0, False, 1, eseq=5))
    feed(a, hold(0.01, True, 2, eseq=6))
    feed(a, hold(0.02, False, 3, eseq=4))   # re-delivered old RESUME
    assert tick(a, 0.03).reason == REASON_HOLD
    assert a.counters["hold_reordered"] == 1


def test_source_timestamp_freshness_is_fooled_by_skew():
    a = ReferenceArbiter(build_config("helix_arbiter", {"freshness_clock": "source_timestamp"}),
                         wall0_ns=0)
    feed(a, hold(0.0, False, 1, eseq=1))
    feed(a, hold(0.45, False, 3, eseq=2))
    feed(a, hold(0.9, False, 4, eseq=3))
    feed(a, msg(0.01, NAV, twist(0.3), seq=2, src_s=1.51))   # publisher clock 1.5 s ahead
    assert tick(a, 1.0).robot_cmd.vx == 0.3, "stamp-based age is negative: still 'fresh'"
    r = arb()
    feed(r, hold(0.0, False, 1, eseq=1))
    feed(r, hold(0.45, False, 3, eseq=2))
    feed(r, hold(0.9, False, 4, eseq=3))
    feed(r, msg(0.01, NAV, twist(0.3), seq=2, src_s=1.51))
    assert tick(r, 1.0).robot_cmd.is_zero(), "receipt-clock freshness is not"


def test_legacy_forwards_nan():
    a = arb("twist_mux_legacy")
    feed(a, msg(0.0, NAV, twist(math.nan), seq=1))
    d = tick(a, 0.01)
    assert d.robot_cmd is None and math.isnan(d.robot_raw[0]) and d.published


def test_legacy_goes_silent_and_sink_keeps_last_command():
    a = arb("twist_mux_legacy")
    feed(a, msg(0.0, NAV, twist(0.3), seq=1))
    assert tick(a, 0.01).robot_cmd.vx == 0.3
    d = tick(a, 0.6)
    assert d.reason == REASON_SILENT and not d.published and d.robot_cmd.vx == 0.3


def test_legacy_teleop_outranks_stop():
    a = arb("twist_mux_legacy")
    feed(a, hold(0.0, True, 1, eseq=1))
    feed(a, msg(0.01, TELEOP, twist(0.4), seq=2))
    d = tick(a, 0.02)
    assert d.source == "teleop" and d.robot_cmd.vx == 0.4


def test_legacy_stop_wins_over_lower_priority_nav():
    a = arb("twist_mux_legacy")
    feed(a, msg(0.0, NAV, twist(0.3), seq=1))
    feed(a, hold(0.01, True, 2, eseq=1))
    d = tick(a, 0.02)
    assert d.source == "helix_recovery" and d.robot_cmd.is_zero()


def test_config_validation():
    with pytest.raises(ValueError):
        build_config("nope")
    with pytest.raises(ValueError):
        build_config("helix_arbiter", {"nonfinite": "forward"})   # semantics are fixed
    with pytest.raises(ValueError):
        build_config("helix_arbiter", {"freshness_clock": "wall"})
    with pytest.raises(ValueError):
        build_config("helix_arbiter", {"sources": [{"name": "a", "topic": "/a", "priority": 1,
                                                    "timeout_s": 0}]})
