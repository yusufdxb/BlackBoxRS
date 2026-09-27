"""The system under test: what command reaches the robot-facing boundary.

Three kinds:

``observed``
    The arbitration output recorded in the evidence (``/cmd_vel`` when the
    profile subscribed to it, otherwise the ``ArbiterStatus.out_*`` fields,
    which is what the arbiter published on ``/cmd_vel``). Faults on input
    topics cannot change a recorded output; use this mode to check recorded
    incidents and to inject faults into the recorded output itself.

``helix_arbiter`` (reference)
    HELIX's own arbiter, executed: ``blackboxrs.lab.helix`` runs the real
    ``arbiter_core`` (vendored byte for byte, hash checked) behind an adapter
    for the node's ROS glue, configured from HELIX's own ``arbiter.yaml``.
    Parity with the running ``arbiter_node`` is recorded in docs/parity/.

``twist_mux_legacy`` (reference)
    A model of ``twist_mux`` 4.3.0 (C++; a ROS node with its own clock, so it
    cannot be run inside a deterministic replay), configured from HELIX's own
    ``twist_mux.yaml``. It follows the 4.3.0 source: a message is published
    from its input callback only when its input is the highest-priority
    unexpired one (ties go to the alphabetically first input name, as measured
    on the binary); nothing is ever published on a timer, so when every input
    is stale the output is silent; NaN passes through unchanged. Parity with
    the real binary is recorded in docs/parity/.

    HELIX's STOP enters this path as a zero Twist on /helix/cmd_vel, which
    the recovery node publishes right after each hold=true message
    (recovery_node.py ``_on_publish_tick``). When the evidence recorded
    /helix/cmd_vel, those messages are used. When it did not (the go2
    profiles do not record it), they are derived from the hold=true messages
    in exactly that way, and the result says so.

    What the robot does when twist_mux goes silent is not decided by twist_mux.
    The model reports the command the robot-facing consumer would still hold
    IF it retains its last command; that is an assumption about the consumer
    (see docs/ARBITER_PARITY.md), and every legacy result carries it.

    ``freshness_clock: source_timestamp`` (helix_arbiter only) is a
    counterfactual that judges source freshness by the publisher's DDS source
    timestamp. HELIX P10 rejects exactly this; the override exists to show why.

Everything is on integer nanoseconds of the replay clock. The caller
supplies time on every call; nothing here reads a clock.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
    config_file: str
    config_sha256: str
    hold_topic: str = "/helix/hold"
    hold_timeout_s: float = 0.5
    period_s: float = 0.02
    max_abs_linear: float = 1.0
    max_abs_angular: float = 1.5
    freshness_clock: str = "receipt"    # receipt | source_timestamp (helix_arbiter only)
    recovery_topic: str = ""            # twist_mux_legacy: HELIX's zero-twist input
    consumer_assumption: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sources"] = [asdict(s) for s in self.sources]
        return d


def _vendor(name: str) -> tuple[dict[str, Any], str, str]:
    import hashlib

    import yaml

    from blackboxrs.lab.helix import VENDOR, provenance
    path = VENDOR / name
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    want = provenance()["files"][name]["sha256"]
    if digest != want:
        raise ValueError(f"{path}: sha256 {digest} is not the pinned HELIX config {want}")
    return yaml.safe_load(data), provenance()["files"][name]["path"], digest


def _helix_preset() -> ArbiterConfig:
    raw, path, digest = _vendor("arbiter.yaml")
    p = raw["helix_arbiter"]["ros__parameters"]
    return ArbiterConfig(
        preset="helix_arbiter",
        sources=tuple(SourceSpec(n, str(c["topic"]), int(c["priority"]), float(c["timeout"]))
                      for n, c in sorted(p["sources"].items())),
        config_file=path, config_sha256=digest, hold_topic=str(p["hold_topic"]),
        hold_timeout_s=float(p["hold_timeout_sec"]), period_s=1.0 / float(p["rate_hz"]),
        max_abs_linear=float(p["max_abs_linear"]), max_abs_angular=float(p["max_abs_angular"]))


def _twist_mux_preset() -> ArbiterConfig:
    raw, path, digest = _vendor("twist_mux.yaml")
    p = raw["twist_mux"]["ros__parameters"]
    for name, lock in (p.get("locks") or {}).items():
        # a lock with timeout 0 never expires and, never published, never locks;
        # with priority 0 it could not mask any input anyway
        if float(lock["timeout"]) != 0.0 or int(lock["priority"]) != 0:
            raise ValueError(f"twist_mux lock {name!r} is active; locks are not modeled")
    srcs = tuple(SourceSpec(n, str(c["topic"]), max(0, min(255, int(c["priority"]))),
                            float(c["timeout"])) for n, c in sorted(p["topics"].items()))
    recovery = next((s.topic for s in srcs if s.name == "helix_recovery"), "")
    return ArbiterConfig(
        preset="twist_mux_legacy", sources=srcs, config_file=path, config_sha256=digest,
        recovery_topic=recovery,
        consumer_assumption=("UNVERIFIED: the robot-facing consumer keeps executing the last "
                             "command twist_mux published; twist_mux itself publishes nothing "
                             "when every input is stale"))


PRESETS = {"helix_arbiter": _helix_preset, "twist_mux_legacy": _twist_mux_preset}

_OVERRIDABLE = {"helix_arbiter": {"sources", "hold_topic", "hold_timeout_s", "max_abs_linear",
                                  "max_abs_angular", "freshness_clock"},
                "twist_mux_legacy": {"sources"}}


def build_config(preset: str, overrides: dict[str, Any] | None = None) -> ArbiterConfig:
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; known: {sorted(PRESETS)}")
    cfg = PRESETS[preset]()
    o = dict(overrides or {})
    bad = sorted(set(o) - _OVERRIDABLE[preset])
    if bad:
        raise ValueError(f"sut overrides for {preset}: unknown or fixed keys {bad} "
                         f"(overridable: {sorted(_OVERRIDABLE[preset])})")
    if "sources" in o:
        o["sources"] = tuple(SourceSpec(str(s["name"]), str(s["topic"]), int(s["priority"]),
                                        float(s["timeout_s"])) for s in o["sources"])
    if o.get("freshness_clock", "receipt") not in ("receipt", "source_timestamp"):
        raise ValueError("freshness_clock must be receipt or source_timestamp")
    if o:
        o["config_file"] = cfg.config_file + " (overridden)"
    cfg = replace(cfg, **o)
    names = [s.name for s in cfg.sources]
    if not cfg.sources or len(set(names)) != len(names):
        raise ValueError(f"sources must be non-empty with unique names: {names}")
    if any(s.timeout_s <= 0 for s in cfg.sources) or cfg.hold_timeout_s <= 0:
        raise ValueError("every timeout must be > 0 (twist_mux timeout 0 = never expires "
                         "is not modeled)")
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


@dataclass
class _Input:
    spec: SourceSpec
    raw: tuple[Any, ...] | None = None
    cmd: Command | None = None
    rx_ns: int | None = None
    eid: str = ""


class TwistMuxModel:
    """twist_mux 4.3.0 on HELIX's twist_mux.yaml (see the module docstring)."""

    implementation = "twist_mux 4.3.0 model (C++ binary not executable in replay)"

    def __init__(self, cfg: ArbiterConfig, *, derive_recovery_from_hold: bool = False) -> None:
        self.cfg = cfg
        self.derive = derive_recovery_from_hold and bool(cfg.recovery_topic)
        # sorted by name: equal priorities go to the first name (measured)
        self.inputs = {s.topic: _Input(s) for s in sorted(cfg.sources, key=lambda s: s.name)}
        self._sink: Command | None = None
        self._sink_raw: tuple[Any, ...] | None = None
        self._sink_eid = ""
        self._pending = ""
        self.published = 0
        self.derived = 0

    def on_event(self, e: ReplayEvent, t_ns: int) -> None:
        if e.kind != "msg":
            return
        if self.derive and e.topic == self.cfg.hold_topic:
            if e.data is not None and e.data.get("hold") is True:
                # recovery_node publishes Twist() on /helix/cmd_vel after each hold=true
                self.derived += 1
                self._input(self.inputs[self.cfg.recovery_topic],
                            (0.0, 0.0, 0.0, 0.0, 0.0, 0.0), e.eid, t_ns)
            return
        inp = self.inputs.get(e.topic or "")
        if inp is None:
            return
        if e.data is None:
            raise EvidenceError(f"{e.eid} on {e.topic}: payload not stored; the model cannot "
                                "replay a command it cannot read")
        lin, ang = twist_of(e.data)
        self._input(inp, (*lin, *ang), e.eid, t_ns)

    def _input(self, inp: _Input, v: tuple[Any, ...], eid: str, t_ns: int) -> None:
        nums = [as_number(x) for x in v]
        inp.raw = (v[0], v[1], v[5])   # forwarded unchanged: NaN and garbage included
        inp.cmd = (None if any(p for _, p in nums)
                   else Command(nums[0][0] + 0.0, nums[1][0] + 0.0, nums[5][0] + 0.0))
        inp.rx_ns, inp.eid = t_ns, eid
        if self._winner(t_ns) is inp:      # VelocityTopicHandle::callback + hasPriority
            self._sink, self._sink_raw, self._sink_eid = inp.cmd, inp.raw, eid
            self._pending = eid
            self.published += 1

    def _winner(self, t_ns: int) -> _Input | None:
        live = [i for i in self.inputs.values() if i.rx_ns is not None
                and t_ns - i.rx_ns <= int(round(i.spec.timeout_s * NS))]   # hasExpired: >
        return max(live, key=lambda i: i.spec.priority) if live else None  # first max wins

    def take_publication(self, t_ns: int) -> Decision | None:
        eid, self._pending = self._pending, ""
        if not eid:
            return None
        win = self._winner(t_ns)
        return Decision(t_ns, REASON_SOURCE, "" if win is None else win.spec.name, self._sink,
                        self._sink_raw, True, None, eid)

    def tick(self, t_ns: int) -> Decision:
        """No publication happens here; this samples what the consumer holds."""
        win = self._winner(t_ns)
        reason = REASON_SOURCE if win is not None else REASON_SILENT
        return Decision(t_ns, reason, "" if win is None else win.spec.name, self._sink,
                        self._sink_raw, False, None,
                        (self._sink_eid or "clock") if win is not None else "clock")

    def state(self) -> dict[str, Any]:
        return {"implementation": self.implementation,
                "config": {"file": self.cfg.config_file, "sha256": self.cfg.config_sha256},
                "published": self.published,
                "recovery_zero_twists_derived_from_hold": self.derived,
                "consumer_assumption": self.cfg.consumer_assumption}


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
