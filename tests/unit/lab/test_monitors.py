"""Safety invariants and detectors on hand-built streams (positive and negative)."""

from __future__ import annotations

import math

from blackboxrs.lab.monitors import ClockMonitor, CommandPath, OdometryConsistency, StopDominance
from blackboxrs.lab.sut import Command, Decision

from .conftest import NS, hold, msg, twist

NAV = "/nav/cmd_vel"


def dec(t_s, raw, cause="clock", source=""):
    raw = None if raw is None else tuple(raw)
    ok = raw is not None and all(isinstance(v, float) and math.isfinite(v) for v in raw)
    return Decision(int(round(t_s * NS)), "X", source, Command(*raw) if ok else None, raw,
                    True, None, cause)


def test_stop_dominance_passes_zero_and_fails_nonzero_after_grace():
    m = StopDominance({"/helix/hold"}, {NAV}, grace_s=0.05)
    m.on_event(hold(1.0, True, 1, eseq=1))
    assert m.on_decision(dec(1.02, (0.3, 0.0, 0.0))) == []      # inside grace
    assert m.on_decision(dec(1.10, (0.0, 0.0, 0.0))) == []
    f = m.on_decision(dec(1.12, (0.3, 0.0, 0.0), cause="r9"))
    assert [x.kind for x in f] == ["stop_violated"] and f[0].invariant == "stop_dominance"
    assert m.on_decision(dec(1.14, (0.3, 0.0, 0.0))) == []      # same episode
    assert m.inv.status() == "FAIL" and m.inv.violations == 2 and m.inv.episodes == 1


def test_stop_dominance_nan_output_while_held_is_a_violation():
    m = StopDominance({"/helix/hold"}, set(), grace_s=0.0)
    m.on_event(hold(0.0, True, 1, eseq=1))
    assert m.on_decision(dec(0.1, (math.nan, 0.0, 0.0)))


def test_stale_release_does_not_end_the_hold_for_the_oracle():
    m = StopDominance({"/helix/hold"}, set(), grace_s=0.0)
    m.on_event(hold(0.0, False, 1, eseq=10))
    m.on_event(hold(0.1, True, 2, eseq=11))
    m.on_event(hold(0.2, False, 3, eseq=9))                     # re-delivered old RESUME
    assert m.held and m.ignored_older == 1
    assert m.on_decision(dec(0.3, (0.2, 0.0, 0.0)))


def test_stop_dominance_not_exercised_without_a_hold():
    m = StopDominance({"/helix/hold"}, set(), grace_s=0.0)
    m.on_event(hold(0.0, False, 1, eseq=1))
    m.on_decision(dec(0.1, (0.2, 0.0, 0.0)))
    assert m.inv.status() == "NOT_EXERCISED"


def test_fresh_output_accepts_matching_fresh_source_and_zero():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    m.on_event(msg(0.0, NAV, twist(0.3), seq=1))
    assert m.on_decision(dec(0.4, (0.3, 0.0, 0.0))) == []
    late = m.on_decision(dec(2.0, (0.0, 0.0, 0.0)))
    assert [f.kind for f in late] == ["command_source_stale"]   # detected ...
    assert all(f.invariant is None for f in late)               # ... but zero never violates
    assert m.fresh.status() == "PASS"


def test_fresh_output_flags_stale_and_mismatched_commands():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    m.on_event(msg(0.0, NAV, twist(0.3), seq=1))
    f = m.on_decision(dec(0.6, (0.3, 0.0, 0.0)))
    kinds = sorted(x.kind for x in f)
    assert kinds == ["command_source_stale", "stale_command_forwarded"]
    m2 = CommandPath({NAV: 0.5}, grace_s=0.05)
    m2.on_event(msg(0.0, NAV, twist(0.3), seq=1))
    assert [x.kind for x in m2.on_decision(dec(0.1, (0.5, 0.0, 0.0)))] == [
        "stale_command_forwarded"], "an output no source asked for is not justified"


def test_invalid_latest_message_cannot_justify_an_older_value():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    m.on_event(msg(0.0, NAV, twist(0.3), seq=1))
    f = m.on_event(msg(0.1, NAV, twist(math.nan), seq=2))
    assert [x.kind for x in f] == ["nonfinite_input"]
    assert m.on_event(msg(0.12, NAV, twist("NaN"), seq=3)) == []   # one finding per run
    assert [x.kind for x in m.on_decision(dec(0.2, (0.3, 0.0, 0.0)))] == [
        "stale_command_forwarded"]


def test_source_that_stopped_on_zero_is_inactive_not_stale():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    m.on_event(msg(0.0, NAV, twist(0.0), seq=1))
    assert m.on_decision(dec(5.0, (0.0, 0.0, 0.0))) == []


def test_malformed_input_named_separately():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    assert [x.kind for x in m.on_event(msg(0.0, NAV, twist("fast"), seq=1))] == [
        "malformed_input"]


def test_finite_output_and_incomplete_without_outputs():
    m = CommandPath({NAV: 0.5}, grace_s=0.05)
    f = m.on_decision(dec(0.1, (math.inf, 0.0, 0.0)))
    assert [x.kind for x in f] == ["nonfinite_output"] and m.finite.status() == "FAIL"
    m2 = CommandPath({NAV: 0.5}, grace_s=0.05)
    m2.on_decision(dec(0.1, None))
    m2.finish(NS)
    assert m2.fresh.status() == "INCOMPLETE" and m2.finite.status() == "INCOMPLETE"


def test_clock_monitor_offset_is_info_and_step_is_warning():
    m = ClockMonitor({"/odom": "robot"}, step_threshold_s=0.2, offset_info_s=0.5)
    found = []
    for k in range(10):
        t = k * 0.05
        src = t + 1.7 + (2.0 if k >= 7 else 0.0)                # constant skew, then a step
        found += m.on_event(msg(t, "/odom", {}, seq=k + 1, src_s=src))
    assert [f.kind for f in found] == ["stamp_ahead"]
    fin = m.finish(NS)
    assert [(f.kind, f.severity) for f in fin] == [("clock_offset", "info")]


def test_clock_monitor_ignores_jitter():
    m = ClockMonitor({"/odom": "robot"}, step_threshold_s=0.2, offset_info_s=0.5)
    out = []
    for k in range(40):
        out += m.on_event(msg(k * 0.05, "/odom", {}, seq=k + 1,
                              src_s=k * 0.05 - (0.003 if k % 3 else 0.0)))
    assert out == [] and m.finish(NS) == []


def odom(t, x, v, seq):
    return msg(t, "/odom", {"pose": {"pose": {"position": {"x": x, "y": 0.0}}},
                            "twist": {"twist": {"linear": {"x": v, "y": 0.0}}}}, seq=seq)


def test_odometry_consistent_motion_is_quiet():
    m = OdometryConsistency({"/odom"})
    out = []
    for k in range(40):
        out += m.on_event(odom(k * 0.05, 0.15 * k * 0.05, 0.15, k + 1))
    assert out == []


def test_odometry_frozen_and_jump():
    m = OdometryConsistency({"/odom"}, frozen_samples=5)
    out = []
    for k in range(30):
        x = 0.15 * min(k, 5) * 0.05 if k < 25 else 0.15 * k * 0.05   # frozen 1 s
        out += m.on_event(odom(k * 0.05, x, 0.15, k + 1))
    assert [f.kind for f in out] == ["odometry_frozen", "odometry_jump"]
