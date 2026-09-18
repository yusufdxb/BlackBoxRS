"""End-to-end fixtures: synthetic traffic -> core -> bundle -> report.

Every scenario is GENERATED traffic; these tests exercise the pipeline, not
hardware. Each checks that the report names the injected problem and does
not invent evidence for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackboxrs.flight import synthetic
from blackboxrs.flight.analysis import CHAIN

from .conftest import FIXED_SESSION


def run(go2, tmp_path, name, **over):
    _, bundles = synthetic.run(go2, synthetic.scenario(name, **over), tmp_path / "out",
                               session=dict(FIXED_SESSION))
    assert len(bundles) == 1, bundles
    b = Path(bundles[0])
    return b, json.loads((b / "report.json").read_text())


def verdicts(rep):
    return {v["id"]: v["result"] for v in rep["verdicts"]}


def test_stopmove_full_chain(go2, tmp_path):
    b, rep = run(go2, tmp_path, "stopmove")
    assert rep["synthetic"] is True
    assert all(rep["chain"][k]["status"] == "observed" for k in CHAIN)
    assert set(verdicts(rep).values()) == {"PASS"}
    od = rep["motion"]["odometry"]
    assert od["status"] == "stopped" and 0.3 < od["stop_latency_s"] < 0.6
    assert od["stop_distance_m"] is not None
    assert rep["motion"]["stopmove_request_id"] is not None
    assert rep["motion"]["sport_response"]["code"] == 0
    assert rep["window"]["seconds_before_trigger"] >= 9.9
    assert (b / "report.md").read_text().startswith("# BlackBoxRS flight incident")
    assert "SYNTHETIC DATA" in (b / "report.md").read_text()


def test_emission_intervals_use_one_clock_and_receipt_is_labelled(go2, tmp_path):
    _, rep = run(go2, tmp_path, "stopmove")
    spans = {(s["from"], s["to"]): s for s in rep["key_spans"]}
    assert spans[("fault", "helix_hold")]["basis"] == "emission"
    # response comes from the robot: never compared on emission clocks
    s = spans[("sport_stopmove_request", "sport_response")]
    assert s["basis"] == "receipt" and "not a causal" in s["caveat"]
    stop = spans[("helix_hold", "odometry_stopped")]
    # hold (payload clock) and odometry (robot clock) are placed on one clock
    # through median receipt offsets; the receipt interval is kept alongside
    assert stop["basis"] == "emission (offset-mapped)" and stop["uncertainty_s"] > 0
    assert stop["receipt_value_s"] is not None


def test_receipt_order_differs_from_causal_order_and_chain_survives(go2, tmp_path):
    # queue jitter makes the recorder see the hold before the action; the chain
    # must still be built from emission stamps, not receipt order
    _, rep = run(go2, tmp_path, "stopmove", queue_jitter_s=0.02)
    assert all(rep["chain"][k]["status"] == "observed" for k in CHAIN)
    iv = [x for x in rep["stage_intervals"] if x["basis"] == "emission"]
    assert iv and all(x["value_s"] >= 0 for x in iv)


def test_stale_odometry_never_claims_a_stop(go2, tmp_path):
    _, rep = run(go2, tmp_path, "stale_odometry")
    assert rep["chain"]["odometry_stopped"]["status"] == "not_observed"
    assert verdicts(rep)["stopped_within_deadline"] == "INCOMPLETE"
    assert rep["motion"]["odometry"].get("stop_latency_s") is None
    assert rep["motion"]["odometry"].get("stop_distance_m") is None


def test_missing_sport_response(go2, tmp_path):
    _, rep = run(go2, tmp_path, "missing_sport_response")
    assert rep["chain"]["sport_stopmove_request"]["status"] == "observed"
    assert rep["chain"]["sport_response"]["status"] == "not_observed"
    assert verdicts(rep)["stopmove_acknowledged"] == "FAIL"


def test_node_death(go2, tmp_path):
    _, rep = run(go2, tmp_path, "node_death")
    assert rep["trigger"]["type"] == "node_disappeared"
    assert rep["trigger"]["node"] == "/helix_go2_sport_sink"
    assert "/helix_go2_sport_sink" in rep["nodes"]["disappeared"]
    assert rep["verdicts"] == []  # no HELIX stop in bundle: no stop criteria apply
    assert all(rep["chain"][k]["status"] != "observed" for k in CHAIN)


def test_dropped_messages_counted_from_sequence(go2, tmp_path):
    _, rep = run(go2, tmp_path, "dropped_messages")
    lost = rep["data_quality"]["publisher_sequence_lost"]
    assert lost.get("/helix/hold", 0) > 0 and lost.get("/helix/arbiter/status", 0) > 0


def test_duplicates_detected(go2, tmp_path):
    _, rep = run(go2, tmp_path, "duplicated_messages")
    d = rep["data_quality"]["duplicates"]
    assert d.get("/utlidar/robot_odom", 0) > 0
    assert rep["topics"]["/helix/hold"]["sequence"]["duplicates"] > 0


def test_out_of_order_stamps(go2, tmp_path):
    _, rep = run(go2, tmp_path, "out_of_order")
    assert rep["data_quality"]["out_of_order_stamps"]["/utlidar/robot_odom"] > 0
    # non-monotonic robot stamps: speed falls back to receipt times, and says so
    assert rep["motion"]["odometry"]["speed_time_base"] == "recorder receipt times"


def test_clock_jump(go2, tmp_path):
    _, rep = run(go2, tmp_path, "clock_jump")
    q = rep["data_quality"]
    assert q["clock_jumps"] and abs(q["clock_jumps"][0]["wall_step_s"] - 2.0) < 1e-6
    assert any("jump" in w for w in q["clock_warnings"])
    # the stop latency is on the monotonic clock and is unaffected
    assert 0.3 < rep["motion"]["odometry"]["stop_latency_s"] < 0.6


def test_high_rate(go2, tmp_path):
    _, rep = run(go2, tmp_path, "high_rate")
    t = rep["topics"]
    assert t["/lowstate"]["rate_hz"]["before"] == pytest.approx(500, rel=0.02)
    assert t["/lowstate"]["stored"] < t["/lowstate"]["count"]  # still all counted
    assert rep["data_quality"]["recorder_ring_evicted_in_window"] == 0


def test_interrupted(go2, tmp_path):
    _, rep = run(go2, tmp_path, "interrupted")
    assert rep["status"] == "interrupted"
    assert rep["secondary_triggers"][-1]["type"] == "recorder_shutdown"
    assert rep["window"]["seconds_after_trigger"] < 15.0


def test_not_moving_does_not_pass_the_stop(go2, tmp_path):
    _, rep = run(go2, tmp_path, "not_moving")
    v = verdicts(rep)
    assert v["moving_at_fault"] == "FAIL"
    assert v["stopped_within_deadline"] == "INCOMPLETE"


def test_robot_that_ignores_stop_fails(go2, tmp_path):
    _, rep = run(go2, tmp_path, "robot_ignores_stop")
    assert rep["motion"]["odometry"]["status"] == "not_stopped"
    assert verdicts(rep)["stopped_within_deadline"] == "FAIL"


def test_normal_motion_marker_bundle_has_no_verdicts(go2, tmp_path):
    _, rep = run(go2, tmp_path, "normal_motion")
    assert rep["trigger"]["type"] == "manual_marker"
    assert rep["verdicts"] == []
    assert rep["motion"]["odometry"]["status"] == "not_applicable"


def test_absent_topics_are_unavailable_not_fake(go2, tmp_path):
    _, rep = run(go2, tmp_path, "stopmove")
    t = rep["topics"]["/phoenix/estop"]
    assert t["availability"] == "absent" and t["count"] == 0
    assert "rate_hz" not in t


def test_helix_profile_reconstructs_cmd_vel_without_subscribing(tmp_path):
    from blackboxrs.flight import load_profile
    prof = load_profile("go2_helix", evidence_dir=str(tmp_path / "ev"))
    assert prof.topic("/cmd_vel") is None
    _, bundles = synthetic.run(prof, synthetic.scenario("stopmove"), tmp_path / "out",
                               session=dict(FIXED_SESSION))
    rep = json.loads((Path(bundles[0]) / "report.json").read_text())
    st = rep["chain"]["cmd_vel_zero"]
    assert st["status"] == "observed"
    assert st["evidence"]["topic"] == "/helix/sink/trace"
    assert "sink trace" in st["detail"]["source"]
    assert rep["motion"]["outputs_source"].startswith("ArbiterStatus")
    assert rep["motion"]["nonzero_outputs_while_held"] == 0
    assert {v["id"]: v["result"] for v in rep["verdicts"]}["full_chain"] == "PASS"
    assert "/cmd_vel" not in rep["topics"]


def _true_stop_latency(rep, robot_offset=1.734):
    """Synthetic ground truth: emission-to-emission, both on the payload clock."""
    hold = rep["chain"]["helix_hold"]["evidence"]
    stop = rep["chain"]["odometry_stopped"]["evidence"]
    return (stop["pub_stamp_s"] - robot_offset) - hold["dds_src_ns"] / 1e9


def test_recorder_stall_at_the_hold_does_not_bias_stop_latency(go2, tmp_path):
    """Regression: a recorder stall delays receipt of the hold itself. The
    receipt interval then undercounts the stop; the offset-mapped value must
    not, and the receipt bound must still contain the truth."""
    _, rep = run(go2, tmp_path, "recorder_stall_at_hold")
    od = rep["motion"]["odometry"]
    truth = _true_stop_latency(rep)
    assert od["hold_receipt_excess_delay_s"] > 0.01  # the stall delayed the hold itself
    assert abs(od["stop_latency_s"] - truth) <= od["stop_latency_uncertainty_s"] + 0.002
    assert abs(od["stop_latency_s"] - truth) < 0.01
    assert abs(od["stop_latency_receipt_s"] - truth) <= od["stop_latency_receipt_uncertainty_s"]


def test_stop_latency_matches_truth_without_stall(go2, tmp_path):
    _, rep = run(go2, tmp_path, "stopmove")
    od = rep["motion"]["odometry"]
    assert abs(od["stop_latency_s"] - _true_stop_latency(rep)) < 0.01
