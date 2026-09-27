"""The system under test: what command reaches the robot-facing boundary.

Two modes:

``observed``
    The arbitration output recorded in the evidence (``/cmd_vel`` when the
    profile subscribed to it, otherwise the ``ArbiterStatus.out_*`` fields,
    which is what the arbiter published on ``/cmd_vel``). Faults on input
    topics cannot change a recorded output; use this mode to check recorded
    incidents and to inject faults into the recorded output itself.

``reference``
    A model of the arbitration path is run on the replayed (and possibly
    faulted) inputs, so an input fault can change the outcome. Recorded
    outputs are suppressed and counted. Two presets:

    * ``helix_arbiter``: the policies of HELIX ``helix_arbiter/arbiter_core.py``
      (P1-P9 in HELIX docs/MOTION_ARBITRATION.md): the hold state dominates
      every source, a stale or missing hold state forces zero, non-finite or
      over-limit input is rejected and discards that source's previous
      command, a hold transition discards every stored command, the output
      is published on a fixed timer and is zero when no fresh source exists.
      Freshness uses the arbiter's receipt clock (P10). This is a model of
      the documented policy, not the HELIX code; ``tests/unit/lab/
      test_helix_parity.py`` compares it decision by decision against the
      HELIX module when a HELIX checkout is available.
    * ``twist_mux_legacy``: the behaviour measured on the real twist_mux
      4.3.0 binary and recorded in HELIX docs/MOTION_ARBITRATION.md: HELIX's
      stop is a zero-twist input at priority 100 below teleop at 200; NaN is
      forwarded; the output is published only from an input callback, so
      when every input goes stale nothing is published. The robot-facing
      consumer keeps its last command (same document), which the model
      represents as a sink that holds the last published command.

    ``freshness_clock: source_timestamp`` is a counterfactual override that
    judges source freshness by the publisher's DDS source timestamp against
    the arbiter host's wall clock (what a stamp-based freshness check does).
    HELIX P10 rejects exactly this; the override exists to show why.

Everything is on integer nanoseconds of the replay clock. The caller
supplies time on every call; nothing here reads a clock.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.evidence import EvidenceError
from blackboxrs.lab.values import NS, as_number, get_path

REASON_SOURCE = "SOURCE"
REASON_HOLD = "HELIX_HOLD"
REASON_STALE = "HELIX_STATE_STALE"
REASON_MISSING = "HELIX_STATE_MISSING"
REASON_NO_INPUT = "NO_LIVE_INPUT"
REASON_SILENT = "SILENT"


@dataclass(frozen=True)
class Command:
    vx: float
    vy: float
    wz: float

    def is_zero(self) -> bool:
        return self.vx == 0.0 and self.vy == 0.0 and self.wz == 0.0

    def as_list(self) -> list[float]:
        return [self.vx, self.vy, self.wz]


ZERO = Command(0.0, 0.0, 0.0)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    topic: str
    priority: int
    timeout_s: float


@dataclass(frozen=True)
class ArbiterConfig:
    preset: str
    sources: tuple[SourceSpec, ...]
    hold_topic: str = "/helix/hold"
    hold_timeout_s: float = 0.5
    period_s: float = 0.02
    max_abs_linear: float = 1.0
    max_abs_angular: float = 1.5
    # semantics
    stop_mode: str = "state"            # state: hold dominates | source: zero-twist input
    stop_priority: int = 100
    stop_timeout_s: float = 0.5
    nonfinite: str = "reject"           # reject | forward
    publish: str = "timer"              # timer | on_input
    freshness_clock: str = "receipt"    # receipt | source_timestamp

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sources"] = [asdict(s) for s in self.sources]
        return d


HELIX_SOURCES = (SourceSpec("teleop", "/teleop/cmd_vel", 200, 0.5),
                 SourceSpec("nav", "/nav/cmd_vel", 50, 0.5))

PRESETS: dict[str, ArbiterConfig] = {
    # HELIX src/helix_arbiter/config/arbiter.yaml: teleop 200, nav 50, 0.5 s,
    # hold_timeout 0.5 s, 50 Hz timer, limits 1.0 m/s and 1.5 rad/s.
    "helix_arbiter": ArbiterConfig(preset="helix_arbiter", sources=HELIX_SOURCES),
    # HELIX config/twist_mux.yaml: teleop 200 > helix_recovery 100 > navigation 50.
    "twist_mux_legacy": ArbiterConfig(
        preset="twist_mux_legacy", sources=HELIX_SOURCES, stop_mode="source",
        stop_priority=100, stop_timeout_s=0.5, nonfinite="forward", publish="on_input"),
}

_OVERRIDABLE = {"sources", "hold_topic", "hold_timeout_s", "period_s", "max_abs_linear",
                "max_abs_angular", "stop_priority", "stop_timeout_s", "freshness_clock"}


def build_config(preset: str, overrides: dict[str, Any] | None = None) -> ArbiterConfig:
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; known: {sorted(PRESETS)}")
    cfg = PRESETS[preset]
    o = dict(overrides or {})
    bad = sorted(set(o) - _OVERRIDABLE)
    if bad:
        raise ValueError(f"sut overrides: unknown or fixed keys {bad} "
                         f"(overridable: {sorted(_OVERRIDABLE)})")
    if "sources" in o:
        o["sources"] = tuple(SourceSpec(str(s["name"]), str(s["topic"]), int(s["priority"]),
                                        float(s["timeout_s"])) for s in o["sources"])
    if o.get("freshness_clock", "receipt") not in ("receipt", "source_timestamp"):
        raise ValueError("freshness_clock must be receipt or source_timestamp")
    cfg = replace(cfg, **o)
    names = [s.name for s in cfg.sources]
    if not cfg.sources or len(set(names)) != len(names):
        raise ValueError(f"sources must be non-empty with unique names: {names}")
    if any(s.timeout_s <= 0 for s in cfg.sources) or cfg.hold_timeout_s <= 0:
        raise ValueError("every timeout must be > 0")
    if cfg.period_s <= 0:
        raise ValueError("period_s must be > 0")
    return cfg


# ---------------------------------------------------------------------------
# output of one tick
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """Arbitration state at one instant, and what the robot-facing sink holds.

    ``robot_raw`` is the command in effect at the robot-facing boundary, as
    published (it may hold NaN or a non-number when an arbiter forwards
    one): for a timer publisher the latest decision; for a callback
    publisher the last command it published, which the sink keeps.
    ``robot_cmd`` is the same command when every axis is a finite number,
    else ``None``. Both are ``None`` before anything was published.
    ``cause`` is the id of the input event that determines the command (the
    winning source's message, the hold message, a rejected message, or for
    a recorded output the output message), or ``clock`` when the decision
    follows from time alone (a timeout, or nothing received yet).
    """

    t_ns: int
    reason: str
    source: str
    robot_cmd: Command | None
    robot_raw: tuple[Any, ...] | None
    published: bool
    hold: bool | None
    cause: str


def twist_of(data: dict[str, Any]) -> tuple[tuple[Any, Any, Any], tuple[Any, Any, Any]]:
    lin = tuple(get_path(data, f"linear.{a}")[1] for a in "xyz")
    ang = tuple(get_path(data, f"angular.{a}")[1] for a in "xyz")
    return lin, ang  # type: ignore[return-value]


def _hold_fields(e: ReplayEvent) -> tuple[bool, str, int, int] | None:
    d = e.data or {}
    hold, epoch, seq = d.get("hold"), d.get("epoch"), d.get("seq")
    if not isinstance(hold, bool):
        return None
    ok = all(isinstance(x, int) and not isinstance(x, bool) for x in (epoch, seq))
    return hold, str(d.get("fault_id", "")), (epoch if ok else 0), (seq if ok else 0)


# ---------------------------------------------------------------------------
# reference model
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    spec: SourceSpec
    cmd: Command | None = None
    raw: tuple[Any, ...] | None = None   # forwarded payload (legacy, may be non-finite)
    rx_ns: int | None = None
    fresh_ref_ns: int | None = None      # time freshness is measured from
    order: int = -1
    eid: str = ""


@dataclass
class _Hold:
    hold: bool
    fault_id: str
    epoch: int
    seq: int
    rx_ns: int
    eid: str


@dataclass
class ReferenceArbiter:
    cfg: ArbiterConfig
    wall0_ns: int = 0                    # arbiter host wall clock at replay t=0
    slots: dict[str, _Slot] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=lambda: {
        "rejected": 0, "hold_reordered": 0, "hold_transitions": 0, "hold_malformed": 0,
        "published": 0})

    def __post_init__(self) -> None:
        self.slots = {s.name: _Slot(s) for s in self.cfg.sources}
        if self.cfg.stop_mode == "source":
            spec = SourceSpec("helix_recovery", self.cfg.hold_topic, self.cfg.stop_priority,
                              self.cfg.stop_timeout_s)
            self.slots[spec.name] = _Slot(spec)
        self._by_topic = {s.spec.topic: s for s in self.slots.values()}
        self._hold: _Hold | None = None
        self._order = 0
        self._sink: Command | None = None
        self._sink_raw: tuple[Any, ...] | None = None
        self._sink_eid = ""
        self._pending_pub = ""
        self._published_now = False
        self._rejected = ""

    # -- inputs --------------------------------------------------------------

    @property
    def input_topics(self) -> set[str]:
        return set(self._by_topic) | {self.cfg.hold_topic}

    def _fresh_ref(self, e: ReplayEvent, t_ns: int) -> int:
        if self.cfg.freshness_clock == "source_timestamp" and e.src_ns is not None:
            # the publisher's clock mapped onto the replay clock through the
            # arbiter host's wall clock: skew between the two hosts shifts it
            return e.src_ns - self.wall0_ns
        return t_ns

    def on_event(self, e: ReplayEvent, t_ns: int) -> None:
        if e.kind != "msg":
            return
        if e.topic == self.cfg.hold_topic:
            self._on_hold(e, t_ns)
            if self.cfg.stop_mode != "source":
                return
        slot = self._by_topic.get(e.topic or "")
        if slot is None:
            return
        if e.data is None:
            raise EvidenceError(f"{e.eid} on {e.topic}: payload not stored; the reference "
                                "arbiter cannot replay a command it cannot read")
        if slot.spec.name == "helix_recovery":
            fields = _hold_fields(e)
            if fields is None or not fields[0]:
                return   # only an asserted hold is a zero-twist input on this path
            lin, ang = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        else:
            lin, ang = twist_of(e.data)
        self._on_source(slot, lin, ang, e, t_ns)

    def _on_source(self, slot: _Slot, lin, ang, e: ReplayEvent, t_ns: int) -> None:
        nums = [as_number(v) for v in (*lin, *ang)]
        bad = any(p for _, p in nums)
        if self.cfg.nonfinite == "reject":
            if bad or not self._within_limits(nums):
                slot.cmd = slot.raw = None      # P4: never fall back to an older value
                slot.rx_ns = slot.fresh_ref_ns = None
                self.counters["rejected"] += 1
                self._rejected = e.eid
                return
            cmd = Command(nums[0][0] + 0.0, nums[1][0] + 0.0, nums[5][0] + 0.0)
            raw = tuple(cmd.as_list())
        else:
            # twist_mux forwards the payload unchanged, NaN and garbage included
            cmd = None if bad else Command(nums[0][0] + 0.0, nums[1][0] + 0.0, nums[5][0] + 0.0)
            raw = (lin[0], lin[1], ang[2])
        self._order += 1
        slot.cmd, slot.raw, slot.rx_ns, slot.order, slot.eid = cmd, raw, t_ns, self._order, e.eid
        slot.fresh_ref_ns = self._fresh_ref(e, t_ns)
        if self.cfg.publish == "on_input":
            win = self._winner(t_ns)
            if win is slot:
                self._publish(slot.cmd, slot.raw, e.eid)

    def _within_limits(self, nums) -> bool:
        lx, ly, wz = nums[0][0], nums[1][0], nums[5][0]
        return (abs(lx) <= self.cfg.max_abs_linear and abs(ly) <= self.cfg.max_abs_linear
                and abs(wz) <= self.cfg.max_abs_angular)

    def _on_hold(self, e: ReplayEvent, t_ns: int) -> None:
        if e.data is None:
            raise EvidenceError(f"{e.eid} on {e.topic}: hold payload not stored")
        fields = _hold_fields(e)
        if fields is None:
            self.counters["hold_malformed"] += 1
            return
        if self.cfg.stop_mode == "source":
            return   # legacy path: the hold only feeds the zero-twist input
        hold, fid, epoch, seq = fields
        cur = self._hold
        if cur is not None and self._hold_fresh(t_ns) and (epoch, seq) <= (cur.epoch, cur.seq):
            self.counters["hold_reordered"] += 1     # P8
            return
        before = self._effective_hold(t_ns)
        self._hold = _Hold(hold, fid, epoch, seq, t_ns, e.eid)
        if self._effective_hold(t_ns) != before:
            self._clear()                             # P7
            self.counters["hold_transitions"] += 1

    # -- decision ------------------------------------------------------------

    def _hold_fresh(self, t_ns: int) -> bool:
        return (self._hold is not None
                and t_ns - self._hold.rx_ns <= int(round(self.cfg.hold_timeout_s * NS)))

    def _effective_hold(self, t_ns: int) -> bool:
        return self._hold is None or not self._hold_fresh(t_ns) or self._hold.hold

    def _clear(self) -> None:
        for s in self.slots.values():
            s.cmd = s.raw = None
            s.rx_ns = s.fresh_ref_ns = None

    def _fresh(self, s: _Slot, t_ns: int) -> bool:
        if s.raw is None or s.fresh_ref_ns is None:
            return False
        return t_ns - s.fresh_ref_ns <= int(round(s.spec.timeout_s * NS))

    def _winner(self, t_ns: int) -> _Slot | None:
        live = [s for s in self.slots.values() if self._fresh(s, t_ns)]
        if not live:
            return None
        return max(live, key=lambda s: (s.spec.priority, s.order))

    def _publish(self, cmd: Command | None, raw: tuple[Any, ...] | None,
                 eid: str = "") -> None:
        self._sink, self._sink_raw, self._sink_eid = cmd, raw, eid
        self._published_now = True
        self.counters["published"] += 1
        if eid:
            self._pending_pub = eid

    def take_publication(self, t_ns: int) -> Decision | None:
        """A command published from an input callback at ``t_ns``, if any.

        Only the callback (``on_input``) publisher puts commands on the robot
        between ticks; the timer publisher decides at ticks only.
        """
        eid, self._pending_pub = self._pending_pub, ""
        if not eid:
            return None
        win = self._winner(t_ns)
        return Decision(t_ns, REASON_SOURCE, "" if win is None else win.spec.name, self._sink,
                        self._sink_raw, True, None, eid)

    def tick(self, t_ns: int) -> Decision:
        rejected, self._rejected = self._rejected, ""
        if self.cfg.stop_mode == "state":
            return self._tick_state(t_ns, rejected)
        return self._tick_source(t_ns)

    def _tick_state(self, t_ns: int, cause: str) -> Decision:
        # The decision's cause is the input that determines it: the winning
        # source's message, the hold message, a just-rejected message, or
        # the clock (a timeout, or nothing received yet).
        if self._effective_hold(t_ns):
            self._clear()
        hold = None if self._hold is None else self._hold.hold
        if self._hold is None:
            reason, src, cmd, why = REASON_MISSING, "", ZERO, "clock"
        elif not self._hold_fresh(t_ns):
            reason, src, cmd, why = REASON_STALE, "", ZERO, "clock"
        elif self._hold.hold:
            reason, src, cmd, why = REASON_HOLD, "", ZERO, self._hold.eid
        else:
            win = self._winner(t_ns)
            if win is None:
                reason, src, cmd, why = REASON_NO_INPUT, "", ZERO, cause or "clock"
            else:
                reason, src, cmd, why = REASON_SOURCE, win.spec.name, win.cmd, win.eid
        self._publish(cmd, tuple(cmd.as_list()))
        self._published_now = False
        return Decision(t_ns, reason, src, cmd, tuple(cmd.as_list()), True, hold, why)

    def _tick_source(self, t_ns: int) -> Decision:
        win = self._winner(t_ns)
        published, self._published_now = self._published_now, False
        reason = REASON_SOURCE if win is not None else REASON_SILENT
        return Decision(t_ns, reason, "" if win is None else win.spec.name, self._sink,
                        self._sink_raw, published, None,
                        (self._sink_eid or "clock") if published or win is not None
                        else "clock")

    def state(self) -> dict[str, Any]:
        return {"counters": dict(self.counters)}


# ---------------------------------------------------------------------------
# observed outputs
# ---------------------------------------------------------------------------


class ObservedOutput:
    """The recorded arbitration output, sampled at every tick.

    Prefers ``/cmd_vel`` (role ``cmd_vel_out``); when the profile did not
    record it, uses ``ArbiterStatus.out_*`` (the documented reconstruction,
    see docs/FLIGHT_RECORDER.md, profile go2_helix).
    """

    def __init__(self, topics_by_role: dict[str, list[str]]) -> None:
        self.out_topics = set(topics_by_role.get("cmd_vel_out", []))
        self.status_topics = set(topics_by_role.get("arbiter_status", []))
        self.use_status = not self.out_topics
        self.source_label = ("/cmd_vel (recorded)" if not self.use_status else
                             "ArbiterStatus out_* (recorded)" if self.status_topics else None)
        self._cmd: Command | None = None
        self._raw: tuple[Any, ...] | None = None
        self._published = False
        self._reason = ""
        self._src = ""
        self._hold: bool | None = None
        self._cause = ""
        self._pending = ""

    @property
    def available(self) -> bool:
        return self.source_label is not None

    def on_event(self, e: ReplayEvent, t_ns: int) -> None:
        if e.kind != "msg" or e.data is None:
            return
        if e.topic in self.status_topics:
            d = e.data
            self._reason = str(d.get("reason", ""))
            self._src = str(d.get("selected_source", ""))
            ha = d.get("hold_active")
            self._hold = ha if isinstance(ha, bool) else None
            if self.use_status:
                self._set((d.get("out_linear_x"), d.get("out_linear_y"), d.get("out_angular_z")),
                          e)
        elif e.topic in self.out_topics:
            lin, ang = twist_of(e.data)
            self._set((lin[0], lin[1], ang[2]), e)

    def _set(self, raw: tuple[Any, Any, Any], e: ReplayEvent) -> None:
        nums = [as_number(v) for v in raw]
        self._raw = raw
        self._cmd = None if any(p for _, p in nums) else Command(*(n + 0.0 for n, _ in nums))
        self._published = True
        self._cause = e.eid
        self._pending = e.eid

    def take_publication(self, t_ns: int) -> Decision | None:
        """The recorded output message just delivered, judged at its own time."""
        eid, self._pending = self._pending, ""
        if not eid:
            return None
        return Decision(t_ns, self._reason or "RECORDED", self._src, self._cmd, self._raw,
                        True, self._hold, eid)

    def tick(self, t_ns: int) -> Decision:
        published, self._published = self._published, False
        cause, self._cause = self._cause, ""
        return Decision(t_ns, self._reason or "RECORDED", self._src, self._cmd, self._raw,
                        published, self._hold, cause or "clock")

    def state(self) -> dict[str, Any]:
        return {"output_source": self.source_label}
