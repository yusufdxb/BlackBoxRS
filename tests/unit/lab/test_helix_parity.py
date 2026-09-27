"""The helix_arbiter reference model against the HELIX arbiter itself.

Runs only when a HELIX checkout is available (HELIX_SRC=<path to the helix
repo>); HELIX is a separate repository and is not a dependency. Both
arbiters get the same randomly generated input stream (seeded, so any
failure is reproducible) and must make the same decision at every tick.
"""

from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from pathlib import Path

import pytest

from blackboxrs.lab.sut import ReferenceArbiter, build_config

from .conftest import NS, hold, msg

HELIX = os.environ.get("HELIX_SRC")
CORE = Path(HELIX or "/nonexistent") / "src/helix_arbiter/helix_arbiter/arbiter_core.py"
pytestmark = pytest.mark.skipif(not CORE.is_file(),
                                reason="set HELIX_SRC to a HELIX checkout to run")


def _helix():
    spec = importlib.util.spec_from_file_location("helix_arbiter_core", CORE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def _value(rng):
    r = rng.random()
    if r < 0.05:
        return math.nan
    if r < 0.08:
        return math.inf
    if r < 0.12:
        return 1.3          # over the linear limit
    if r < 0.14:
        return "fast"       # not a number
    return round(rng.uniform(-0.8, 0.8), 3)


@pytest.mark.parametrize("seed", range(20))
def test_decisions_match_helix(seed):
    h = _helix()
    rng = random.Random(seed)
    cfg = build_config("helix_arbiter")
    ours = ReferenceArbiter(cfg)
    theirs = h.Arbiter([h.SourceSpec(s.name, s.topic, s.priority, s.timeout_s)
                        for s in cfg.sources], cfg.hold_timeout_s,
                       h.Limits(cfg.max_abs_linear, cfg.max_abs_angular))
    names = {s.topic: s.name for s in cfg.sources}
    t, seq, hseq, epoch, held = 0.0, 0, 0, 1, False
    ticks = 0
    while t < 8.0:
        t = round(t + rng.choice([0.003, 0.007, 0.011, 0.02, 0.05, 0.2]), 6)
        r = rng.random()
        seq += 1
        if r < 0.55:
            topic = rng.choice(sorted(names))
            vx, vy, wz = _value(rng), _value(rng), _value(rng)
            # the non-actuated axes: usually zero, sometimes nonzero or non-finite (P9)
            lz, ax, ay = (rng.choice([0.0, 0.0, 0.0, 0.2, math.nan, "fast"]) for _ in range(3))
            data = {"linear": {"x": vx, "y": vy, "z": lz}, "angular": {"x": ax, "y": ay, "z": wz}}
            e = msg(t, topic, data, seq=seq)
            ours.on_event(e, e.t_ns)
            theirs.on_source(names[topic], (vx, vy, lz), (ax, ay, wz), t)
        elif r < 0.75:
            if rng.random() < 0.15:
                held = not held
            if rng.random() < 0.05:
                epoch += 1
            hseq = hseq + 1 if rng.random() > 0.1 else max(0, hseq - 2)   # some reordering
            e = hold(t, held, seq, eseq=hseq, epoch=epoch)
            ours.on_event(e, e.t_ns)
            theirs.on_hold(held, "f" if held else "", epoch, hseq, t)
        else:
            ticks += 1
            ns = int(round(t * NS))
            a, b = ours.tick(ns), theirs.decide(ns / NS)
            assert a.reason == b.reason, (seed, t)
            assert a.source == b.source, (seed, t)
            assert tuple(a.robot_cmd.as_list()) == (b.command.vx, b.command.vy,
                                                     b.command.wz), (seed, t)
    assert ticks > 20
