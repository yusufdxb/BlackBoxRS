#!/usr/bin/env python3
"""Freeze the decisions of the real HELIX arbiter for the C++ parity tests.

HELIX's arbitration logic (src/helix_arbiter/helix_arbiter/arbiter_core.py) is
pure Python and cannot be linked into the C++ runtime. The C++ helix_arbiter
model is therefore checked, decision by decision, against the real module:

* this script runs HELIX's own ``Arbiter`` on targeted scenarios and seeded
  random streams and writes the input operations and HELIX's decisions to
  ``examples/replay_lab/arbiter_parity/helix_arbiter_core.json``, together
  with the HELIX commit and the SHA-256 of arbiter_core.py;
* ``cpp/tests/test_helix_parity.cpp`` replays every stream through the C++
  model and requires the same reason, winner and command at every tick;
* ``tests/cpp/test_helix_arbiter_parity.py`` does the same live against a
  HELIX checkout when HELIX_SRC is set, so a changed arbiter_core.py is
  caught even before the frozen file is regenerated.

Usage: HELIX_SRC=~/workspace/helix python scripts/cpp/make_arbiter_traces.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "examples" / "replay_lab" / "arbiter_parity" / "helix_arbiter_core.json"
CORE_REL = "src/helix_arbiter/helix_arbiter/arbiter_core.py"

SOURCES = [("teleop", "/teleop/cmd_vel", 200, 0.5), ("nav", "/nav/cmd_vel", 50, 0.5)]
HOLD_TIMEOUT = 0.5
LIMITS = (1.0, 1.5)


def load_helix(helix_src: Path):
    core = helix_src / CORE_REL
    spec = importlib.util.spec_from_file_location("helix_arbiter_core", core)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod, core


def enc(v):
    """JSON-safe encoding of an input value (the C++ side decodes it the same way)."""
    if isinstance(v, float) and math.isnan(v):
        return "NaN"
    if isinstance(v, float) and math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    return v


class Stream:
    """Collects operations and runs them through the real HELIX arbiter."""

    def __init__(self, h, name: str, seed: int | None = None):
        self.h = h
        self.name = name
        self.seed = seed
        self.ops: list[dict] = []
        self.decisions: list[dict] = []
        self.arb = h.Arbiter([h.SourceSpec(*s) for s in SOURCES], HOLD_TIMEOUT, h.Limits(*LIMITS))
        self.names = {topic: name for name, topic, _, _ in SOURCES}

    def source(self, t: float, topic: str, lin, ang):
        self.ops.append({"op": "source", "t": t, "topic": topic,
                         "linear": [enc(x) for x in lin], "angular": [enc(x) for x in ang]})
        self.arb.on_source(self.names[topic], tuple(lin), tuple(ang), t)

    def hold(self, t: float, held: bool, epoch: int, seq: int, fault_id: str = ""):
        self.ops.append({"op": "hold", "t": t, "hold": held, "fault_id": fault_id,
                         "epoch": epoch, "seq": seq})
        self.arb.on_hold(held, fault_id, epoch, seq, t)

    def tick(self, t: float):
        self.ops.append({"op": "tick", "t": t})
        d = self.arb.decide(t)
        self.decisions.append({"t": t, "reason": d.reason, "source": d.source,
                               "command": [d.command.vx, d.command.vy, d.command.wz]})

    def to_json(self) -> dict:
        return {"name": self.name, "seed": self.seed, "ops": self.ops, "decisions": self.decisions}


def twist(vx, vy=0.0, wz=0.0, lz=0.0, ax=0.0, ay=0.0):
    return (vx, vy, lz), (ax, ay, wz)


def timeline(stream: Stream, until: float, events):
    """Run events (t, fn) merged with 50 Hz ticks, events first at equal times."""
    ticks = [round(k * 0.02, 6) for k in range(int(until / 0.02) + 1)]
    items = [(t, 0, i, fn) for i, (t, fn) in enumerate(events)] + \
            [(t, 1, i, None) for i, t in enumerate(ticks)]
    for t, kind, _, fn in sorted(items, key=lambda x: (x[0], x[1], x[2])):
        if kind == 0:
            fn(t)
        else:
            stream.tick(t)


def periodic(start: float, end: float, hz: float, fn):
    n = int(round((end - start) * hz))
    return [(round(start + k / hz, 6), fn) for k in range(n)]


def scenarios(h) -> list[Stream]:
    out = []

    # 1. normal: hold released at 20 Hz, navigation at 20 Hz.
    s = Stream(h, "normal_command")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 2.0, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(0.01, 2.0, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 2.0, ev)
    out.append(s)

    # 2. STOP: the hold is asserted at 1.0 s while navigation keeps commanding.
    s = Stream(h, "stop")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 1.0, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(1.0, 2.0, 20, lambda t: s.hold(t, True, 1, next(seq), "F1"))
    ev += periodic(0.01, 2.0, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 2.0, ev)
    out.append(s)

    # 3. stale command: navigation goes silent at 1.0 s.
    s = Stream(h, "stale_command")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 2.5, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(0.01, 1.0, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 2.5, ev)
    out.append(s)

    # 4. teleop and navigation both command: teleop (200) beats nav (50).
    s = Stream(h, "conflicting_sources")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 2.0, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(0.01, 2.0, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    ev += periodic(0.5, 1.5, 20, lambda t: s.source(t, "/teleop/cmd_vel", *twist(0.4, wz=0.3)))
    timeline(s, 2.0, ev)
    out.append(s)

    # 5. teleop vs STOP: a hold during active teleop must win.
    s = Stream(h, "teleop_vs_stop")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 0.8, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(0.8, 2.0, 20, lambda t: s.hold(t, True, 1, next(seq), "F2"))
    ev += periodic(0.3, 2.0, 20, lambda t: s.source(t, "/teleop/cmd_vel", *twist(0.4)))
    timeline(s, 2.0, ev)
    out.append(s)

    # 6. NaN, Inf, over-limit and non-numeric input is rejected, not clamped.
    s = Stream(h, "nonfinite_input")
    seq = iter(range(1, 10_000))
    bad = [math.nan, math.inf, 1.3, "fast", -math.inf]
    ev = periodic(0.0, 2.0, 20, lambda t: s.hold(t, False, 1, next(seq)))
    k = iter(range(10_000))
    ev += periodic(0.01, 2.0, 20, lambda t: s.source(
        t, "/nav/cmd_vel", *twist(bad[next(k) % 5] if int(t * 20) % 3 == 0 else 0.2)))
    timeline(s, 2.0, ev)
    out.append(s)

    # 7. the hold stream disappears: the state goes stale and forces zero.
    s = Stream(h, "hold_stream_lost")
    seq = iter(range(1, 10_000))
    ev = periodic(0.0, 1.0, 20, lambda t: s.hold(t, False, 1, next(seq)))
    ev += periodic(0.01, 2.5, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 2.5, ev)
    out.append(s)

    # 8. no hold message ever: HELIX_STATE_MISSING.
    s = Stream(h, "hold_state_missing")
    ev = periodic(0.01, 1.0, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 1.0, ev)
    out.append(s)

    # 9. an older RESUME redelivered while the hold is fresh is ignored.
    s = Stream(h, "old_resume_redelivered")
    ev = [(0.0, lambda t: s.hold(t, False, 1, 1)), (0.2, lambda t: s.hold(t, True, 1, 5, "F3")),
          (0.3, lambda t: s.hold(t, False, 1, 3)), (0.35, lambda t: s.hold(t, True, 1, 6, "F3")),
          (0.5, lambda t: s.hold(t, False, 1, 7))]
    ev += periodic(0.01, 1.2, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 1.2, ev)
    out.append(s)

    # 10. a restarted publisher (lower epoch) is accepted only once the state is stale.
    s = Stream(h, "restart_lower_epoch")
    ev = [(0.0, lambda t: s.hold(t, False, 3, 10)), (0.1, lambda t: s.hold(t, True, 1, 1, "F4")),
          (0.9, lambda t: s.hold(t, True, 1, 2, "F4")), (1.0, lambda t: s.hold(t, False, 1, 3))]
    ev += periodic(0.01, 1.5, 20, lambda t: s.source(t, "/nav/cmd_vel", *twist(0.2)))
    timeline(s, 1.5, ev)
    out.append(s)
    return out


def value(rng):
    r = rng.random()
    if r < 0.05:
        return math.nan
    if r < 0.08:
        return math.inf
    if r < 0.12:
        return 1.3
    if r < 0.14:
        return "fast"
    return round(rng.uniform(-0.8, 0.8), 3)


def random_stream(h, seed: int) -> Stream:
    """The stream generator of tests/unit/lab/test_helix_parity.py, recorded."""
    s = Stream(h, f"random_{seed}", seed)
    rng = random.Random(seed)
    t, hseq, epoch, held = 0.0, 0, 1, False
    topics = sorted(s.names)
    while t < 8.0:
        t = round(t + rng.choice([0.003, 0.007, 0.011, 0.02, 0.05, 0.2]), 6)
        r = rng.random()
        if r < 0.55:
            topic = rng.choice(topics)
            vx, vy, wz = value(rng), value(rng), value(rng)
            lz, ax, ay = (rng.choice([0.0, 0.0, 0.0, 0.2, math.nan, "fast"]) for _ in range(3))
            s.source(t, topic, (vx, vy, lz), (ax, ay, wz))
        elif r < 0.75:
            if rng.random() < 0.15:
                held = not held
            if rng.random() < 0.05:
                epoch += 1
            hseq = hseq + 1 if rng.random() > 0.1 else max(0, hseq - 2)
            s.hold(t, held, epoch, hseq, "f" if held else "")
        else:
            s.tick(t)
    return s


def main() -> int:
    src = os.environ.get("HELIX_SRC")
    if not src:
        print("set HELIX_SRC to a HELIX checkout", file=sys.stderr)
        return 2
    helix_src = Path(src).expanduser()
    h, core = load_helix(helix_src)
    commit = subprocess.run(["git", "-C", str(helix_src), "log", "-1", "--format=%H", "--",
                             CORE_REL], capture_output=True, text=True).stdout.strip()
    streams = scenarios(h) + [random_stream(h, seed) for seed in range(30)]
    doc = {
        "schema": "blackboxrs.arbiter_parity.v1",
        "about": "Decisions of the real HELIX arbiter (arbiter_core.Arbiter) for recorded input "
                 "streams. Generated by scripts/cpp/make_arbiter_traces.py; do not edit.",
        "helix_arbiter_core": {"path": CORE_REL, "last_commit": commit,
                               "sha256": hashlib.sha256(core.read_bytes()).hexdigest()},
        "config": {"sources": [dict(zip(("name", "topic", "priority", "timeout_s"), s))
                               for s in SOURCES],
                   "hold_timeout_s": HOLD_TIMEOUT, "max_abs_linear": LIMITS[0],
                   "max_abs_angular": LIMITS[1]},
        "streams": [s.to_json() for s in streams],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # One stream per line: compact, and a regenerated file still diffs per stream.
    head = {k: v for k, v in doc.items() if k != "streams"}
    lines = [json.dumps(head, sort_keys=True)[:-1] + ',"streams":[']
    lines += [json.dumps(s, sort_keys=True, separators=(",", ":")) + ","
              for s in doc["streams"]]
    lines[-1] = lines[-1][:-1]
    lines.append("]}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n = sum(len(s.decisions) for s in streams)
    print(f"{OUT}: {len(streams)} streams, {n} decisions "
          f"(arbiter_core {doc['helix_arbiter_core']['sha256'][:12]} @ {commit[:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
