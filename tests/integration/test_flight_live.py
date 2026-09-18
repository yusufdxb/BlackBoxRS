"""Live rclpy capture on an isolated localhost-only DDS domain.

Skipped without rclpy. Publishes only std/geometry test messages on
/bbrs_test/* topics in domain 92 with ROS_LOCALHOST_ONLY=1.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

rclpy = pytest.importorskip("rclpy")


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    monkeypatch.setenv("ROS_DOMAIN_ID", "92")


def _profile(tmp_path):
    from blackboxrs.flight.profile import profile_from_dict
    return profile_from_dict({
        "profile": "live_test", "evidence_dir": str(tmp_path),
        "buffer": {"pre_trigger_sec": 1.0, "post_trigger_sec": 1.0},
        "sampling": {"graph_poll_sec": 0.2, "system_sample_hz": 5.0, "health_tick_sec": 0.05},
        "topics": [
            {"name": "/bbrs_test/cmd_vel", "type": "geometry_msgs/msg/Twist",
             "role": "cmd_vel_out", "stale_after_sec": 0.4},
            {"name": "/bbrs_test/wrong_type", "type": "geometry_msgs/msg/Twist",
             "role": "other"},
            {"name": "/bbrs_test/absent", "type": "std_msgs/msg/Bool", "role": "other"},
            {"name": "/bbrs_test/nopkg", "type": "no_such_msgs/msg/X", "role": "other"},
            {"name": "/bbrs_test/decimated", "type": "std_msgs/msg/String", "role": "other",
             "store_max_hz": 5.0},
        ]})


def test_live_capture_marker_staleness_and_availability(tmp_path, isolated):
    from geometry_msgs.msg import Twist
    from std_msgs.msg import String

    from blackboxrs.flight.provenance import build_session
    from blackboxrs.flight.recorder import FlightRecorder, request_marker

    prof = _profile(tmp_path)
    rec = FlightRecorder(prof, build_session(prof, synthetic=True, experiment="pytest"),
                         gpu="none")
    ctx = rclpy.Context()
    rclpy.init(context=ctx)
    node = rclpy.create_node("bbrs_live_test_pub", context=ctx)
    p_twist = node.create_publisher(Twist, "/bbrs_test/cmd_vel", 10)
    p_wrong = node.create_publisher(String, "/bbrs_test/wrong_type", 10)
    p_dec = node.create_publisher(String, "/bbrs_test/decimated", 10)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            m = Twist()
            m.linear.x = 0.1
            p_twist.publish(m)
            p_wrong.publish(String(data="x"))
            p_dec.publish(String(data="d"))
            time.sleep(0.02)

    th = threading.Thread(target=loop)
    th.start()
    try:
        rec.spin(2.0)
        request_marker(prof, "pytest marker")
        rec.spin(1.5)
        stop.set()
        th.join()
        rec.spin(1.5)  # publisher silent -> /bbrs_test/cmd_vel goes stale
    finally:
        stop.set()
        bundles = rec.close()
        node.destroy_node()
        rclpy.shutdown(context=ctx)

    st = rec.topic_status
    assert st["/bbrs_test/cmd_vel"]["status"] == "subscribed"
    assert st["/bbrs_test/wrong_type"]["status"] == "type_mismatch"
    assert st["/bbrs_test/absent"]["status"] in ("absent", "no_publishers")
    assert st["/bbrs_test/nopkg"]["status"] == "type_unavailable"
    assert len(bundles) >= 1
    first = Path(bundles[0])
    rep = json.loads((first / "report.json").read_text())
    assert rep["trigger"]["type"] == "manual_marker"
    assert rep["synthetic"] is True
    recs = [json.loads(x) for x in (first / "records.jsonl").read_text().splitlines()]
    tw = [r for r in recs if r.get("topic") == "/bbrs_test/cmd_vel"]
    assert tw and tw[-1]["data"]["linear"]["x"] == 0.1
    assert all(r["dds_src_ns"] for r in tw)  # DDS source stamps captured
    dec = [r for r in recs if r.get("topic") == "/bbrs_test/decimated"]
    assert dec and sum(r["data"] is not None for r in dec) < len(dec)
    assert rep["topics"]["/bbrs_test/wrong_type"]["availability"] == "type_mismatch"
    # staleness after the publisher stopped: either its own bundle or attached
    types = {t["type"] for b in bundles
             for t in json.loads((Path(b) / "manifest.json").read_text())["triggers"]}
    assert "topic_stale" in types


def test_preflight_live_is_zero_motion_and_selftests(tmp_path, isolated):
    from blackboxrs.flight import load_profile
    from blackboxrs.flight.preflight import run_preflight

    prof = load_profile("go2", evidence_dir=str(tmp_path))
    res = run_preflight(prof, listen_sec=1.0)
    checks = {c["id"]: c["status"] for c in res["checks"]}
    assert res["verdict"] in ("GO", "GO WITH WARNINGS", "NO-GO")
    assert checks["no_publish"] == "PASS"
    assert checks["bundle_roundtrip"] == "PASS"
    assert checks["evidence_writable"] == "PASS"
    assert res["motion_commands_published"] == 0
    assert Path(res["written_to"]).exists()
    # nothing from the robot on an isolated domain: the report must say so
    assert checks["topics_present"] in ("WARN", "FAIL")
