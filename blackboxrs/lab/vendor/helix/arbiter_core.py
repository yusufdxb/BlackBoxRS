"""Pure motion-arbitration core. No ROS imports; every safety policy is here.

The arbiter is the single authoritative velocity output of the HELIX stack.
Upstream command sources (teleop, navigation, ...) compete by priority. HELIX
does NOT compete: it contributes a hold STATE that, when asserted, forces the
output to zero regardless of any source's priority.

Policies (documented in docs/MOTION_ARBITRATION.md, tested in
test/test_arbiter_core.py):

P1  HELIX hold asserted                     -> output ZERO
P2  HELIX state never received              -> output ZERO  (HELIX_STATE_MISSING)
P3  HELIX state older than hold_timeout     -> output ZERO  (HELIX_STATE_STALE)
    A dead or partitioned recovery node therefore stops the robot. There is
    deliberately no "release on stale" option.
P4  Malformed input (NaN, Inf, over limit)  -> rejected, AND that source's
    previous command is discarded, so a stale good value is never reused.
P5  Source older than its timeout           -> dropped from arbitration.
P6  No valid fresh source                   -> output ZERO  (NO_LIVE_INPUT)
P7  Any hold transition (assert or release) discards every source's stored
    command. After a RESUME a source must publish a NEW command before it
    can move the robot, so RESUME never replays a pre-hold command.
P8  HELIX state ordered by (epoch, seq); an older or duplicate message is
    dropped while the current state is fresh. Once stale, any epoch is
    accepted so a restarted publisher with a stepped clock can recover.
P9  Only linear.x, linear.y, angular.z pass; the other three axes must be
    finite and are forced to zero in the output.
P10 Freshness uses the arbiter's receipt clock only. Publisher stamps are
    for tracing (robot and payload clocks are known to be skewed).

Priority ties are broken by most recent receipt, like twist_mux.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

REASON_SOURCE = 'SOURCE'
REASON_HOLD = 'HELIX_HOLD'
REASON_STALE = 'HELIX_STATE_STALE'
REASON_MISSING = 'HELIX_STATE_MISSING'
REASON_NO_INPUT = 'NO_LIVE_INPUT'
REASON_SHUTDOWN = 'SHUTDOWN'

HELIX_FORCED = (REASON_HOLD, REASON_STALE, REASON_MISSING)


@dataclass(frozen=True)
class Command:
    """The three GO2-actionable axes. Everything else is forced to zero."""
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0

    @property
    def is_zero(self) -> bool:
        return self.vx == 0.0 and self.vy == 0.0 and self.wz == 0.0


ZERO = Command()


@dataclass(frozen=True)
class SourceSpec:
    name: str
    topic: str
    priority: int
    timeout_sec: float


@dataclass(frozen=True)
class Limits:
    max_abs_linear: float = 1.0
    max_abs_angular: float = 1.5


@dataclass(frozen=True)
class HoldState:
    hold: bool
    fault_id: str
    epoch: int
    seq: int
    received_at: float


@dataclass(frozen=True)
class Decision:
    command: Command
    reason: str
    source: str = ''
    hold_fault_id: str = ''

    @property
    def helix_forced(self) -> bool:
        return self.reason in HELIX_FORCED


@dataclass
class _Slot:
    spec: SourceSpec
    command: Optional[Command] = None
    received_at: Optional[float] = None
    order: int = -1


@dataclass
class Counters:
    rejected: int = 0
    hold_reordered: int = 0
    hold_transitions: int = 0
    by_source_rejected: Dict[str, int] = field(default_factory=dict)


def validate_twist(linear: Tuple[float, float, float],
                   angular: Tuple[float, float, float],
                   limits: Limits) -> Tuple[Optional[Command], str]:
    """Return (Command, '') if acceptable, else (None, why)."""
    for v in (*linear, *angular):
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            return None, 'non-finite component'
    lx, ly, _ = linear
    _, _, az = angular
    if abs(lx) > limits.max_abs_linear or abs(ly) > limits.max_abs_linear:
        return None, 'linear over limit'
    if abs(az) > limits.max_abs_angular:
        return None, 'angular over limit'
    # +0.0 normalises -0.0 so is_zero and equality are exact.
    return Command(float(lx) + 0.0, float(ly) + 0.0, float(az) + 0.0), ''


class Arbiter:
    """Deterministic arbiter; the caller supplies time on every call."""

    def __init__(self, sources: List[SourceSpec], hold_timeout_sec: float,
                 limits: Limits = Limits()) -> None:
        if not sources:
            raise ValueError('arbiter needs at least one source')
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError(f'duplicate source names: {names}')
        for s in sources:
            if s.timeout_sec <= 0.0:
                # twist_mux treats timeout 0 as "never expires"; that would let a
                # dead source keep authority forever, so it is refused here.
                raise ValueError(f'source {s.name!r} needs a timeout > 0')
        if hold_timeout_sec <= 0.0:
            raise ValueError('hold_timeout_sec must be > 0')
        self._slots: Dict[str, _Slot] = {s.name: _Slot(s) for s in sources}
        self.hold_timeout_sec = hold_timeout_sec
        self.limits = limits
        self._hold: Optional[HoldState] = None
        self._order = 0
        self.counters = Counters()

    @property
    def source_names(self) -> Tuple[str, ...]:
        return tuple(self._slots)

    def spec(self, name: str) -> SourceSpec:
        return self._slots[name].spec

    # -- inputs ---------------------------------------------------------------

    def on_source(self, name: str, linear, angular, now: float) -> bool:
        """Record a source message. Returns False if it was rejected."""
        slot = self._slots[name]
        cmd, _why = validate_twist(tuple(linear), tuple(angular), self.limits)
        if cmd is None:
            slot.command = None          # P4: never fall back to an older value
            slot.received_at = None
            self.counters.rejected += 1
            self.counters.by_source_rejected[name] = (
                self.counters.by_source_rejected.get(name, 0) + 1)
            return False
        self._order += 1
        slot.command, slot.received_at, slot.order = cmd, now, self._order
        return True

    def on_hold(self, hold: bool, fault_id: str, epoch: int, seq: int,
                now: float) -> bool:
        """Record a HELIX hold state. Returns False if dropped as out of order."""
        cur = self._hold
        if cur is not None and self._hold_fresh(now):
            if (epoch, seq) <= (cur.epoch, cur.seq):
                self.counters.hold_reordered += 1
                return False
        prev_effective = self._effective_hold(now)
        self._hold = HoldState(bool(hold), fault_id, int(epoch), int(seq), now)
        if self._effective_hold(now) != prev_effective:
            self._clear_sources()        # P7
            self.counters.hold_transitions += 1
        return True

    # -- decision -------------------------------------------------------------

    def decide(self, now: float) -> Decision:
        effective = self._effective_hold(now)
        if effective:
            # Staleness is also a hold transition: clear sources so a command
            # stored before the state went stale cannot resume motion later.
            self._clear_sources()
        if self._hold is None:
            return Decision(ZERO, REASON_MISSING)
        if not self._hold_fresh(now):
            return Decision(ZERO, REASON_STALE, hold_fault_id=self._hold.fault_id)
        if self._hold.hold:
            return Decision(ZERO, REASON_HOLD, hold_fault_id=self._hold.fault_id)
        live = [s for s in self._slots.values() if self._source_fresh(s, now)]
        if not live:
            return Decision(ZERO, REASON_NO_INPUT)
        win = max(live, key=lambda s: (s.spec.priority, s.order))
        return Decision(win.command, REASON_SOURCE, source=win.spec.name)

    def reset(self) -> None:
        """Forget all inputs and HELIX state (used on lifecycle deactivate)."""
        self._clear_sources()
        self._hold = None

    # -- internals ------------------------------------------------------------

    def _hold_fresh(self, now: float) -> bool:
        return (self._hold is not None
                and (now - self._hold.received_at) <= self.hold_timeout_sec)

    def _effective_hold(self, now: float) -> bool:
        """True whenever HELIX forces zero (asserted, stale or missing)."""
        return self._hold is None or not self._hold_fresh(now) or self._hold.hold

    @staticmethod
    def _source_fresh(slot: _Slot, now: float) -> bool:
        return (slot.command is not None and slot.received_at is not None
                and (now - slot.received_at) <= slot.spec.timeout_sec)

    def _clear_sources(self) -> None:
        for s in self._slots.values():
            s.command = None
            s.received_at = None


def load_sources(params: dict) -> List[SourceSpec]:
    """Build SourceSpecs from a flat {name: {topic, priority, timeout}} map."""
    out = []
    for name, cfg in params.items():
        out.append(SourceSpec(str(name), str(cfg['topic']), int(cfg['priority']),
                              float(cfg['timeout'])))
    return out
