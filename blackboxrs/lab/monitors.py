"""Command-path safety invariants and the detectors BlackBoxRS lacked for them.

These monitors watch the replayed inputs and the robot-facing output of the
system under test. They are an independent oracle: they never call into the
arbitration model, and they judge freshness on the receipt clock only,
whatever the model does.

Safety invariants (a violation makes the replay verdict FAIL):

``stop_dominance``
    Once HELIX asserts a hold, every robot-facing command from ``grace``
    after the hold was received until HELIX releases it is zero. The hold
    state is ordered by the publisher's ``(epoch, seq)``, so a stale or
    reordered "released" message cannot end a hold.
``finite_output``
    Every robot-facing command component is a finite number.
``fresh_output``
    A nonzero robot-facing command equals the latest message of a command
    source that is valid (all six twist components finite) and was received
    within that source's freshness window (plus ``grace``). Zero is always
    allowed: stopping is never a freshness violation. A source's previous
    valid message still counts for ``grace`` after a newer one replaced it:
    a timer-driven arbiter publishes the new command only at its next tick,
    so the robot-facing command legitimately lags a source change by up to
    one period. (Found on a real HELIX run: a navigation command changed
    0.2 ms after an arbiter output and 7 ms before the replay's tick; without
    the allowance that lag read as a stale command.) An arbiter that keeps
    forwarding the older command past ``grace`` still violates.
``consistent_state``
    An arbiter status never reports the hold active while reporting a
    nonzero output (evaluated on recorded ``ArbiterStatus``).

Detectors (findings, not invariants):

``nonfinite_input``, ``malformed_input``
    a command source message with a NaN/Inf or non-numeric twist component
``command_source_stale``
    a source whose latest command was nonzero has been silent past its
    window. A source whose latest command was zero going silent is ordinary
    inactivity and is not reported.
``motion_request_while_held`` (info)
    a source commands motion while a hold is asserted
``stamp_ahead``, ``stamp_behind``
    a topic's (receipt wall time - DDS source time) moved by more than the
    threshold from its own baseline: the publisher clock stepped, the
    receiver clock stepped, or the message was delivered late or re-delivered.
    Which one is not decidable from one topic; the finding says so.
``clock_offset`` (info)
    a host's publisher clock differs from the receiver's by more than the
    threshold from the start (a known condition on the GO2, harmless when
    freshness uses receipt time)
``odometry_frozen``, ``odometry_jump``, ``nonfinite_telemetry``
    odometry position that does not move while its own twist says the robot
    moves, position steps larger than its twist allows, or non-finite values.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.sut import Decision
from blackboxrs.lab.values import NS, as_number, get_path

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Finding:
    t_ns: int
    monitor: str
    kind: str
    severity: str
    subject: str
    message: str
    evidence: tuple[str, ...] = ()
    invariant: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[Any, ...]:
        return (self.t_ns, self.monitor, self.kind, self.subject, self.evidence)


@dataclass
class InvariantStatus:
    name: str
    statement: str
    checks: int = 0
    violations: int = 0
    exercised: bool = False
    first_violation_t_ns: int | None = None
    episodes: int = 0
    incomplete_reason: str | None = None

    def status(self) -> str:
        if self.violations:
            return "FAIL"
        if self.incomplete_reason:
            return "INCOMPLETE"
        if not self.exercised:
            return "NOT_EXERCISED"
        return "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status(), "statement": self.statement, "checks": self.checks,
                "violating_ticks": self.violations, "episodes": self.episodes,
                "first_violation_t_ns": self.first_violation_t_ns,
                "incomplete_reason": self.incomplete_reason}


class Monitor:
    name = "monitor"

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        return []

    def on_decision(self, d: Decision) -> list[Finding]:
        return []

    def finish(self, t_end_ns: int) -> list[Finding]:
        return []

    def invariants(self) -> list[InvariantStatus]:
        return []


def _hold_key(d: dict[str, Any]) -> tuple[int, int] | None:
    ep, sq = d.get("epoch"), d.get("seq")
    if all(isinstance(x, int) and not isinstance(x, bool) for x in (ep, sq)):
        return (ep, sq)
    return None


def _nonzero(raw: tuple[Any, ...] | None) -> bool:
    """A robot-facing command that is not exactly zero. Non-numbers count."""
    if raw is None:
        return False
    for v in raw:
        n, problem = as_number(v)
        if problem or n != 0.0:
            return True
    return False


_AXES = ("linear.x", "linear.y", "linear.z", "angular.x", "angular.y", "angular.z")


def _twist_values(data: dict[str, Any]) -> tuple[tuple[float, float, float], bool]:
    """(vx, vy, wz), and whether any of the six components is not a finite number.

    Parsed here rather than shared with the arbitration model, so a parsing
    bug in one cannot hide the same bug in the other.
    """
    vals = {a: as_number(get_path(data, a)[1]) for a in _AXES}
    bad = any(p for _, p in vals.values())
    pick = tuple((vals[a][0] if not vals[a][1] else 0.0) for a in
                 ("linear.x", "linear.y", "angular.z"))
    return pick, bad  # type: ignore[return-value]


def twist_problem(data: dict[str, Any]) -> tuple[str, list[str]]:
    """('', []) when all six components are finite numbers, else the worst problem."""
    bad: list[str] = []
    worst = ""
    for name in _AXES:
        _, p = as_number(get_path(data, name)[1])
        if p:
            bad.append(name)
            worst = "malformed" if p == "malformed" or worst == "malformed" else "nonfinite"
    return worst, bad


# ---------------------------------------------------------------------------
# stop dominance
# ---------------------------------------------------------------------------


class _HoldTracker:
    """HELIX hold state as one stream of hold messages shows it.

    Ordered by the publisher's ``(epoch, seq)``: an older state is ignored
    while the current one is fresh; once it is stale (no hold message for
    ``timeout``) any state is accepted, as HELIX P8 does for a restarted
    publisher.
    """

    def __init__(self, timeout_ns: int) -> None:
        self.timeout_ns = timeout_ns
        self.key: tuple[int, int] | None = None
        self.last_rx: int | None = None
        self.held = False
        self.assert_t: int | None = None
        self.assert_eid = ""
        self.fault_id = ""
        self.ignored_older = 0
        self.ever_asserted = False

    def update(self, e: ReplayEvent) -> bool:
        """Apply one hold message; True if the held state changed."""
        d = e.data or {}
        hold = d.get("hold")
        if not isinstance(hold, bool):
            return False
        key = _hold_key(d)
        fresh = self.last_rx is not None and e.t_ns - self.last_rx <= self.timeout_ns
        if key is not None and self.key is not None and key <= self.key and fresh:
            self.ignored_older += 1
            return False
        self.key = key if key is not None else self.key
        self.last_rx = e.t_ns
        if hold and not self.held:
            self.held, self.assert_t, self.assert_eid = True, e.t_ns, e.eid
            self.fault_id = str(d.get("fault_id", ""))
            self.ever_asserted = True
            return True
        if not hold and self.held:
            self.held = False
            return True
        return False


class StopDominance(Monitor):
    """The hold is judged from two streams and the stricter one wins.

    ``delivered``: the hold messages the system under test received (after
    fault injection). ``recorded``: the hold messages in the evidence as
    recorded, before any fault. A fault that deletes, delays, reorders or
    rewrites the STOP signal therefore cannot make the oracle forget a STOP
    the evidence contains: the robot-facing command must still be zero.
    """

    name = "stop_dominance"

    def __init__(self, hold_topics: set[str], source_topics: set[str], grace_s: float,
                 hold_timeout_s: float = 0.5) -> None:
        self.hold_topics = hold_topics
        self.source_topics = source_topics
        self.grace_ns = int(round(grace_s * NS))
        timeout = int(round(hold_timeout_s * NS))
        self.delivered = _HoldTracker(timeout)
        self.recorded = _HoldTracker(timeout)
        self.inv = InvariantStatus(
            "stop_dominance",
            f"while a HELIX hold is asserted (in the delivered stream or in the evidence as "
            f"recorded), every robot-facing command from {grace_s:g} s after the hold is "
            "received until its release is zero")
        self._in_violation = False
        self._asked: set[str] = set()

    @property
    def ignored_older(self) -> int:
        return self.delivered.ignored_older

    @property
    def ever_asserted(self) -> bool:
        return self.delivered.ever_asserted or self.recorded.ever_asserted

    def _active(self) -> _HoldTracker | None:
        held = [t for t in (self.delivered, self.recorded) if t.held]
        return min(held, key=lambda t: t.assert_t) if held else None

    def on_recorded(self, e: ReplayEvent) -> None:
        """A hold message from the evidence as recorded (not the faulted stream)."""
        if e.kind == "msg" and e.topic in self.hold_topics:
            if self.recorded.update(e) and not self._active():
                self._in_violation = False

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        out: list[Finding] = []
        if e.kind != "msg" or e.data is None:
            return out
        if e.topic in self.hold_topics:
            if self.delivered.update(e):
                if self.delivered.held:
                    self._asked.clear()
                elif not self._active():
                    self._in_violation = False
        else:
            act = self._active()
            if act is not None and e.topic in self.source_topics:
                vals, bad = _twist_values(e.data)
                if not bad and any(v != 0.0 for v in vals) and e.topic not in self._asked:
                    self._asked.add(e.topic)
                    out.append(Finding(
                        e.t_ns, self.name, "motion_request_while_held", "info", e.topic,
                        f"{e.topic} commands motion while hold {act.fault_id or '(no id)'} "
                        "is asserted", (e.eid, act.assert_eid)))
        return out

    def on_decision(self, d: Decision) -> list[Finding]:
        act = self._active()
        if act is None or act.assert_t is None or d.t_ns < act.assert_t + self.grace_ns:
            return []
        self.inv.exercised = True
        self.inv.checks += 1
        if not _nonzero(d.robot_raw):
            self._in_violation = False
            return []
        self.inv.violations += 1
        if self.inv.first_violation_t_ns is None:
            self.inv.first_violation_t_ns = d.t_ns
        if self._in_violation:
            return []
        self._in_violation = True
        self.inv.episodes += 1
        which = "recorded" if act is self.recorded and not self.delivered.held else "delivered"
        note = (" (the hold is in the evidence as recorded; the replayed stream lost or "
                "altered it)" if which == "recorded" else "")
        return [Finding(
            d.t_ns, self.name, "stop_violated", "critical", "robot_output",
            f"robot-facing command {list(d.robot_raw or ())} is nonzero while hold "
            f"{act.fault_id or '(no id)'} is asserted (winner: {d.source or 'none'}){note}",
            tuple(x for x in (act.assert_eid, d.cause) if x and x != "clock"),
            invariant="stop_dominance",
            data={"robot_command": list(d.robot_raw or ()), "arbiter_reason": d.reason,
                  "winner": d.source, "hold_stream": which})]

    def finish(self, t_end_ns: int) -> list[Finding]:
        if self.ever_asserted and not self.inv.checks:
            self.inv.incomplete_reason = ("a hold was asserted but no robot-facing command was "
                                          "sampled after it")
        return []

    def invariants(self) -> list[InvariantStatus]:
        return [self.inv]


# ---------------------------------------------------------------------------
# command freshness, finiteness, input validity
# ---------------------------------------------------------------------------


@dataclass
class _Latest:
    t_ns: int
    eid: str
    valid: bool
    cmd: tuple[float, float, float] | None
    stale_reported: bool = False


class CommandPath(Monitor):
    name = "command_path"

    def __init__(self, sources: dict[str, float], grace_s: float) -> None:
        self.sources = {t: int(round(s * NS)) for t, s in sorted(sources.items())}
        self.grace_ns = int(round(grace_s * NS))
        self.latest: dict[str, _Latest] = {}
        # Messages a newer one replaced, with the time they were replaced;
        # each can justify an output for ``grace`` after that (propagation).
        self.replaced: dict[str, list[tuple[_Latest, int]]] = {}
        self._bad_run: dict[str, bool] = {}
        self.fresh = InvariantStatus(
            "fresh_output",
            "a nonzero robot-facing command equals the latest valid message of a command "
            f"source received within that source's freshness window (+{grace_s:g} s), or one "
            f"it replaced less than {grace_s:g} s ago")
        self.finite = InvariantStatus(
            "finite_output", "every robot-facing command component is a finite number")
        self._stale_ep = False
        self._nonfinite_ep = False
        self.outputs_seen = 0

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        if e.kind != "msg" or e.topic not in self.sources or e.data is None:
            return []
        worst, bad = twist_problem(e.data)
        out: list[Finding] = []
        if worst:
            self._set_latest(e.topic, _Latest(e.t_ns, e.eid, False, None), e.t_ns)
            if not self._bad_run.get(e.topic):
                self._bad_run[e.topic] = True
                out.append(Finding(
                    e.t_ns, self.name, f"{worst}_input", "warning", e.topic,
                    f"{e.topic} carries {'NaN/Inf' if worst == 'nonfinite' else 'non-numeric'} "
                    f"value(s) in {', '.join(bad)}", (e.eid,), data={"fields": bad}))
            return out
        self._bad_run[e.topic] = False
        vals, _ = _twist_values(e.data)
        self._set_latest(e.topic, _Latest(e.t_ns, e.eid, True, vals), e.t_ns)
        return out

    def _set_latest(self, topic: str, new: _Latest, t_ns: int) -> None:
        prev = self.latest.get(topic)
        kept = [(m, at) for m, at in self.replaced.get(topic, [])
                if at >= t_ns - self.grace_ns]
        if prev is not None:
            kept.append((prev, t_ns))
        self.replaced[topic] = kept
        self.latest[topic] = new

    def _justified(self, cmd: tuple[float, float, float], t_ns: int) -> str | None:
        def backs(m: _Latest, window: int) -> bool:
            return (m.valid and m.cmd is not None and t_ns - m.t_ns <= window + self.grace_ns
                    and all(abs(a - b) <= 1e-9 for a, b in zip(m.cmd, cmd)))
        for topic, window in self.sources.items():
            last = self.latest.get(topic)
            if last is not None and backs(last, window):
                return topic
            # a message replaced less than grace ago: the arbiter has not yet
            # had a tick to publish its successor
            if any(at >= t_ns - self.grace_ns and backs(m, window)
                   for m, at in self.replaced.get(topic, [])):
                return topic
        return None

    def on_decision(self, d: Decision) -> list[Finding]:
        out: list[Finding] = []
        t = d.t_ns
        # sources that went silent while their last command was motion
        for topic, window in self.sources.items():
            last = self.latest.get(topic)
            if (last is not None and last.valid and last.cmd is not None
                    and any(v != 0.0 for v in last.cmd) and not last.stale_reported
                    and t - last.t_ns > window + self.grace_ns):
                last.stale_reported = True
                out.append(Finding(
                    t, self.name, "command_source_stale", "warning", topic,
                    f"{topic} last commanded {list(last.cmd)} and has been silent "
                    f"{(t - last.t_ns) / NS:.3f} s (window {window / NS:g} s)", (last.eid,),
                    data={"last_command": list(last.cmd), "silent_s": (t - last.t_ns) / NS}))
        if d.robot_raw is None:
            return out
        self.outputs_seen += 1
        # finite
        self.finite.exercised = True
        self.finite.checks += 1
        nums = [as_number(v) for v in d.robot_raw]
        if any(p for _, p in nums):
            self.finite.violations += 1
            if self.finite.first_violation_t_ns is None:
                self.finite.first_violation_t_ns = t
            if not self._nonfinite_ep:
                self._nonfinite_ep = True
                self.finite.episodes += 1
                out.append(Finding(
                    t, self.name, "nonfinite_output", "critical", "robot_output",
                    f"robot-facing command {list(d.robot_raw)} is not finite",
                    tuple(x for x in (d.cause,) if x != "clock"), invariant="finite_output",
                    data={"robot_command": list(d.robot_raw)}))
            return out
        self._nonfinite_ep = False
        # fresh
        cmd = tuple(n for n, _ in nums)
        if all(v == 0.0 for v in cmd):
            self._stale_ep = False
            return out
        self.fresh.exercised = True
        self.fresh.checks += 1
        if self._justified(cmd, t) is not None:
            self._stale_ep = False
            return out
        self.fresh.violations += 1
        if self.fresh.first_violation_t_ns is None:
            self.fresh.first_violation_t_ns = t
        if self._stale_ep:
            return out
        self._stale_ep = True
        self.fresh.episodes += 1
        match = [(tp, lt) for tp, lt in sorted(self.latest.items())
                 if lt.cmd is not None and all(abs(a - b) <= 1e-9 for a, b in zip(lt.cmd, cmd))]
        ages = {tp: (t - lt.t_ns) / NS for tp, lt in match}
        ev = tuple([lt.eid for _, lt in match] + ([d.cause] if d.cause != "clock" else []))
        out.append(Finding(
            t, self.name, "stale_command_forwarded", "critical", "robot_output",
            f"robot-facing command {list(cmd)} is not backed by a fresh valid source message"
            + (f" (matches {', '.join(f'{k} aged {v:.3f} s' for k, v in ages.items())})"
               if ages else ""), ev, invariant="fresh_output",
            data={"robot_command": list(cmd), "matching_source_age_s": ages}))
        return out

    def finish(self, t_end_ns: int) -> list[Finding]:
        if not self.outputs_seen:
            reason = "no robot-facing command was observed"
            self.fresh.incomplete_reason = reason
            self.finite.incomplete_reason = reason
        return []

    def invariants(self) -> list[InvariantStatus]:
        return [self.finite, self.fresh]


# ---------------------------------------------------------------------------
# arbiter self-consistency (recorded status)
# ---------------------------------------------------------------------------


class ConsistentState(Monitor):
    name = "consistent_state"

    def __init__(self, status_topics: set[str]) -> None:
        self.status_topics = status_topics
        self.inv = InvariantStatus(
            "consistent_state",
            "an arbiter status never reports the hold active together with a nonzero output")
        self._ep = False

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        if e.kind != "msg" or e.topic not in self.status_topics or e.data is None:
            return []
        d = e.data
        self.inv.exercised = True
        self.inv.checks += 1
        raw = (d.get("out_linear_x"), d.get("out_linear_y"), d.get("out_angular_z"))
        if d.get("hold_active") is True and _nonzero(raw):
            self.inv.violations += 1
            if self.inv.first_violation_t_ns is None:
                self.inv.first_violation_t_ns = e.t_ns
            if not self._ep:
                self._ep = True
                self.inv.episodes += 1
                return [Finding(e.t_ns, self.name, "contradictory_state", "critical", e.topic,
                                f"{e.topic} reports hold_active with output {list(raw)}",
                                (e.eid,), invariant="consistent_state",
                                data={"out": list(raw), "reason": d.get("reason")})]
            return []
        self._ep = False
        return []

    def invariants(self) -> list[InvariantStatus]:
        return [self.inv]


# ---------------------------------------------------------------------------
# publisher timestamps vs receipt
# ---------------------------------------------------------------------------


class ClockMonitor(Monitor):
    name = "clock"
    BASELINE_N = 5

    def __init__(self, hosts: dict[str, str], step_threshold_s: float,
                 offset_info_s: float) -> None:
        self.hosts = hosts
        self.thr = int(round(step_threshold_s * NS))
        self.offset_info = int(round(offset_info_s * NS))
        self._samples: dict[str, list[int]] = {}
        self.baseline: dict[str, int] = {}
        self._baseline_t: dict[str, int] = {}
        self._episode: dict[str, str] = {}
        self.untimed: set[str] = set()

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        if e.kind != "msg" or not e.topic:
            return []
        if e.src_ns is None or e.rx_wall_ns is None:
            self.untimed.add(e.topic)
            return []
        lat = e.rx_wall_ns - e.src_ns
        topic = e.topic
        if topic not in self.baseline:
            s = self._samples.setdefault(topic, [])
            s.append(lat)
            if len(s) >= self.BASELINE_N:
                self.baseline[topic] = int(statistics.median_low(s))
                self._baseline_t[topic] = e.t_ns
            return []
        dev = lat - self.baseline[topic]
        state = "ahead" if dev < -self.thr else "behind" if dev > self.thr else ""
        prev = self._episode.get(topic, "")
        self._episode[topic] = state
        if not state or state == prev:
            return []
        why = ("the publisher clock stepped forward, or the receiver clock stepped back"
               if state == "ahead" else
               "late or repeated delivery, the publisher clock stepped back, or the receiver "
               "clock stepped forward")
        return [Finding(
            e.t_ns, self.name, f"stamp_{state}", "warning", topic,
            f"{topic}: receipt minus source time moved {dev / NS:+.3f} s from its baseline "
            f"({self.baseline[topic] / NS:+.3f} s); {why}", (e.eid,),
            data={"deviation_s": dev / NS, "baseline_s": self.baseline[topic] / NS,
                  "host": self.hosts.get(topic, "unknown")})]

    def finish(self, t_end_ns: int) -> list[Finding]:
        out = []
        by_host: dict[str, list[str]] = {}
        for topic in sorted(self.baseline):
            by_host.setdefault(self.hosts.get(topic, "unknown"), []).append(topic)
        for host, topics in sorted(by_host.items()):
            off = statistics.median_low([-self.baseline[t] for t in topics])
            if abs(off) > self.offset_info:
                t0 = min(self._baseline_t[t] for t in topics)
                out.append(Finding(
                    t0, self.name, "clock_offset", "info", host,
                    f"host {host}: publisher clock is {off / NS:+.3f} s from the receiver clock "
                    "from the start of the evidence (constant offset, not a step)",
                    data={"offset_s": off / NS, "topics": topics}))
        return out

    def summary(self) -> dict[str, Any]:
        return {"baseline_receipt_minus_source_s": {t: v / NS
                                                     for t, v in sorted(self.baseline.items())},
                "topics_without_source_timestamps": sorted(self.untimed)}


# ---------------------------------------------------------------------------
# odometry consistency
# ---------------------------------------------------------------------------


class OdometryConsistency(Monitor):
    name = "odometry"

    def __init__(self, topics: set[str], moving_mps: float = 0.05, frozen_samples: int = 5,
                 jump_tolerance_m: float = 0.1) -> None:
        self.topics = topics
        self.moving = moving_mps
        self.frozen_n = frozen_samples
        self.tol = jump_tolerance_m
        self._prev: dict[str, tuple[float, float, float, float]] = {}
        self._frozen: dict[str, int] = {}
        self._bad: dict[str, bool] = {}
        self._jump: dict[str, bool] = {}

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        if e.kind != "msg" or e.topic not in self.topics or e.data is None:
            return []
        topic = e.topic
        vals = {}
        problems = []
        for key, path in (("x", "pose.pose.position.x"), ("y", "pose.pose.position.y"),
                          ("vx", "twist.twist.linear.x"), ("vy", "twist.twist.linear.y")):
            n, p = as_number(get_path(e.data, path)[1])
            vals[key] = n
            if p:
                problems.append(path)
        if problems:
            self._prev.pop(topic, None)
            if not self._bad.get(topic):
                self._bad[topic] = True
                return [Finding(e.t_ns, self.name, "nonfinite_telemetry", "warning", topic,
                                f"{topic}: {', '.join(problems)} not a finite number",
                                (e.eid,), data={"fields": problems})]
            return []
        self._bad[topic] = False
        t = e.pub_stamp_s if e.pub_stamp_s is not None else e.t_ns / NS
        cur = (t, vals["x"], vals["y"], (vals["vx"] ** 2 + vals["vy"] ** 2) ** 0.5)
        prev, self._prev[topic] = self._prev.get(topic), cur
        if prev is None or cur[0] <= prev[0]:
            return []
        dt = cur[0] - prev[0]
        dp = ((cur[1] - prev[1]) ** 2 + (cur[2] - prev[2]) ** 2) ** 0.5
        v = (prev[3] + cur[3]) / 2
        out = []
        if dp > 2 * v * dt + self.tol:
            if not self._jump.get(topic):
                out.append(Finding(
                    e.t_ns, self.name, "odometry_jump", "warning", topic,
                    f"{topic}: position moved {dp:.3f} m in {dt:.3f} s while its twist says "
                    f"{v:.3f} m/s", (e.eid,), data={"moved_m": dp, "dt_s": dt, "speed_mps": v}))
            self._jump[topic] = True
        else:
            self._jump[topic] = False
        if dp < 1e-9 and v > self.moving:
            self._frozen[topic] = self._frozen.get(topic, 0) + 1
            if self._frozen[topic] == self.frozen_n:
                out.append(Finding(
                    e.t_ns, self.name, "odometry_frozen", "warning", topic,
                    f"{topic}: position unchanged for {self.frozen_n} samples while its twist "
                    f"says {v:.3f} m/s (stuck sensor or frozen publisher)", (e.eid,),
                    data={"samples": self.frozen_n, "speed_mps": v}))
        else:
            self._frozen[topic] = 0
        return out
