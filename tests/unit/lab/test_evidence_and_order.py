"""Evidence validation, event ordering, and the replay clock."""

from __future__ import annotations

import json
import shutil

import pytest

from blackboxrs.lab.clock import ClockError, RealtimePacer, ReplayClock
from blackboxrs.lab.events import check_total_order, normalize
from blackboxrs.lab.evidence import EvidenceError, load_evidence, topic_table, validate_records

from .conftest import LAB, msg


def copy_bundle(tmp_path, name="nominal_motion"):
    dst = tmp_path / name
    shutil.copytree(LAB / "evidence" / name, dst)
    return dst


def rewrite(bundle, fn):
    lines = (bundle / "records.jsonl").read_text().splitlines()
    (bundle / "records.jsonl").write_text("\n".join(fn(lines)) + "\n")


def test_torn_bundle_is_refused_unless_allowed(tmp_path):
    b = copy_bundle(tmp_path)
    with open(b / "records.jsonl", "a") as fh:
        fh.write('{"kind": "msg", "topic": "/nav')
    with pytest.raises(EvidenceError, match="torn"):
        load_evidence(b)
    ev = load_evidence(b, allow_partial=True)
    assert ev.partial and ev.read_info["problems"]


def test_missing_manifest_is_refused(tmp_path):
    b = copy_bundle(tmp_path)
    (b / "manifest.json").unlink()
    with pytest.raises(EvidenceError, match="manifest"):
        load_evidence(b)


def test_duplicate_seq_and_missing_fields_are_refused(tmp_path):
    b = copy_bundle(tmp_path)
    rewrite(b, lambda ls: ls + [ls[-1]])
    with pytest.raises(EvidenceError, match="duplicate seq"):
        load_evidence(b)
    with pytest.raises(EvidenceError, match="without 'topic'"):
        validate_records([{"kind": "msg", "seq": 1, "t_mono_ns": 1, "t_wall_ns": 1,
                           "role": "x", "type": "a/b/C"}])
    with pytest.raises(EvidenceError, match="t_mono_ns"):
        validate_records([{"kind": "msg", "seq": 1, "topic": "/a", "role": "x",
                           "type": "a/b/C", "t_wall_ns": 1}])


def test_equal_receipt_times_are_ordered_by_recorder_sequence(tmp_path):
    b = copy_bundle(tmp_path)

    def same_time(lines):
        recs = [json.loads(x) for x in lines]
        msgs = [r for r in recs if r["kind"] == "msg"][:6]
        for r in msgs:
            r["t_mono_ns"] = msgs[0]["t_mono_ns"]
        # shuffle file order: ordering must come from seq, not position
        return [json.dumps(r) for r in reversed(recs)]
    rewrite(b, same_time)
    events, _ = normalize(load_evidence(b))
    t_tie = min(e.t_ns for e in events if e.kind == "msg")
    tied = [e for e in events if e.t_ns == t_tie and e.kind == "msg"]
    assert len(tied) >= 6
    assert [e.order[1] for e in tied] == sorted(e.order[1] for e in tied)


def test_total_order_check_refuses_ambiguous_keys():
    a = msg(1.0, "/a", {}, seq=1)
    with pytest.raises(EvidenceError):
        check_total_order([a, a])


def test_topic_overrides_are_validated(nominal):
    t = topic_table(nominal, {"/lowstate": {"host": "other"}})
    assert t["/lowstate"].host == "other" and t["/utlidar/robot_odom"].host == "robot"
    assert t["/nav/cmd_vel"].liveness == "event" and t["/helix/hold"].liveness == "periodic"
    with pytest.raises(EvidenceError):
        topic_table(nominal, {"/nope": {"host": "x"}})
    with pytest.raises(EvidenceError):
        topic_table(nominal, {"/nav/cmd_vel": {"liveness": "periodic"}})   # no stale_after_s


def test_replay_clock_is_monotonic():
    c = ReplayClock(0)
    assert c.advance_to(5) == 5 and c.advance_to(5) == 0
    with pytest.raises(ClockError):
        c.advance_to(4)


def test_realtime_pacer_scales_sleep():
    slept = []
    p = RealtimePacer(4.0, sleep=slept.append)
    p.wait(2_000_000_000)
    p.wait(0)
    assert slept == [0.5]
    with pytest.raises(ValueError):
        RealtimePacer(0.0)
