"""GO2 profile contents and profile validation."""

from __future__ import annotations

import pytest

from blackboxrs.flight.profile import ProfileError, load_profile, profile_from_dict

REQUIRED_TOPICS = {
    "/api/sport/request", "/api/sport/response", "/utlidar/robot_odom", "/cmd_vel",
    "/helix/hold", "/helix/arbiter/status", "/helix/recovery_hints",
    "/helix/recovery_actions", "/helix/faults", "/sportmodestate", "/lowstate",
    "/phoenix/estop", "/phoenix/shield", "/joint_group_position_controller/command",
}


def test_go2_profile_covers_the_experiment_topics(go2):
    names = {t.name for t in go2.topics}
    assert REQUIRED_TOPICS <= names


def test_every_go2_topic_is_optional(go2):
    # Not every experiment runs every subsystem: absence must never be fatal.
    assert not [t.name for t in go2.topics if t.required]


def test_go2_window_and_stop_criteria(go2):
    assert go2.buffer.pre_trigger_sec == 10.0
    assert go2.buffer.post_trigger_sec == 15.0
    # Copied from HELIX hw_stage.py STOPPED_SPEED / STOP_DEADLINE_S.
    assert go2.stop.stopped_speed_mps == 0.03
    assert go2.stop.stop_deadline_sec == 1.5


def test_robot_side_roles_are_not_co_hosted(go2):
    # Odometry and sport responses come from the GO2 computer, not the payload.
    assert "odometry" not in go2.co_hosted_roles
    assert "sport_response" not in go2.co_hosted_roles
    assert {"helix_hold", "arbiter_status", "sport_request"} <= go2.co_hosted_roles


def test_evidence_override_keeps_hash(tmp_path):
    a = load_profile("go2")
    b = load_profile("go2", evidence_dir=str(tmp_path))
    assert a.sha256 == b.sha256
    assert b.evidence_path == tmp_path


@pytest.mark.parametrize("topic,err", [
    ({"name": "cmd_vel", "type": "geometry_msgs/msg/Twist"}, "fully qualified"),
    ({"name": "/x", "type": "Twist"}, "pkg/msg/Type"),
    ({"name": "/x", "type": "a/msg/B", "role": "nope"}, "unknown role"),
    ({"name": "/x"}, "needs name and type"),
])
def test_bad_topics_rejected(topic, err):
    with pytest.raises(ProfileError, match=err):
        profile_from_dict({"topics": [topic]})


def test_duplicate_topic_rejected():
    t = {"name": "/x", "type": "a/msg/B"}
    with pytest.raises(ProfileError, match="twice"):
        profile_from_dict({"topics": [t, t]})


def test_non_positive_window_rejected():
    with pytest.raises(ProfileError, match="> 0"):
        profile_from_dict({"topics": [{"name": "/x", "type": "a/msg/B"}],
                           "buffer": {"pre_trigger_sec": 0}})


def test_unknown_profile_name():
    with pytest.raises(ProfileError, match="not found"):
        load_profile("no_such_profile_xyz")


def test_go2_helix_extends_go2_without_cmd_vel(tmp_path):
    base = load_profile("go2")
    h = load_profile("go2_helix")
    assert h.topic("/cmd_vel") is None
    assert {t.name for t in h.topics} == {t.name for t in base.topics} - {"/cmd_vel"}
    assert h.sha256 != base.sha256
    assert h.buffer == base.buffer and h.stop == base.stop
    # the embedded text is the resolved profile, so replay needs no base file
    from blackboxrs.flight.profile import profile_from_text
    again = profile_from_text(h.text)
    assert {t.name for t in again.topics} == {t.name for t in h.topics}


def test_extends_rejects_unknown_exclusion(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("extends: go2\nprofile: bad\nexclude_topics: [/nope]\n")
    with pytest.raises(ProfileError, match="exclude_topics"):
        load_profile(str(p))
