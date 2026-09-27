"""Replay adapter around the real HELIX motion arbiter.

The arbitration decisions come from HELIX's own ``arbiter_core.Arbiter``,
executed, not re-implemented. The module is vendored byte for byte from the
HELIX repository (``vendor/helix/arbiter_core.py``, provenance in
``vendor/helix/PROVENANCE.json``) and refused at load time if its SHA-256
does not match. ``tests/unit/lab/test_arbiter_parity.py`` checks the vendored
file against a HELIX checkout when ``HELIX_SRC`` is set; CI checks out HELIX
at the pinned commit to run it.

This adapter only reproduces the ROS glue of HELIX ``arbiter_node.py``
(same commit), which cannot be imported without rclpy and helix_msgs:

* a Twist on a source topic -> ``on_source(name, linear xyz, angular xyz, now)``
* a HelixHold -> ``on_hold(hold, fault_id, epoch, seq, now)``, and when the
  message asserts the hold, an immediate ``decide(now)`` and publish
  (``_on_hold`` calls ``_on_tick`` so a hold lands on the next publish, not
  up to one period later)
* a timer at ``rate_hz`` -> ``decide(now)`` and publish
* ``now`` is the node's monotonic receipt clock, which is the replay clock.

The glue itself is checked against the real node running under ROS 2
(``scripts/parity/run_ros_parity.py``, evidence in docs/parity/).

Values: over ROS a Twist field is always a float, so the flight record's
"NaN"/"Infinity" strings are turned back into floats; anything else that is
not a number is passed through unchanged and the real core rejects it. A
HelixHold whose fields have the wrong types could not be delivered by ROS at
all; the adapter skips it and counts it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.values import NS, as_number, get_path

VENDOR = Path(__file__).parent / "vendor" / "helix"
_MODULE_NAME = "blackboxrs.lab.vendor.helix.arbiter_core"
_loaded: ModuleType | None = None


class ProvenanceError(RuntimeError):
    """The vendored HELIX module is not the pinned file."""


def provenance() -> dict[str, Any]:
    return json.loads((VENDOR / "PROVENANCE.json").read_text(encoding="utf-8"))


def load_core(path: Path | None = None) -> ModuleType:
    """Import HELIX arbiter_core (the vendored copy, or ``path``) after a hash check."""
    global _loaded
    if path is None and _loaded is not None:
        return _loaded
    src = path or VENDOR / "arbiter_core.py"
    want = provenance()["files"]["arbiter_core.py"]["sha256"]
    got = hashlib.sha256(src.read_bytes()).hexdigest()
    if got != want:
        raise ProvenanceError(f"{src}: sha256 {got} is not the pinned HELIX arbiter_core "
                              f"{want}; refusing to run an unverified arbiter")
    name = _MODULE_NAME if path is None else f"_helix_arbiter_core_{got[:12]}"
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses resolve their module by name
    spec.loader.exec_module(mod)     # type: ignore[union-attr]
    if path is None:
        _loaded = mod
    return mod


def _ros_float(v: Any) -> Any:
    """What rclpy would hand the node for a flight-record value."""
    n, problem = as_number(v)
    return n if problem != "malformed" else v


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class HelixArbiterAdapter:
    """System under test: replay events -> real HELIX Arbiter -> robot-facing command."""

    implementation = "helix_arbiter_core (real code, vendored)"

    def __init__(self, cfg: Any, wall0_ns: int = 0) -> None:
        from blackboxrs.lab.sut import build_config  # noqa: F401  (cfg is an ArbiterConfig)
        core = load_core()
        self.core = core
        self.cfg = cfg
        self.wall0_ns = wall0_ns
        # arbiter_node._source_specs: sorted by source name
        specs = [core.SourceSpec(s.name, s.topic, int(s.priority), float(s.timeout_s))
                 for s in sorted(cfg.sources, key=lambda s: s.name)]
        self.arb = core.Arbiter(specs, hold_timeout_sec=float(cfg.hold_timeout_s),
                                limits=core.Limits(float(cfg.max_abs_linear),
                                                   float(cfg.max_abs_angular)))
        self._name_of = {s.topic: s.name for s in specs}
        self._last_eid: dict[str, str] = {}
        self._hold_eid = ""
        self._rejected = ""
        self._pending = None
        self.skipped_malformed_hold = 0

    @property
    def input_topics(self) -> set[str]:
        return set(self._name_of) | {self.cfg.hold_topic}

    def on_event(self, e: ReplayEvent, t_ns: int) -> None:
        from blackboxrs.lab.evidence import EvidenceError
        if e.kind != "msg":
            return
        if e.topic == self.cfg.hold_topic:
            if e.data is None:
                raise EvidenceError(f"{e.eid} on {e.topic}: hold payload not stored")
            d = e.data
            hold, epoch, seq = d.get("hold"), d.get("epoch"), d.get("seq")
            fid = d.get("fault_id", "")
            if not (isinstance(hold, bool) and _is_int(epoch) and _is_int(seq)
                    and isinstance(fid, str)):
                self.skipped_malformed_hold += 1
                return
            if self.arb.on_hold(hold, fid, epoch, seq, t_ns / NS):
                self._hold_eid = e.eid
            if hold:   # arbiter_node._on_hold: publish at once on an assertion
                self._pending = self._emit(t_ns, rejected="")
            return
        name = self._name_of.get(e.topic or "")
        if name is None:
            return
        if e.data is None:
            raise EvidenceError(f"{e.eid} on {e.topic}: payload not stored; the arbiter "
                                "cannot replay a command it cannot read")
        lin = tuple(_ros_float(get_path(e.data, f"linear.{a}")[1]) for a in "xyz")
        ang = tuple(_ros_float(get_path(e.data, f"angular.{a}")[1]) for a in "xyz")
        now = t_ns / NS
        if self.cfg.freshness_clock == "source_timestamp" and e.src_ns is not None:
            # counterfactual: freshness measured from the publisher's clock
            now = (e.src_ns - self.wall0_ns) / NS
        if self.arb.on_source(name, lin, ang, now):
            self._last_eid[name] = e.eid
        else:
            self._rejected = e.eid

    def _emit(self, t_ns: int, rejected: str):
        from blackboxrs.lab.sut import Command, Decision
        d = self.arb.decide(t_ns / NS)
        c = d.command
        cmd = Command(c.vx, c.vy, c.wz)
        if d.reason == self.core.REASON_SOURCE:
            cause = self._last_eid.get(d.source, "clock")
        elif d.reason == self.core.REASON_HOLD:
            cause = self._hold_eid or "clock"
        elif d.reason == self.core.REASON_NO_INPUT:
            cause = rejected or "clock"
        else:
            cause = "clock"
        hold = True if d.reason == self.core.REASON_HOLD else (
            False if d.reason in (self.core.REASON_SOURCE, self.core.REASON_NO_INPUT) else None)
        return Decision(t_ns, d.reason, d.source, cmd, (c.vx, c.vy, c.wz), True, hold, cause)

    def tick(self, t_ns: int):
        rejected, self._rejected = self._rejected, ""
        return self._emit(t_ns, rejected)

    def take_publication(self, t_ns: int):
        p, self._pending = self._pending, None
        return p

    def state(self) -> dict[str, Any]:
        c = self.arb.counters
        return {"implementation": provenance(),
                "counters": {"rejected": c.rejected, "hold_reordered": c.hold_reordered,
                             "hold_transitions": c.hold_transitions,
                             "by_source_rejected": dict(sorted(c.by_source_rejected.items())),
                             "skipped_malformed_hold": self.skipped_malformed_hold}}
