"""Frozen arbiter-parity scenarios and the decision-trace driver.

Each scenario is a script of input messages (command sources and HELIX hold
states) at fixed receipt times. The same script drives every implementation
being compared:

* the system under test Replay Lab uses (``HelixArbiterAdapter`` around the
  real HELIX core, or the ``twist_mux_legacy`` model),
* the real HELIX ``arbiter_node`` and the real ``twist_mux`` binary running
  under ROS 2 (``scripts/parity/run_ros_parity.py``).

A trace is the decision sequence at every arbiter tick (50 Hz) and at every
publication between ticks: time, reason, winning source, robot-facing
command, and whether anything was published. Comparing traces compares
decisions step by step, not a final verdict.

Hold messages follow the HELIX recovery node: 20 Hz, one (epoch, seq)
counter. On the legacy twist_mux path the recovery node also publishes one
zero Twist on /helix/cmd_vel right after each hold=true message
(recovery_node.py ``_on_publish_tick``); ``legacy_events`` adds exactly that.

Times avoid landing exactly on a timeout boundary: two implementations that
agree on ``age <= timeout`` can still disagree on a float tie, and that is
not a behavioural difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.values import NS, canonical_json

NAV, TELEOP, HOLD, HELIX_CMD = "/nav/cmd_vel", "/teleop/cmd_vel", "/helix/hold", "/helix/cmd_vel"
PERIOD_NS = 20_000_000          # arbiter_node rate_hz 50.0
HOLD_HZ = 20.0                  # recovery_node PUBLISH_HZ
SRC_HZ = 20.0


def twist(vx: Any = 0.0, vy: Any = 0.0, wz: Any = 0.0, lz: Any = 0.0) -> dict[str, Any]:
    return {"linear": {"x": vx, "y": vy, "z": lz}, "angular": {"x": 0.0, "y": 0.0, "z": wz}}


@dataclass
class Script:
    """Builder for one scenario's input messages."""

    steps: list[tuple[float, str, dict[str, Any]]] = field(default_factory=list)
    _hold_seq: int = 0

    def stream(self, topic: str, t0: float, t1: float, data: dict[str, Any] | Callable[
            [float], dict[str, Any]], hz: float = SRC_HZ, offset: float = 0.0) -> "Script":
        k = 0
        while True:
            t = round(t0 + offset + k / hz, 6)
            if t >= t1:
                break
            self.steps.append((t, topic, data(t) if callable(data) else dict(data)))
            k += 1
        return self

    def hold(self, t: float, held: bool, *, epoch: int = 1, seq: int | None = None) -> "Script":
        if seq is None:
            self._hold_seq += 1
            seq = self._hold_seq
        self.steps.append((round(t, 6), HOLD, {"hold": held, "fault_id": "f1" if held else "",
                                               "epoch": epoch, "seq": seq}))
        return self

    def holds(self, t0: float, t1: float, held: Callable[[float], bool] | bool,
              *, epoch: int = 1, offset: float = 0.013) -> "Script":
        k = 0
        while True:
            t = round(t0 + offset + k / HOLD_HZ, 6)
            if t >= t1:
                break
            self.hold(t, held(t) if callable(held) else held, epoch=epoch)
            k += 1
        return self


@dataclass(frozen=True)
class Scenario:
    name: str
    covers: str
    script: Script
    duration_s: float


def _scenarios() -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}

    def add(name: str, covers: str, script: Script, duration: float = 3.0) -> None:
        out[name] = Scenario(name, covers, script, duration)

    add("nominal_velocity", "a released hold and one fresh source drive the output",
        Script().holds(0.0, 3.0, False).stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007))
    add("stop", "hold asserted while navigation keeps commanding",
        Script().holds(0.0, 3.0, lambda t: t >= 1.0).stream(NAV, 0.1, 3.0, twist(0.3),
                                                               offset=0.007))
    add("stale_command", "the only source goes silent mid-motion",
        Script().holds(0.0, 3.0, False).stream(NAV, 0.1, 1.0, twist(0.3), offset=0.007))
    add("stop_while_teleop_active", "hold asserted while teleop (priority 200) streams",
        Script().holds(0.0, 3.0, lambda t: t >= 1.0).stream(TELEOP, 0.2, 3.0, twist(0.4),
                                                               offset=0.004))
    add("teleop_after_stop", "teleop starts after the hold is asserted",
        Script().holds(0.0, 3.0, lambda t: t >= 1.0).stream(TELEOP, 1.5, 3.0, twist(0.4),
                                                               offset=0.004))
    add("nan_command", "NaN on the active source, then valid again",
        Script().holds(0.0, 3.0, False).stream(
            NAV, 0.1, 3.0, lambda t: twist(math.nan if 1.0 <= t < 1.2 else 0.3), offset=0.007))
    add("nan_on_unused_axis", "NaN on linear.z only (an axis the GO2 does not use)",
        Script().holds(0.0, 3.0, False).stream(
            NAV, 0.1, 3.0, lambda t: twist(0.3, lz=math.nan if 1.0 <= t < 1.2 else 0.0),
            offset=0.007))
    add("over_limit_command", "a command above the arbiter limit (1.0 m/s)",
        Script().holds(0.0, 3.0, False).stream(
            NAV, 0.1, 3.0, lambda t: twist(1.3 if 1.0 <= t < 1.2 else 0.3), offset=0.007))
    add("freshness_expiry", "one command, then silence across its 0.5 s window",
        Script().holds(0.0, 2.0, False).stream(NAV, 0.507, 0.52, twist(0.3)), 2.0)
    add("conflicting_sources", "teleop and nav both live; teleop leaves and returns",
        Script().holds(0.0, 3.0, False).stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007)
        .stream(TELEOP, 0.3, 1.5, twist(0.1), offset=0.004)
        .stream(TELEOP, 2.2, 3.0, twist(-0.2), offset=0.004))
    add("hold_stream_lost", "the hold state stops arriving (recovery node lost)",
        Script().holds(0.0, 1.0, False).stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007))
    add("hold_missing_at_start", "commands arrive before any hold state",
        Script().stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007).holds(1.0, 3.0, False))
    add("resume_needs_new_command", "release after a hold; the source stopped during it",
        Script().holds(0.0, 3.0, lambda t: 1.0 <= t < 1.6)
        .stream(NAV, 0.1, 1.3, twist(0.3), offset=0.007))
    add("release_with_live_source", "release after a hold while the source keeps streaming",
        Script().holds(0.0, 3.0, lambda t: 1.0 <= t < 1.6)
        .stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007))
    s = Script().holds(0.0, 3.0, lambda t: t >= 1.0).stream(NAV, 0.1, 3.0, twist(0.3),
                                                               offset=0.007)
    s.hold(1.5071, False, seq=3)       # a re-delivered old RESUME, while held
    add("old_resume_redelivered", "an older (epoch, seq) release arrives during a hold", s)
    s = Script().holds(0.0, 1.0, True).stream(NAV, 0.1, 3.0, twist(0.3), offset=0.007)
    s.holds(1.8, 3.0, False, epoch=0)  # recovery restarted with a lower epoch
    add("restart_lower_epoch", "hold stream stops, then resumes from a restarted publisher", s)
    return out


SCENARIOS = _scenarios()


def events_for(sc: Scenario, *, legacy: bool = False) -> list[ReplayEvent]:
    """The script as replay events; ``legacy`` adds the recovery node's zero twists."""
    steps = list(sc.script.steps)
    if legacy:
        steps += [(t, HELIX_CMD, twist(0.0)) for t, topic, d in sc.script.steps
                  if topic == HOLD and d["hold"]]
    steps.sort(key=lambda s: (s[0], {HOLD: 0, HELIX_CMD: 1}.get(s[1], 2), s[1]))
    out = []
    for i, (t, topic, data) in enumerate(steps):
        t_ns = int(round(t * NS))
        role = {HOLD: "helix_hold"}.get(topic, "cmd_vel_source")
        out.append(ReplayEvent(t_ns=t_ns, order=(0, i + 1, 0), eid=f"s{i + 1}", kind="msg",
                               topic=topic, role=role, type="x/msg/Y", data=data,
                               src_ns=t_ns, rx_wall_ns=t_ns,
                               record={"kind": "msg", "seq": i + 1, "t_mono_ns": t_ns,
                                       "t_wall_ns": t_ns}))
    return out


def run_trace(sut: Any, events: list[ReplayEvent], duration_s: float,
              period_ns: int = PERIOD_NS) -> list[dict[str, Any]]:
    """Drive ``sut`` like the replay engine: events first, then the tick, at each instant."""
    trace: list[dict[str, Any]] = []

    def rec(d: Any, kind: str) -> None:
        raw = None if d.robot_raw is None else [
            ("NaN" if isinstance(v, float) and math.isnan(v) else v) for v in d.robot_raw]
        trace.append({"t_ns": d.t_ns, "kind": kind, "reason": d.reason, "source": d.source,
                      "command": raw, "published": bool(d.published)})

    end = int(round(duration_s * NS))
    t = 0
    i = 0
    while t <= end:
        while i < len(events) and events[i].t_ns <= t:
            e = events[i]
            i += 1
            sut.on_event(e, e.t_ns)
            pub = sut.take_publication(e.t_ns)
            if pub is not None:
                rec(pub, "publish")
        rec(sut.tick(t), "tick")
        t += period_ns
    return trace


def diff_traces(a: list[dict[str, Any]], b: list[dict[str, Any]],
                fields: tuple[str, ...] = ("t_ns", "kind", "reason", "source", "command",
                                            "published")) -> list[str]:
    """Step-by-step differences between two traces ([] = identical on ``fields``)."""
    out = []
    for n in range(max(len(a), len(b))):
        x = a[n] if n < len(a) else None
        y = b[n] if n < len(b) else None
        if x is None or y is None:
            out.append(f"step {n}: only in {'first' if y is None else 'second'}: {x or y}")
            continue
        dx = {k: x.get(k) for k in fields}
        dy = {k: y.get(k) for k in fields}
        if canonical_json(dx) != canonical_json(dy):
            out.append(f"step {n}: {dx} != {dy}")
    return out


# ---------------------------------------------------------------------------
# comparison against traces recorded from the real processes
# ---------------------------------------------------------------------------

SEGMENT_FROM_S = 0.05       # before the first hold state, the real timer phase decides
TRANSITION_TOL_S = 0.025    # one 50 Hz period (the real timer phase is arbitrary) + 5 ms
IMMEDIATE_TOL_S = 0.005     # arbiter_node publishes on a hold assertion, before the next tick
MUX_TOL_S = 0.005           # twist_mux publishes from the input callback


def _expand(payload: list[Any]) -> dict[str, Any]:
    if len(payload) == 4 and isinstance(payload[0], bool):
        return {"hold": payload[0], "epoch": payload[1], "seq": payload[2],
                "fault_id": payload[3]}
    v = [math.nan if x == "NaN" else x for x in payload]
    return {"linear": {"x": v[0], "y": v[1], "z": v[2]},
            "angular": {"x": v[3], "y": v[4], "z": v[5]}}


def recorded_events(rec: dict[str, Any]) -> list[ReplayEvent]:
    """The inputs of a recorded run, at the times they were actually sent."""
    out = []
    for i, (_, t_sent, topic, payload) in enumerate(rec["sent"]):
        t_ns = int(round(t_sent * NS))
        out.append(ReplayEvent(t_ns=t_ns, order=(0, i + 1, 0), eid=f"s{i + 1}", kind="msg",
                               topic=topic, role="helix_hold" if topic == HOLD else "cmd_vel_source",
                               type="x/msg/Y", data=_expand(payload), src_ns=t_ns,
                               rx_wall_ns=t_ns, record={"kind": "msg", "seq": i + 1,
                                                        "t_mono_ns": t_ns, "t_wall_ns": t_ns}))
    out.sort(key=lambda e: e.key)
    return out


def _segments(rows: list[tuple[float, tuple[Any, ...]]]) -> list[tuple[float, tuple[Any, ...]]]:
    segs: list[tuple[float, tuple[Any, ...]]] = []
    for t, key in rows:
        if not segs or segs[-1][1] != key:
            segs.append((t, key))
    return segs


def _key(reason: str, source: str, cmd: list[Any]) -> tuple[Any, ...]:
    return (reason, source, tuple(round(float(v), 9) for v in cmd))


def compare_helix(rec: dict[str, Any], make_sut: Callable[[], Any]) -> dict[str, Any]:
    """Replay Lab's HELIX system under test against a recorded real arbiter_node run.

    Decisions are compared as the ordered sequence of (reason, source,
    command) segments, which must be identical; each transition time must
    agree within one arbiter period. Separately, every hold=true message must
    be followed by a real publication within IMMEDIATE_TOL_S (the node's
    publish-on-hold glue).
    """
    events = recorded_events(rec)
    trace = run_trace(make_sut(), events, rec["duration_s"])
    ours = _segments([(r["t_ns"] / NS, _key(r["reason"], r["source"], r["command"]))
                      for r in trace if r["t_ns"] / NS >= SEGMENT_FROM_S])
    real_rows = [(o[0], _key(o[1], o[2], o[3:6])) for o in rec["outputs"]
                 if SEGMENT_FROM_S <= o[0] <= rec["duration_s"]]
    real = _segments(real_rows)
    problems = []
    if [k for _, k in ours] != [k for _, k in real]:
        problems.append(f"decision sequence differs: replay {[k for _, k in ours]} vs real "
                        f"{[k for _, k in real]}")
    else:
        for (ta, k), (tb, _) in list(zip(ours, real))[1:]:
            if abs(ta - tb) > TRANSITION_TOL_S:
                problems.append(f"transition to {k} at {ta:.4f}s (replay) vs {tb:.4f}s (real)")
    out_t = [o[0] for o in rec["outputs"]]
    late = []
    for e in events:
        if e.topic == HOLD and e.data["hold"]:
            t = e.t_ns / NS
            if not any(t <= x <= t + IMMEDIATE_TOL_S for x in out_t):
                late.append(round(t, 4))
    if late:
        problems.append(f"no real publication within {IMMEDIATE_TOL_S}s of hold=true at {late}")
    ours_t = [r["t_ns"] / NS for r in trace if r["kind"] == "publish"]
    missing = [round(e.t_ns / NS, 4) for e in events if e.topic == HOLD and e.data["hold"]
               and not any(e.t_ns / NS <= x <= e.t_ns / NS + IMMEDIATE_TOL_S for x in ours_t)]
    if missing:
        problems.append(f"replay does not publish on hold=true (the node does) at {missing[:5]}"
                        f"{' ...' if len(missing) > 5 else ''}")
    return {"scenario": rec["scenario"], "ok": not problems, "problems": problems,
            "segments": len(real), "real_outputs": len(rec["outputs"]),
            "holds_asserted": sum(1 for e in events if e.topic == HOLD and e.data["hold"])}


def compare_twist_mux(rec: dict[str, Any], make_sut: Callable[[], Any]) -> dict[str, Any]:
    """The twist_mux model against a recorded real twist_mux run, message by message.

    Every real output must be matched, in order, by a model publication with
    the same command within MUX_TOL_S, and vice versa. Silence (no
    publication) is part of the comparison: an extra or missing message is a
    mismatch.
    """
    events = recorded_events(rec)
    trace = run_trace(make_sut(), events, rec["duration_s"])
    ours = [(r["t_ns"] / NS, [("NaN" if v == "NaN" else round(float(v), 9)) for v in r["command"]])
            for r in trace if r["kind"] == "publish"]
    real = [(o[0], [("NaN" if v == "NaN" else round(float(v), 9)) for v in o[1:4]])
            for o in rec["outputs"]]
    problems = []
    if [c for _, c in ours] != [c for _, c in real]:
        n = next((i for i, (a, b) in enumerate(zip(ours, real)) if a[1] != b[1]),
                 min(len(ours), len(real)))
        problems.append(f"published sequence differs at message {n}: model {len(ours)} msgs, "
                        f"real {len(real)} msgs; model[{n}]={ours[n] if n < len(ours) else None}"
                        f" real[{n}]={real[n] if n < len(real) else None}")
    else:
        for (ta, c), (tb, _) in zip(ours, real):
            if abs(ta - tb) > MUX_TOL_S:
                problems.append(f"message {c} at {ta:.4f}s (model) vs {tb:.4f}s (real)")
                break
    last_in = max((e.t_ns / NS for e in events), default=0.0)
    last_out = real[-1][0] if real else None
    return {"scenario": rec["scenario"], "ok": not problems, "problems": problems,
            "real_messages": len(real), "last_input_s": round(last_in, 4),
            "last_output_s": None if last_out is None else round(last_out, 4),
            "silent_after_last_output_s": None if last_out is None
            else round(rec["duration_s"] - last_out, 4)}
