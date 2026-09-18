"""Synthetic GO2 + HELIX traffic for the offline rehearsal and the tests.

Everything here is GENERATED. Bundles produced from it carry
``session.synthetic = true`` and every report says so. The odometry is a
first-order lag model, not a GO2 measurement.

The generator models three clocks, the way the hardware setup has them:

* payload wall clock (HELIX, arbiter, sink, /cmd_vel publishers),
* robot clock (GO2 odometry header stamps and the robot's DDS writes),
  offset from the payload by ``robot_clock_offset_s``,
* recorder clocks: monotonic and wall, with receipt = emission + transport
  + executor queueing jitter.

Scenario variants inject exactly one kind of trouble each, so a test can
check that the report names it.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from blackboxrs.flight.bundle import BundleWriter
from blackboxrs.flight.core import FlightCore
from blackboxrs.flight.profile import FlightProfile
from blackboxrs.flight.records import StoreDecimator, make_event_record, make_msg_record

_NS = 1_000_000_000
PAYLOAD_NODES = ["/helix_arbiter", "/helix_recovery_node", "/helix_diagnosis_node",
                 "/helix_go2_sport_sink", "/helix_hw_stage"]
ROBOT_NODES = ["/sport_service_node", "/utlidar_node"]


@dataclass
class Scenario:
    name: str = "stopmove"
    seed: int = 7
    duration_s: float = 30.0
    fault_at_s: float = 12.0
    cmd_vx: float = 0.15
    moving: bool = True
    robot_clock_offset_s: float = 1.734      # robot clock ahead of payload
    recorder_wall_offset_s: float = 0.0      # recorder on the payload: same wall clock
    transport_s: float = 0.0004
    robot_transport_s: float = 0.0025
    queue_jitter_s: float = 0.003
    odom_hz: float = 50.0
    lowstate_hz: float = 0.0
    stop_tau_s: float = 0.22
    robot_react_s: float = 0.06
    # trouble switches
    odom_stale_from_s: float | None = None
    drop_response: bool = False
    sink_dies_at_s: float | None = None
    drop_fraction: float = 0.0
    duplicate_fraction: float = 0.0
    reorder_odom: bool = False
    clock_jump_at_s: float | None = None
    clock_jump_s: float = 2.0
    interrupt_at_s: float | None = None
    marker_at_s: float | None = None
    robot_ignores_stop: bool = False
    # recorder stall: nothing is received during [stall_at_s, stall_at_s +
    # stall_s); everything queued is delivered at the end (executor backlog)
    stall_at_s: float | None = None
    stall_s: float = 0.08
    extra: dict[str, Any] = field(default_factory=dict)


VARIANTS: dict[str, dict[str, Any]] = {
    "normal_motion": {"fault_at_s": None, "marker_at_s": 12.0},
    "stopmove": {},
    "stale_odometry": {"odom_stale_from_s": 11.0},
    "missing_sport_response": {"drop_response": True},
    "node_death": {"fault_at_s": None, "sink_dies_at_s": 12.0},
    "dropped_messages": {"drop_fraction": 0.2},
    "duplicated_messages": {"duplicate_fraction": 0.1},
    "out_of_order": {"reorder_odom": True},
    "clock_jump": {"clock_jump_at_s": 12.5},
    "high_rate": {"odom_hz": 200.0, "lowstate_hz": 500.0},
    "interrupted": {"interrupt_at_s": 15.0},
    "not_moving": {"moving": False},
    "robot_ignores_stop": {"robot_ignores_stop": True},
    "recorder_stall_at_hold": {"stall_at_s": 11.95, "stall_s": 0.08},
}


def scenario(name: str, **over: Any) -> Scenario:
    base = Scenario(name=name)
    return replace(base, **{**VARIANTS.get(name, {}), **over})


class _Emitter:
    """Turns emission events into records with modelled receipt times."""

    def __init__(self, sc: Scenario, t_wall0: float, rng: random.Random,
                 decimator: StoreDecimator | None = None) -> None:
        self.sc = sc
        self.decimator = decimator
        self.t_wall0 = t_wall0
        self.rng = rng
        self.items: list[tuple[int, int, Any]] = []  # (t_mono_ns, order, record|callable)
        # (emission time relative to start, topic, type, data): what a live
        # publisher would send. Used by scripts/flight_dds_rehearsal.py.
        self.emissions: list[tuple[float, str, str, dict[str, Any]]] = []
        self._order = 0
        self.mono0 = 5_000 * _NS

    def recorder_times(self, t_rel_rx: float) -> tuple[int, int]:
        mono = self.mono0 + int(t_rel_rx * _NS)
        wall = self.t_wall0 + t_rel_rx + self.sc.recorder_wall_offset_s
        if self.sc.clock_jump_at_s is not None and t_rel_rx >= self.sc.clock_jump_at_s:
            wall += self.sc.clock_jump_s
        return mono, int(wall * _NS)

    def emit(self, t_rel: float, topic: str, role: str, mtype: str, data: dict[str, Any],
             *, robot: bool = False) -> None:
        sc = self.sc
        if sc.drop_fraction and role in ("helix_hold", "arbiter_status") and \
                self.rng.random() < sc.drop_fraction:
            return
        self.emissions.append((t_rel, topic, mtype, data))
        transport = sc.robot_transport_s if robot else sc.transport_s
        queue = self.rng.uniform(0, sc.queue_jitter_s)
        src_wall = self.t_wall0 + t_rel + (sc.robot_clock_offset_s if robot else 0.0)
        rx_rel = t_rel + transport
        cb_rel = rx_rel + queue
        if sc.stall_at_s is not None and sc.stall_at_s <= cb_rel < sc.stall_at_s + sc.stall_s:
            # backlog drains in arrival order (DDS keeps per-writer order)
            cb_rel = sc.stall_at_s + sc.stall_s + (rx_rel - sc.stall_at_s) * 1e-3
        mono, wall = self.recorder_times(cb_rel)
        _, rx_wall = self.recorder_times(rx_rel)
        stored = self.decimator is None or self.decimator.keep(topic, mono)
        rec = make_msg_record(topic=topic, role=role, msg_type=mtype, data=data,
                              t_mono_ns=mono, t_wall_ns=wall, t_ros_ns=wall,
                              dds_src_ns=int(src_wall * _NS), dds_rx_ns=rx_wall,
                              stored=stored)
        self._push(mono, rec)
        if sc.duplicate_fraction and self.rng.random() < sc.duplicate_fraction:
            dup = dict(rec)
            dup["t_mono_ns"] = mono + 200_000
            dup["t_wall_ns"] = wall + 200_000
            self._push(dup["t_mono_ns"], dup)

    def event(self, t_rel: float, fn: Callable[[FlightCore, int, int], None]) -> None:
        mono, wall = self.recorder_times(t_rel)
        self._push(mono, (fn, mono, wall))

    def _push(self, mono: int, item: Any) -> None:
        self._order += 1
        self.items.append((mono, self._order, item))


def _twist(vx: float) -> dict[str, Any]:
    return {"linear": {"x": vx, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}


def _req(req_id: int, api_id: int, param: str) -> dict[str, Any]:
    return {"header": {"identity": {"id": req_id, "api_id": api_id}, "lease": {"id": 0},
                       "policy": {"noreply": False}}, "parameter": param}


def generate(sc: Scenario, t_wall0: float = 1_789_000_000.0,
             profile: FlightProfile | None = None) -> _Emitter:
    """Build the full, time-ordered list of records and graph/marker events.

    With ``profile``, payloads are decimated per ``store_max_hz`` exactly as
    the live recorder does.
    """
    rng = random.Random(sc.seed)
    dec = None if profile is None else StoreDecimator(
        {t.name: t.store_max_hz for t in profile.topics if t.store_max_hz})
    em = _Emitter(sc, t_wall0, rng, dec)
    dt = 0.05
    n = int(sc.duration_s / dt)
    epoch = int((t_wall0 - 100) * _NS)
    hold_seq = arb_seq = 0
    req_id = 424_242_000
    held = False
    fault_t = sc.fault_at_s
    hold_t = None
    stopmove_rx_robot = None
    sink_alive = True
    fault_id = "rate_hz/utlidar_helix_injected"
    pending_responses: list[tuple[float, int, int]] = []
    # graph at start
    nodes = PAYLOAD_NODES + ROBOT_NODES
    pubs = {"/helix/hold": ["/helix_recovery_node"], "/cmd_vel": ["/helix_arbiter"],
            "/helix/arbiter/status": ["/helix_arbiter"], "/nav/cmd_vel": ["/helix_hw_stage"],
            "/api/sport/request": ["/helix_go2_sport_sink"],
            "/helix/sink/trace": ["/helix_go2_sport_sink"],
            "/utlidar/robot_odom": ["/utlidar_node"],
            "/api/sport/response": ["/sport_service_node"]}
    topics_all = {t: [] for t in pubs}

    def graph_at(t: float, node_list: list[str]) -> None:
        nl = list(node_list)
        pp = {k: [p for p in v if p in nl] for k, v in pubs.items()}
        em.event(t, lambda core, mono, wall: core.graph(mono, wall, nl, topics_all, pp))

    # poll the graph every 0.5 s, as the live recorder does
    tg = 0.0
    while tg < sc.duration_s:
        dead = sc.sink_dies_at_s is not None and tg >= sc.sink_dies_at_s + 0.3
        graph_at(tg, [x for x in nodes if not (dead and x == "/helix_go2_sport_sink")])
        tg += 0.5
    if sc.marker_at_s is not None:
        em.event(sc.marker_at_s, lambda core, mono, wall: core.mark(
            mono, wall, note="operator marker (synthetic)", source="synthetic"))

    for i in range(n):
        t = i * dt
        if sc.sink_dies_at_s is not None and t >= sc.sink_dies_at_s:
            sink_alive = False
        vx_cmd = sc.cmd_vx if sc.moving and 2.0 <= t else 0.0
        em.emit(t, "/nav/cmd_vel", "cmd_vel_source", "geometry_msgs/msg/Twist", _twist(vx_cmd))
        if i % 2 == 0:
            hold_seq += 1
            em.emit(t + 0.001, "/helix/hold", "helix_hold", "helix_msgs/msg/HelixHold", {
                "hold": held, "fault_id": fault_id if held else "", "reason": "holding" if held
                else "clear", "epoch": epoch, "seq": hold_seq, "stamp": t_wall0 + t + 0.001,
                "asserted_stamp": (t_wall0 + hold_t) if held and hold_t else 0.0})
        if fault_t is not None and abs(t - fault_t) < dt / 2:
            ft = t + 0.011
            em.emit(ft, "/helix/faults", "helix_fault", "helix_msgs/msg/FaultEvent", {
                "node_name": fault_id, "fault_type": "ANOMALY", "severity": 2,
                "detail": "injected benign fault (synthetic)", "timestamp": t_wall0 + ft,
                "context_keys": [], "context_values": []})
            em.emit(ft + 0.0011, "/helix/recovery_hints", "recovery_hint",
                    "helix_msgs/msg/RecoveryHint", {
                        "fault_id": fault_id, "suggested_action": "STOP_AND_HOLD",
                        "confidence": 0.9, "reasoning": "R1 anomaly", "rule_matched": "R1"})
            em.emit(ft + 0.0019, "/helix/recovery_actions", "recovery_action",
                    "helix_msgs/msg/RecoveryAction", {
                        "fault_id": fault_id, "action": "STOP_AND_HOLD", "status": "ACCEPTED",
                        "timestamp": t_wall0 + ft + 0.0019, "reason": "accepted"})
            held = True
            hold_t = ft + 0.0024
            hold_seq += 1
            em.emit(hold_t, "/helix/hold", "helix_hold", "helix_msgs/msg/HelixHold", {
                "hold": True, "fault_id": fault_id, "reason": "STOP_AND_HOLD R1",
                "epoch": epoch, "seq": hold_seq, "stamp": t_wall0 + hold_t,
                "asserted_stamp": t_wall0 + hold_t})
        # arbiter tick (slightly after the source), output zero while held
        at = t + 0.004 if not (held and hold_t and t < hold_t + dt) else max(t + 0.004,
                                                                            hold_t + 0.0006)
        out = 0.0 if held else vx_cmd
        arb_seq += 1
        em.emit(at, "/helix/arbiter/status", "arbiter_status", "helix_msgs/msg/ArbiterStatus", {
            "selected_source": "" if held else "nav", "reason": "HELIX_HOLD" if held else "SOURCE",
            "hold_active": held, "hold_fault_id": fault_id if held else "",
            "out_linear_x": out, "out_linear_y": 0.0, "out_angular_z": 0.0,
            "stamp": t_wall0 + at, "seq": arb_seq, "rejected_total": 0, "sink_subscribers": 1})
        em.emit(at + 0.0002, "/cmd_vel", "cmd_vel_out", "geometry_msgs/msg/Twist", _twist(out))
        if sink_alive:
            st = at + 0.0005
            api = 1008 if out != 0.0 else 1003
            req_id += 1
            reason = "MOVE" if api == 1008 else "ZERO"
            em.emit(st, "/helix/sink/trace", "sink_trace", "std_msgs/msg/String", {
                "data": json.dumps({"t_wall": t_wall0 + st, "t_mono": 100.0 + st,
                                    "mode": "armed", "api_id": api, "reason": reason,
                                    "x": out, "y": 0.0, "z": 0.0, "input": [out, 0.0, 0.0],
                                    "request_id": req_id, "sent_to_robot": True})})
            em.emit(st + 0.0001, "/api/sport/request", "sport_request", "unitree_api/msg/Request",
                    _req(req_id, api, json.dumps({"x": out, "y": 0.0, "z": 0.0})
                         if api == 1008 else ""))
            pending_responses.append((st + 0.018, req_id, api))
            if api == 1003 and held and stopmove_rx_robot is None:
                stopmove_rx_robot = st + sc.robot_transport_s
        for (rt, rid, api) in [p for p in pending_responses if p[0] <= t + dt]:
            pending_responses.remove((rt, rid, api))
            if sc.drop_response and api == 1003:
                continue
            em.emit(rt, "/api/sport/response", "sport_response", "unitree_api/msg/Response", {
                "header": {"identity": {"id": rid, "api_id": api}, "status": {"code": 0}},
                "data": ""}, robot=True)

    # odometry: first-order lag toward the commanded speed, robot-clock stamps
    x = y = 0.0
    v = 0.0
    odt = 1.0 / sc.odom_hz
    m = int(sc.duration_s * sc.odom_hz)
    prev_stamp = None
    for k in range(m):
        t = k * odt
        target = sc.cmd_vx if (sc.moving and t >= 2.0) else 0.0
        if (stopmove_rx_robot is not None and not sc.robot_ignores_stop
                and t >= stopmove_rx_robot + sc.robot_react_s):
            target = 0.0
        v += (target - v) * (1 - math.exp(-odt / sc.stop_tau_s))
        if abs(v) < 1e-4:
            v = 0.0 if target == 0.0 else v
        x += v * odt
        if sc.odom_stale_from_s is not None and t >= sc.odom_stale_from_s:
            continue
        stamp = t_wall0 + t + sc.robot_clock_offset_s
        if sc.reorder_odom and k % 25 == 0 and prev_stamp is not None:
            stamp = prev_stamp - 0.01
        prev_stamp = stamp
        em.emit(t, "/utlidar/robot_odom", "odometry", "nav_msgs/msg/Odometry", {
            "header": {"stamp": {"sec": int(stamp), "nanosec": int((stamp % 1) * 1e9)},
                       "frame_id": "odom"},
            "child_frame_id": "base_link",
            "pose": {"pose": {"position": {"x": x, "y": y, "z": 0.31},
                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}},
            "twist": {"twist": {"linear": {"x": v, "y": 0.0, "z": 0.0},
                                "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}}}, robot=True)
    if sc.lowstate_hz:
        for k in range(int(sc.duration_s * sc.lowstate_hz)):
            t = k / sc.lowstate_hz
            em.emit(t, "/lowstate", "go2_state", "unitree_go/msg/LowState", {
                "tick": k, "power_v": 28.9, "power_a": 1.2, "bms_state": {"soc": 81},
                "foot_force": [20, 21, 19, 22], "temperature_ntc1": 33}, robot=True)
    return em


def synthetic_topic_status(profile: FlightProfile, em: _Emitter) -> dict[str, Any]:
    seen = {rec["topic"] for _, _, rec in em.items if isinstance(rec, dict)}
    out = {}
    for t in profile.topics:
        if t.name in seen:
            out[t.name] = {"status": "subscribed", "graph_types": [t.type],
                           "publishers": ["<synthetic>"], "message_lost": 0}
        else:
            out[t.name] = {"status": "absent", "reason": "not on graph (synthetic run)"}
    return out


def run(profile: FlightProfile, sc: Scenario, out_dir: Path, *,
        session: dict[str, Any] | None = None,
        writer_factory: Callable[..., BundleWriter] | None = None,
        can_open: Callable[[], tuple[bool, str]] | None = None,
        sys_hz: float = 2.0) -> tuple[FlightCore, list[str]]:
    """Feed a scenario through the real core and bundle writer."""
    from blackboxrs.flight.analysis import analyze
    from blackboxrs.flight.render import render_markdown

    em = generate(sc, profile=profile)
    status = synthetic_topic_status(profile, em)
    if session is None:
        from blackboxrs.flight.provenance import build_session
        session = build_session(profile, experiment=f"rehearsal:{sc.name}",
                                session_id=f"synthetic_{sc.name}_{sc.seed}", synthetic=True)
    sess = {**session, "synthetic": True,
            "note": "GENERATED traffic; odometry is a first-order lag model, not a GO2"}
    session_dir = out_dir / sess["session_id"]
    wf = writer_factory or BundleWriter
    core = FlightCore(profile, lambda: wf(session_dir, profile, sess, lambda: status,
                                          analyze=analyze, render=render_markdown,
                                          fsync_every_sec=5.0), can_open=can_open)
    # system samples + ticks
    step = 1.0 / sys_hz
    t = 0.0
    while t < sc.duration_s:
        tt = t

        def _sys(core: FlightCore, mono: int, wall: int, tt: float = tt) -> None:
            core.ingest(make_event_record(
                "sys", t_mono_ns=mono, t_wall_ns=wall, cpu_percent=20.0 + 5 * math.sin(tt),
                mem_percent=41.0, recorder={"cpu_percent": 4.0, "rss_mb": 60.0},
                gpu={"backend": "synthetic", "load_percent": 12.0, "temp_c": 45.0},
                thermal_c={"cpu-thermal:thermal_zone0": 48.0}))
        em.event(tt, _sys)
        t += step
    t = 0.0
    while t < sc.duration_s:
        em.event(t, lambda core, mono, wall: core.tick(mono, wall))
        t += 0.05
    items = sorted(em.items, key=lambda it: (it[0], it[1]))
    stop_at = None if sc.interrupt_at_s is None else em.mono0 + int(sc.interrupt_at_s * _NS)
    wanted = {t.name for t in profile.topics}
    for mono, _, item in items:
        if isinstance(item, dict) and item.get("topic") not in wanted:
            continue  # the live recorder only subscribes to profile topics
        if stop_at is not None and mono >= stop_at:
            core.shutdown("interrupted (synthetic)")
            core.wait_writers()
            return core, core.closed_bundles
        if isinstance(item, dict):
            core.ingest(dict(item))
        else:
            fn, m, w = item
            fn(core, m, w)
    core.shutdown("end of synthetic traffic")
    core.wait_writers()
    return core, core.closed_bundles
