"""Every fault injector on its own, plus parsing and composition."""

from __future__ import annotations

import math

import pytest

from blackboxrs.lab.evidence import TopicInfo
from blackboxrs.lab.faults import FaultError, apply_faults, parse_cli_fault, parse_fault

from .conftest import NS, msg, twist

A, B, R = "/a", "/b", "/r"


def stream():
    """/a and /b at 10 Hz for 1 s (payload host), /r at 10 Hz (robot host)."""
    evs = []
    seq = 0
    for k in range(10):
        for topic in (A, B, R):
            seq += 1
            evs.append(msg(k * 0.1 + seq * 1e-6, topic,
                           {"linear": {"x": 0.1 * k, "y": 0.0, "z": 0.0},
                            "angular": {"x": 0.0, "y": 0.0, "z": 0.0}, "seq": k},
                           seq=seq, src_s=k * 0.1, rx_s=k * 0.1 + 0.001))
    return sorted(evs, key=lambda e: e.key)


TOPICS = {A: TopicInfo(A, "cmd_vel_source", "payload", "event", None),
          B: TopicInfo(B, "other", "payload", "event", None),
          R: TopicInfo(R, "odometry", "robot", "periodic", 0.5)}


def run(*faults):
    return apply_faults(stream(), [parse_fault(f, i) for i, f in enumerate(faults)],
                        dict(TOPICS))


def on(evs, topic):
    return [e for e in evs if e.topic == topic]


def test_drop_all_and_every_n():
    evs, log = run({"kind": "drop", "topic": A, "from_s": 0.5})
    assert len(on(evs, A)) == 5 and log[0]["events_affected"] == 5
    evs, _ = run({"kind": "drop", "topic": A, "every_n": 2})
    assert len(on(evs, A)) == 5 and len(on(evs, B)) == 10


def test_gap_needs_an_end_and_selects_by_host():
    with pytest.raises(FaultError):
        run({"kind": "gap", "host": "robot", "from_s": 0.2})
    evs, log = run({"kind": "gap", "host": "payload", "from_s": 0.2, "to_s": 0.4})
    assert len(on(evs, A)) == 8 and len(on(evs, B)) == 8 and len(on(evs, R)) == 10
    assert log[0]["topics_affected"] == [A, B]


def test_delay_shifts_receipt_not_source_time():
    base = on(stream(), A)[3]
    evs, _ = run({"kind": "delay", "topic": A, "from_s": 0.3, "to_s": 0.35, "delay_s": 0.25})
    moved = next(e for e in evs if e.eid == base.eid)
    assert moved.t_ns == base.t_ns + int(0.25 * NS) and moved.src_ns == base.src_ns
    assert moved.rx_wall_ns == base.rx_wall_ns + int(0.25 * NS) and moved.injected == ("F1",)
    assert [e.key for e in evs] == sorted(e.key for e in evs)


def test_duplicate_copies_payload_and_source_time():
    evs, _ = run({"kind": "duplicate", "topic": A, "from_s": 0.0, "to_s": 0.25})
    dups = [e for e in evs if ".dup" in e.eid]
    assert len(dups) == 3
    for d in dups:
        orig = next(e for e in evs if e.eid == d.eid.split(".")[0])
        assert (d.data, d.src_ns) == (orig.data, orig.src_ns) and d.t_ns > orig.t_ns


def test_reorder_swaps_pairs():
    evs, _ = run({"kind": "reorder", "topic": A, "from_s": 0.0, "to_s": 0.4})
    xs = [e.data["seq"] for e in on(evs, A)]
    assert xs[:4] == [1, 0, 3, 2] and xs[4:] == [4, 5, 6, 7, 8, 9]


def test_stale_redelivery_repeats_an_old_message_late():
    evs, _ = run({"kind": "stale_redelivery", "topic": A, "at_s": 0.9, "age_s": 0.5})
    late = [e for e in evs if ".stale" in e.eid]
    assert len(late) == 1 and late[0].t_ns == int(0.9 * NS) and late[0].data["seq"] == 3


def test_clock_skew_and_timestamp_jump():
    base = {e.eid: e for e in stream()}
    evs, _ = run({"kind": "clock_skew", "host": "robot", "offset_s": 1.5})
    for e in on(evs, R):
        assert e.src_ns == base[e.eid].src_ns + int(1.5 * NS)
    assert all(e.src_ns == base[e.eid].src_ns for e in on(evs, A))
    evs, _ = run({"kind": "timestamp_jump", "topic": A, "at_s": 0.5, "jump_s": -2.0})
    shifted = [e.eid for e in on(evs, A) if e.src_ns != base[e.eid].src_ns]
    assert len(shifted) == 5


@pytest.mark.parametrize("kind,check", [
    ("nan", lambda v: isinstance(v, float) and math.isnan(v)),
    ("inf", lambda v: v == math.inf),
    ("malformed", lambda v: v == "not-a-number"),
])
def test_data_faults(kind, check):
    evs, _ = run({"kind": kind, "topic": A, "field": "linear.x", "from_s": 0.5, "to_s": 0.7})
    vals = [e.data["linear"]["x"] for e in on(evs, A)]
    assert [check(v) for v in vals] == [False] * 5 + [True] * 2 + [False] * 3


def test_malformed_remove_and_missing_field_is_an_error():
    evs, _ = run({"kind": "malformed", "topic": A, "field": "linear.x", "remove": True,
                  "from_s": 0.5, "to_s": 0.6})
    assert "x" not in on(evs, A)[5].data["linear"]
    with pytest.raises(FaultError, match="not in"):
        run({"kind": "nan", "topic": A, "field": "linear.q"})


def test_freeze_and_step():
    evs, _ = run({"kind": "freeze", "topic": A, "fields": ["linear.x"], "from_s": 0.3,
                  "to_s": 0.6})
    assert [round(e.data["linear"]["x"], 3) for e in on(evs, A)][2:7] == [0.2] * 4 + [0.6]
    evs, _ = run({"kind": "step", "topic": A, "field": "linear.x", "at_s": 0.8, "delta": 5.0})
    assert [round(e.data["linear"]["x"], 3) for e in on(evs, A)][7:] == [0.7, 5.8, 5.9]


def test_inject_stream_adds_tagged_events_after_evidence_at_ties():
    evs, log = run({"kind": "inject_stream", "topic": "/teleop", "role": "cmd_vel_source",
                    "data": twist(0.4), "final_data": twist(0.0), "from_s": 0.2, "to_s": 0.5,
                    "rate_hz": 10})
    new = on(evs, "/teleop")
    assert [e.data["linear"]["x"] for e in new] == [0.4, 0.4, 0.4, 0.0]
    assert all(e.order[0] == 1 and e.injected == ("F1",) for e in new)
    assert log[0]["events_affected"] == 4


def test_faults_compose_in_order():
    evs, log = run({"kind": "drop", "topic": A, "from_s": 0.5},
                   {"kind": "stale_redelivery", "topic": A, "at_s": 0.9, "age_s": 0.1})
    late = [e for e in evs if ".stale" in e.eid]
    assert late[0].data["seq"] == 4, "sees the stream after the drop"
    assert [f["id"] for f in log] == ["F1", "F2"]


def test_errors_are_loud():
    with pytest.raises(FaultError, match="unknown kind"):
        parse_fault({"kind": "explode"}, 0)
    with pytest.raises(FaultError, match="unknown parameter"):
        parse_fault({"kind": "drop", "topic": A, "seed": 3}, 0)
    with pytest.raises(FaultError, match="missing parameter"):
        parse_fault({"kind": "delay", "topic": A}, 0)
    with pytest.raises(FaultError, match="matched no event"):
        run({"kind": "drop", "topic": A, "from_s": 5.0})
    with pytest.raises(FaultError, match="not in the evidence"):
        run({"kind": "drop", "topic": "/nope"})
    with pytest.raises(FaultError, match="exactly one"):
        run({"kind": "drop", "topic": A, "host": "robot"})
    with pytest.raises(FaultError, match="duplicate fault ids"):
        apply_faults(stream(), [parse_fault({"kind": "drop", "topic": A, "id": "X"}, 0),
                                parse_fault({"kind": "drop", "topic": B, "id": "X"}, 1)],
                     dict(TOPICS))


def test_cli_fault_syntax():
    f = parse_cli_fault("nan:topic=/nav/cmd_vel,field=linear.x,from_s=3,to_s=3.5", 0)
    assert (f.kind, f.params["from_s"], f.params["to_s"]) == ("nan", 3.0, 3.5)
    g = parse_cli_fault('{"kind": "gap", "topics": ["/a", "/b"], "from_s": 1, "to_s": 2}', 1)
    assert g.params["topics"] == ["/a", "/b"] and g.id == "F2"
    h = parse_cli_fault("gap:topics=/a|/b,from_s=1,to_s=2", 0)
    assert h.params["topics"] == ["/a", "/b"]
