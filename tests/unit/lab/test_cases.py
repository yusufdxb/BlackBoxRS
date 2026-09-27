"""Golden regression cases: each historical failure class as a permanent test.

Every case in examples/replay_lab/cases/ must produce exactly its expected
verdict, invariant statuses and detection set (extra detections fail too).
The targeted tests below pin the causal facts behind the verdicts, so a case
cannot pass for the wrong reason.
"""

from __future__ import annotations

import pytest

from blackboxrs.lab import load_evidence, replay
from blackboxrs.lab.case import load_case
from blackboxrs.lab.verdict import check_expectations

from .conftest import CASES, LAB, NS

_cache: dict[str, dict] = {}


def run(name: str) -> dict:
    if name not in _cache:
        c = load_case(LAB / "cases" / f"{name}.json")
        _cache[name] = replay(load_evidence(c.evidence), c.config)
    return _cache[name]


def inv(res, name):
    return res["invariants"][name]


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_matches_expectation(path):
    c = load_case(path)
    assert c.expect.get("verdict"), "every golden case states its expected verdict"
    for key in ("regression", "initial_conditions", "injected_fault"):
        assert c.raw.get(key), f"{path.name}: missing {key}"
    res = run(c.name)
    assert check_expectations(res, c.expect) == []


def test_there_is_a_fixture_per_failure_class():
    names = {p.stem for p in CASES}
    for need in ("nominal_motion", "clean_stop", "stale_command__twist_mux_legacy",
                 "nan_command__twist_mux_legacy", "teleop_vs_stop__twist_mux_legacy",
                 "stop_loses_arbitration__twist_mux_legacy", "clock_skew__stamp_freshness",
                 "transport_loss__robot_host", "telemetry_dropout__odometry",
                 "legitimate_inactivity__teleop", "contradictory_state__observed",
                 "publisher_crash__odometry", "robot_host_down"):
        assert need in names


def test_nominal_has_no_incident():
    res = run("nominal_motion")
    assert res["verdict"]["result"] == "PASS"
    assert [f for f in res["findings"] if f["severity"] != "info"] == []


def test_stale_command_legacy_keeps_moving_after_the_window():
    res = run("stale_command__twist_mux_legacy")
    t = inv(res, "fresh_output")["first_violation_t_ns"] / NS
    assert 4.5 < t < 4.62, "violation starts one window (+grace) after the last message"
    safe = run("stale_command__helix_arbiter")
    last = [e for e in safe["timeline"] if e["layer"] == "output"][-1]
    # last /nav/cmd_vel receipt is ~3.953 s; its 0.5 s window ends ~4.453 s, next tick 4.46
    assert "(0.000, 0.000, 0.000)" in last["text"] and 4.45 < last["t_s"] <= 4.48


def test_nan_is_rejected_by_intended_arbiter_and_forwarded_by_legacy():
    bad = run("nan_command__twist_mux_legacy")
    v = next(f for f in bad["findings"] if f["kind"] == "nonfinite_output")
    assert v["data"]["robot_command"][0] == "NaN" and 3.0 <= v["t_s"] < 3.1
    good = run("nan_command__helix_arbiter")
    assert inv(good, "finite_output")["checks"] > 250


def test_teleop_beats_stop_only_on_the_legacy_path():
    bad = run("teleop_vs_stop__twist_mux_legacy")
    v = next(f for f in bad["findings"] if f["kind"] == "stop_violated")
    assert v["data"]["winner"] == "teleop" and v["data"]["robot_command"][0] == 0.4
    assert abs(v["t_s"] - 3.5) < 0.021
    good = run("teleop_vs_stop__helix_arbiter")
    assert inv(good, "stop_dominance")["checks"] > 100 and inv(good, "stop_dominance")[
        "violating_ticks"] == 0


def test_stop_loses_arbitration_when_its_input_goes_quiet():
    res = run("stop_loses_arbitration__twist_mux_legacy")
    v = next(f for f in res["findings"] if f["kind"] == "stop_violated")
    assert v["data"]["winner"] == "nav" and 3.9 < v["t_s"] < 4.0
    good = run("stop_loses_arbitration__helix_arbiter")
    reasons = [e["text"] for e in good["timeline"] if e["layer"] == "decision"]
    assert any("HELIX_STATE_STALE" in r for r in reasons)


def test_clock_skew_misleads_only_stamp_based_freshness():
    bad = run("clock_skew__stamp_freshness")
    t = inv(bad, "fresh_output")["first_violation_t_ns"] / NS
    assert 4.5 < t < 4.62
    off = next(f for f in bad["findings"] if f["kind"] == "clock_offset"
               and f["subject"] == "payload")
    assert abs(off["data"]["offset_s"] - 1.5) < 0.01
    good = run("clock_skew__helix_arbiter")
    assert inv(good, "fresh_output")["status"] == "PASS"


def test_dropout_classes_are_distinguished():
    one = run("telemetry_dropout__odometry")
    assert [f["subject"] for f in one["findings"] if f["kind"] == "stale_telemetry"] == [
        "/utlidar/robot_odom"]
    host = run("transport_loss__robot_host")
    tl = [f for f in host["findings"] if f["kind"] == "transport_loss"]
    assert len(tl) == 1 and tl[0]["subject"] == "robot"
    assert tl[0]["data"]["graph"] == "publishers_still_advertised"
    assert tl[0]["data"]["recovered_t_ns"] is not None
    idle = run("legitimate_inactivity__teleop")
    assert idle["liveness"]["/teleop/cmd_vel"]["liveness"] == "event"
    assert idle["liveness"]["/teleop/cmd_vel"]["silent_at_end_s"] > 4.0


def test_every_fault_is_visible_in_the_result():
    for p in CASES:
        c = load_case(p)
        res = run(c.name)
        assert len(res["injections"]) == len(c.config.faults)
        for f in res["injections"]:
            assert f["events_affected"] > 0
            assert any(e["layer"] == "fault" and e.get("fault") == f["id"]
                       for e in res["timeline"]), f"{c.name}: {f['id']} not on the timeline"


def test_violations_link_back_to_the_injected_fault():
    res = run("teleop_vs_stop__twist_mux_legacy")
    by_id = {e["id"]: e for e in res["timeline"]}
    v = next(e for e in res["timeline"] if e["layer"] == "invariant")
    seen, todo = set(), list(v["caused_by"])
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo += by_id[x]["caused_by"]
    assert any(by_id[x]["layer"] == "fault" for x in seen)


def test_stop_oracle_agrees_with_the_flight_report():
    """stop_dominance and the flight report's nonzero-while-held count must agree."""
    from blackboxrs.flight.analysis import analyze
    from blackboxrs.lab.engine import ReplayConfig
    from blackboxrs.lab.events import normalize, to_record
    from blackboxrs.lab.evidence import topic_table
    from blackboxrs.lab.faults import apply_faults, parse_fault

    ev = load_evidence(LAB / "evidence" / "clean_stop")
    for bump, expect_fail in ((None, False), (0.2, True)):
        faults = [] if bump is None else [parse_fault(
            {"kind": "set_value", "topic": "/cmd_vel", "field": "linear.x", "value": bump,
             "from_s": 3.5, "to_s": 3.7}, 0)]
        res = replay(ev, ReplayConfig(sut_mode="observed", faults=tuple(faults)))
        events, _ = normalize(ev)
        events, _ = apply_faults(events, faults, topic_table(ev))
        recs = [to_record(e, ev.t0_mono_ns, i + 1) for i, e in enumerate(events)]
        flight = analyze(ev.manifest, recs)["motion"]["nonzero_outputs_while_held"]
        assert (inv(res, "stop_dominance")["status"] == "FAIL") is expect_fail
        assert (flight > 0) is expect_fail, flight


def test_detection_thresholds_are_not_hair_triggers():
    """Below-threshold faults are not reported (the documented sensitivity bounds)."""
    from blackboxrs.lab.engine import ReplayConfig
    from blackboxrs.lab.faults import parse_fault

    ev = load_evidence(LAB / "evidence" / "nominal_motion")
    small = [({"kind": "delay", "topic": "/nav/cmd_vel", "from_s": 3.0, "to_s": 3.5,
               "delay_s": 0.1}, "stamp_behind"),
             ({"kind": "freeze", "topic": "/utlidar/robot_odom",
               "fields": ["pose.pose.position.x"], "from_s": 3.0, "to_s": 3.15},
              "odometry_frozen"),
             ({"kind": "gap", "topic": "/utlidar/robot_odom", "from_s": 3.0, "to_s": 3.3},
              "stale_telemetry")]
    for f, kind in small:
        res = replay(ev, ReplayConfig(faults=(parse_fault(f, 0),)))
        assert kind not in res["verdict"]["detections"], f


def test_replay_that_never_exercises_the_model_is_incomplete():
    from blackboxrs.lab.engine import ReplayConfig
    ev = load_evidence(LAB / "evidence" / "nominal_motion")
    no_hold = replay(ev, ReplayConfig(overrides={"hold_topic": "/elsewhere/hold"}))
    assert no_hold["verdict"]["result"] == "INCOMPLETE"
    other = {"sources": [{"name": "x", "topic": "/x/cmd_vel", "priority": 1, "timeout_s": 0.5}]}
    assert replay(ev, ReplayConfig(overrides=other))["verdict"]["result"] == "INCOMPLETE"
    windowed = replay(ev, ReplayConfig(from_s=1.0))
    assert any("not replayed" in r for r in windowed["verdict"]["reasons"])


# --- regressions from the adversarial review: each of these used to PASS -------------

def _run(evidence, **kw):
    from blackboxrs.lab.engine import ReplayConfig
    from blackboxrs.lab.faults import parse_fault
    faults = tuple(parse_fault(f, i) for i, f in enumerate(kw.pop("faults", [])))
    return replay(load_evidence(LAB / "evidence" / evidence), ReplayConfig(faults=faults, **kw))


@pytest.mark.parametrize("preset", ["helix_arbiter", "twist_mux_legacy"])
def test_a_fault_on_the_stop_signal_cannot_erase_a_recorded_stop(preset):
    flipped = _run("clean_stop", preset=preset, faults=[
        {"kind": "set_value", "topic": "/helix/hold", "field": "hold", "value": False,
         "from_s": 2.9}])
    assert flipped["verdict"]["result"] == "FAIL"
    v = next(f for f in flipped["findings"] if f["kind"] == "stop_violated")
    assert v["data"]["hold_stream"] == "recorded"


def test_dropping_every_hold_message_is_not_a_pass():
    res = _run("clean_stop", preset="twist_mux_legacy",
               faults=[{"kind": "drop", "topic": "/helix/hold"}])
    assert res["verdict"]["result"] in ("FAIL", "INCOMPLETE")


def test_lower_epoch_does_not_hide_a_hold_from_the_oracle():
    res = _run("clean_stop", sut_mode="observed", faults=[
        {"kind": "set_value", "topic": "/helix/hold", "field": "epoch", "value": 7,
         "from_s": 3.0},
        {"kind": "set_value", "topic": "/cmd_vel", "field": "linear.x", "value": 0.15,
         "from_s": 3.5}])
    assert inv(res, "stop_dominance")["status"] == "FAIL"


def test_a_violation_between_two_ticks_is_caught():
    burst = {"kind": "inject_stream", "topic": "/teleop/cmd_vel", "rate_hz": 1000,
             "from_s": 4.004, "to_s": 4.008,
             "data": {"linear": {"x": 0.4, "y": 0.0, "z": 0.0},
                      "angular": {"x": 0.0, "y": 0.0, "z": 0.0}},
             "final_data": {"linear": {"x": 0.0, "y": 0.0, "z": 0.0},
                            "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}}
    res = _run("clean_stop", preset="twist_mux_legacy", faults=[burst])
    assert inv(res, "stop_dominance")["status"] == "FAIL"
    assert res["replay"]["publications_between_ticks"] > 0


@pytest.mark.parametrize("key", ["stop_grace_s", "fresh_grace_s"])
def test_grace_is_bounded(key):
    with pytest.raises(ValueError, match=key):
        _run("clean_stop", **{key: 1000.0})
    with pytest.raises(ValueError):
        _run("clean_stop", from_s="1")


def test_window_that_excludes_a_recorded_hold_is_incomplete():
    res = _run("clean_stop", to_s=2.9)
    assert inv(res, "stop_dominance")["status"] == "INCOMPLETE"
    assert res["verdict"]["result"] == "INCOMPLETE"


def test_hold_asserted_but_never_checked_is_not_exercised_pass():
    # The hold asserts at 3.016 s; the window ends before any tick after the grace.
    res = _run("clean_stop", to_s=3.03)
    assert inv(res, "stop_dominance")["status"] == "INCOMPLETE"
    assert inv(res, "stop_dominance")["checks"] == 0


def test_publisher_lost_and_host_down_graph_evidence():
    crash = run("publisher_crash__odometry")
    lost = [f for f in crash["findings"] if f["kind"] == "publisher_lost"]
    assert [f["subject"] for f in lost] == ["/utlidar/robot_odom"]
    down = run("robot_host_down")
    tl = next(f for f in down["findings"] if f["kind"] == "transport_loss")
    assert tl["data"]["graph"] == "publishers_gone"
