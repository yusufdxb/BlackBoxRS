"""Rolling buffer, triggers and incident lifecycle (no ROS)."""

from __future__ import annotations

from blackboxrs.flight.core import FlightCore
from blackboxrs.flight.profile import profile_from_dict
from blackboxrs.flight.records import make_msg_record

S = 1_000_000_000
W0 = 1_789_000_000 * S


class Sink:
    def __init__(self):
        self.pre, self.appended, self.triggers, self.status = [], [], [], None
        self.pre_window = None

    def open(self, trigger, pre, pre_window):
        self.pre, self.pre_window = list(pre), pre_window
        self.triggers.append(trigger)
        return "b"

    def append(self, rec):
        self.appended.append(rec)

    def add_trigger(self, t):
        self.triggers.append(t)

    def close(self, status, stats):
        self.status = status
        return f"/b{len(self.triggers)}"

    def wait(self, timeout=None):
        pass


def prof(stale=False, **over):
    hold_topic = {"name": "/helix/hold", "type": "helix_msgs/msg/HelixHold",
                  "role": "helix_hold"}
    if stale:
        hold_topic["stale_after_sec"] = 0.5
    raw = {"profile": "t", "buffer": {"pre_trigger_sec": 2.0, "post_trigger_sec": 3.0},
           "expected_nodes": ["/helix_arbiter"],
           "topics": [hold_topic,
                      {"name": "/cmd_vel", "type": "geometry_msgs/msg/Twist",
                       "role": "cmd_vel_out"},
                      {"name": "/helix/arbiter/status", "type": "helix_msgs/msg/ArbiterStatus",
                       "role": "arbiter_status"},
                      {"name": "/helix/recovery_actions", "type": "helix_msgs/msg/RecoveryAction",
                       "role": "recovery_action"}]}
    raw.update(over)
    return profile_from_dict(raw)


def mk(topic, role, t, data):
    return make_msg_record(topic=topic, role=role, msg_type="x/msg/Y", data=data,
                           t_mono_ns=int(t * S), t_wall_ns=W0 + int(t * S))


def run_core(p=None, **kw):
    sinks = []

    def factory():
        sinks.append(Sink())
        return sinks[-1]
    return FlightCore(p or prof(), factory, **kw), sinks


def hold(t, h):
    return mk("/helix/hold", "helix_hold", t, {"hold": h, "fault_id": "f" if h else ""})


def test_pre_window_contains_only_the_last_pre_seconds():
    core, sinks = run_core()
    for i in range(100):  # 10 s at 10 Hz
        core.ingest(mk("/cmd_vel", "cmd_vel_out", i * 0.1, {"linear": {"x": 0.1}}))
    core.ingest(hold(10.0, False))
    core.ingest(hold(10.05, True))
    pre = sinks[0].pre
    assert pre and min(r["t_mono_ns"] for r in pre) >= int((10.05 - 2.0) * S)
    assert sinks[0].pre_window["available_s"] >= 1.99


def test_post_window_closes_bundle():
    core, sinks = run_core()
    core.ingest(hold(0.0, False))
    core.ingest(hold(1.0, True))
    core.tick(int(3.9 * S), W0)
    assert sinks[0].status is None and core.incident_open
    core.tick(int(4.01 * S), W0)
    assert sinks[0].status == "complete" and not core.incident_open


def test_secondary_trigger_attaches_and_extends_but_is_capped():
    core, sinks = run_core()
    core.ingest(hold(0.0, False))
    core.ingest(hold(1.0, True))
    core.ingest(mk("/helix/recovery_actions", "recovery_action", 3.5,
                   {"action": "STOP_AND_HOLD", "status": "ACCEPTED", "fault_id": "f"}))
    assert len(sinks) == 1 and [t["role"] for t in sinks[0].triggers] == ["primary", "secondary"]
    core.tick(int(4.1 * S), W0)
    assert core.incident_open  # extended to 3.5 + 3 = 6.5
    core.tick(int(6.6 * S), W0)
    assert not core.incident_open


def test_hold_edge_only_once_and_first_observation_flagged():
    core, sinks = run_core()
    core.ingest(hold(0.0, True))  # recorder started while HELIX already held
    assert sinks[0].triggers[0]["observed_edge"] is False
    core.ingest(hold(0.1, True))
    assert core.stats.triggers_fired == 1


def test_arbiter_forced_zero_edge():
    core, sinks = run_core()
    for i, reason in enumerate(["SOURCE", "HELIX_HOLD", "HELIX_HOLD", "SOURCE"]):
        core.ingest(mk("/helix/arbiter/status", "arbiter_status", i * 0.05, {"reason": reason}))
    types = [t["type"] for s in sinks for t in s.triggers]
    assert types == ["arbiter_forced_zero"]


def test_recovery_action_suppressed_is_not_a_trigger():
    core, sinks = run_core()
    core.ingest(mk("/helix/recovery_actions", "recovery_action", 0.0,
                   {"action": "STOP_AND_HOLD", "status": "SUPPRESSED_COOLDOWN"}))
    assert not sinks


def test_stale_topic_fires_once_and_rearms():
    core, sinks = run_core(prof(stale=True, triggers={"max_incidents_per_run": 5}))
    core.ingest(hold(0.0, False))
    core.tick(int(0.4 * S), W0)
    assert not sinks
    core.tick(int(0.6 * S), W0)
    core.tick(int(0.8 * S), W0)
    assert [t["type"] for t in sinks[0].triggers] == ["topic_stale"]
    core.ingest(hold(0.9, False))
    core.tick(int(1.5 * S), W0)
    assert [t["type"] for t in sinks[0].triggers] == ["topic_stale", "topic_stale"]


def test_node_disappearance_only_for_watched_nodes():
    core, sinks = run_core()
    core.graph(0, W0, ["/helix_arbiter", "/random_tool", "/_ros2cli_1"], {}, {})
    core.graph(S, W0 + S, ["/helix_arbiter"], {}, {})
    assert not sinks  # /random_tool and cli nodes are not watched
    core.graph(2 * S, W0 + 2 * S, [], {}, {})
    assert sinks[0].triggers[0]["type"] == "node_disappeared"
    assert sinks[0].triggers[0]["node"] == "/helix_arbiter"


def test_publisher_nodes_become_watched():
    core, sinks = run_core()
    core.graph(0, W0, ["/helix_arbiter", "/odom_node"], {}, {"/utlidar/robot_odom": ["/odom_node"]})
    core.graph(S, W0 + S, ["/helix_arbiter"], {}, {})
    assert sinks and sinks[0].triggers[0]["node"] == "/odom_node"


def test_full_graph_snapshot_recurs_so_windows_start_known():
    core, _ = run_core()
    for i in range(12):
        core.graph(i * S, W0 + i * S, ["/a"], {}, {})
    fulls = [r for r in core.window_records() if r.get("kind") == "graph" and r.get("full")]
    assert len(fulls) >= 1 and all(r["t_mono_ns"] >= 8 * S for r in fulls)


def test_ring_caps_count_and_flag_evictions_inside_window():
    p = prof(buffer={"pre_trigger_sec": 10.0, "post_trigger_sec": 1.0, "max_records": 50})
    core, sinks = run_core(p)
    for i in range(200):
        core.ingest(mk("/cmd_vel", "cmd_vel_out", i * 0.01, {}))
    assert len(core.window_records()) == 50
    assert core.stats.ring_evicted_by_cap_in_window == 150
    core.ingest(hold(2.0, True))  # pushes one more record out of the full ring
    assert sinks[0].pre_window["evicted_by_cap_in_window"] == 151


def test_disk_pressure_refuses_bundle_and_counts_it():
    core, sinks = run_core(can_open=lambda: (False, "disk_pressure: 10 MB free"))
    core.ingest(hold(0.0, False))
    core.ingest(hold(0.1, True))
    assert not sinks
    assert core.stats.incidents_skipped == 1
    assert core.stats_dict()["skip_reasons"] == {"disk_pressure: 10 MB free": 1}


def test_incident_limit():
    core, sinks = run_core(prof(triggers={"max_incidents_per_run": 1}))
    core.mark(0, W0, "a")
    core.tick(int(3.1 * S), W0)
    core.mark(int(4 * S), W0, "b")
    assert len(sinks) == 1 and core.stats.triggers_suppressed_limit == 1


def test_clock_jump_is_recorded():
    core, _ = run_core()
    core.ingest(mk("/cmd_vel", "cmd_vel_out", 0.0, {}))
    r = mk("/cmd_vel", "cmd_vel_out", 0.1, {})
    r["t_wall_ns"] += 3 * S
    core.ingest(r)
    jumps = [x for x in core.window_records() if x.get("kind") == "clock_jump"]
    assert len(jumps) == 1 and jumps[0]["wall_step_ns"] == 3 * S


def test_shutdown_marks_open_bundle_interrupted():
    core, sinks = run_core()
    core.mark(0, W0, "x")
    core.shutdown("signal")
    assert sinks[0].status == "interrupted"
    assert sinks[0].triggers[-1]["type"] == "recorder_shutdown"


def test_trigger_records_have_ingest_ordered_seq():
    core, sinks = run_core()
    core.ingest(hold(0.0, False))
    core.ingest(hold(0.1, True))
    seqs = [r["seq"] for r in sinks[0].pre]
    assert sinks[0].triggers[0]["seq"] > max(seqs)
