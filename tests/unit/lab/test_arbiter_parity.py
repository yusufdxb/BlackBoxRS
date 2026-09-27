"""Arbiter parity: Replay Lab's systems under test against the deployed implementations.

1. The HELIX files Replay Lab executes or reads are byte-identical to HELIX at
   the pinned commit (needs HELIX_SRC; CI checks HELIX out at that commit).
2. The helix_arbiter adapter (real arbiter_core + node glue) reproduces the
   decision trace recorded from the real arbiter_node under ROS 2.
3. The twist_mux model reproduces the message sequence recorded from the real
   twist_mux 4.3.0 binary on HELIX's twist_mux.yaml, including its silences
   and its tie-break.
4. The recorded runs used exactly the frozen scenario scripts.
5. On random streams, the adapter decides exactly as direct calls into the
   live HELIX checkout do.

The recordings are docs/parity/*.json, made by scripts/parity/run_ros_parity.py.
A failure here is a finding: fix Replay Lab, do not loosen the comparison.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from blackboxrs.lab.helix import (
    VENDOR,
    HelixArbiterAdapter,
    ProvenanceError,
    load_core,
    provenance,
)
from blackboxrs.lab.parity import (
    HOLD,
    SCENARIOS,
    compare_helix,
    compare_twist_mux,
    events_for,
    run_trace,
)
from blackboxrs.lab.sut import TwistMuxModel, build_config

from .conftest import NS, ROOT, hold, msg

PARITY = ROOT / "docs" / "parity"
HELIX = os.environ.get("HELIX_SRC")
needs_helix = pytest.mark.skipif(not HELIX or not Path(HELIX).is_dir(),
                                 reason="set HELIX_SRC to a HELIX checkout")


def recorded(name: str) -> dict:
    return json.loads((PARITY / f"{name}.json").read_text())


HELIX_REC = recorded("helix_arbiter_node")
MUX_REC = recorded("twist_mux")


# -- 1. provenance -------------------------------------------------------------------

@needs_helix
def test_vendored_files_are_the_pinned_helix_files():
    prov = provenance()
    head = subprocess.run(["git", "-C", HELIX, "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    for name, meta in prov["files"].items():
        pinned = subprocess.run(["git", "-C", HELIX, "show", f"{prov['commit']}:{meta['path']}"],
                                capture_output=True, check=True).stdout
        assert (VENDOR / name).read_bytes() == pinned, name
        if head == prov["commit"]:
            assert (Path(HELIX) / meta["path"]).read_bytes() == pinned, name


def test_a_modified_arbiter_is_refused(tmp_path):
    bad = tmp_path / "arbiter_core.py"
    bad.write_bytes((VENDOR / "arbiter_core.py").read_bytes() + b"\n# edited\n")
    with pytest.raises(ProvenanceError):
        load_core(bad)


def test_recordings_name_the_implementations_they_ran():
    assert HELIX_REC["environment"]["helix_commit"] == provenance()["commit"]
    assert MUX_REC["environment"]["twist_mux_package"].startswith("4.3.0")


# -- 2./3. recorded real runs -------------------------------------------------------------

@pytest.mark.parametrize("rec", HELIX_REC["scenarios"], ids=lambda r: r["scenario"])
def test_helix_adapter_matches_the_real_arbiter_node(rec):
    cfg = build_config("helix_arbiter")
    res = compare_helix(rec, lambda: HelixArbiterAdapter(cfg))
    assert res["ok"], res["problems"]


@pytest.mark.parametrize("rec", MUX_REC["scenarios"], ids=lambda r: r["scenario"])
def test_twist_mux_model_matches_the_real_binary(rec):
    res = compare_twist_mux(rec, lambda: TwistMuxModel(build_config("twist_mux_legacy")))
    assert res["ok"], res["problems"]


@pytest.mark.parametrize("name", ["twist_mux_tie", "twist_mux_tie_reversed"])
def test_twist_mux_tie_break_matches_the_real_binary(name):
    rec = recorded(name)["scenarios"][0]
    topics = yaml.safe_load(rec["config"])["twist_mux"]["ros__parameters"]["topics"]
    cfg = build_config("twist_mux_legacy", {"sources": [
        {"name": n, "topic": c["topic"], "priority": c["priority"], "timeout_s": c["timeout"]}
        for n, c in topics.items()]})
    res = compare_twist_mux(rec, lambda: TwistMuxModel(cfg))
    assert res["ok"], res["problems"]


def test_twist_mux_goes_silent_and_never_publishes_zero_on_timeout():
    """Measured: after the last input, the binary publishes nothing at all."""
    for name in ("stale_command", "freshness_expiry"):
        rec = next(r for r in MUX_REC["scenarios"] if r["scenario"] == name)
        last_in = max(s[1] for s in rec["sent"])
        after = [o for o in rec["outputs"] if o[0] > last_in + 0.005]
        assert after == [], after
        assert rec["duration_s"] - last_in > 1.4
        assert rec["outputs"][-1][1:4] != [0.0, 0.0, 0.0]


def test_helix_sport_sink_stops_the_robot_when_twist_mux_goes_silent():
    """Measured: HELIX's robot-facing sink (dry_run) sends StopMove, not the last Move."""
    rec = recorded("twist_mux_then_sink")["scenarios"][0]
    last_cmd = max(o[0] for o in rec["outputs"] if o[1] != "sink")
    after = [o for o in rec["outputs"] if o[1] == "sink" and o[0] > last_cmd + 0.01]
    assert after and all(o[2] == 1003 and o[3] == "DEADMAN" for o in after)
    assert 0.25 <= after[0][0] - last_cmd <= 0.31     # input_timeout 0.25 s, 50 ms tick


def test_old_model_divergence_is_still_detectable():
    """The comparison is sharp enough to fail an arbiter without publish-on-hold (D1)."""
    cfg = build_config("helix_arbiter")

    class NoPublishOnHold(HelixArbiterAdapter):
        def take_publication(self, t_ns):
            super().take_publication(t_ns)
            return None

    rec = next(r for r in HELIX_REC["scenarios"] if r["scenario"] == "stop")
    res = compare_helix(rec, lambda: NoPublishOnHold(cfg))
    assert not res["ok"] and "does not publish on hold" in res["problems"][-1]


# -- 4. the recordings used the frozen scripts ---------------------------------------------

@pytest.mark.parametrize("rec", HELIX_REC["scenarios"] + MUX_REC["scenarios"],
                         ids=lambda r: r["scenario"])
def test_recordings_used_the_frozen_scenarios(rec):
    sc = SCENARIOS[rec["scenario"]]
    legacy = rec in MUX_REC["scenarios"]
    want = [(round(e.t_ns / NS, 6), e.topic) for e in events_for(sc, legacy=legacy)
            if not (legacy and e.topic == HOLD)]
    got = [(s[0], s[2]) for s in rec["sent"]]
    assert got == want
    assert rec["max_send_lateness_s"] < 0.01


# -- 5. adapter vs direct calls into the live HELIX module ------------------------------------

def _live_core():
    path = Path(HELIX) / "src/helix_arbiter/helix_arbiter/arbiter_core.py"
    spec = importlib.util.spec_from_file_location("live_helix_arbiter_core", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _value(rng):
    r = rng.random()
    return (math.nan if r < 0.05 else math.inf if r < 0.08 else 1.3 if r < 0.12
            else "fast" if r < 0.14 else round(rng.uniform(-0.8, 0.8), 3))


@needs_helix
@pytest.mark.parametrize("seed", range(20))
def test_adapter_decides_like_direct_calls_into_helix(seed):
    h = _live_core()
    rng = random.Random(seed)
    cfg = build_config("helix_arbiter")
    ours = HelixArbiterAdapter(cfg)
    theirs = h.Arbiter([h.SourceSpec(s.name, s.topic, s.priority, s.timeout_s)
                        for s in sorted(cfg.sources, key=lambda s: s.name)],
                       cfg.hold_timeout_s, h.Limits(cfg.max_abs_linear, cfg.max_abs_angular))
    names = {s.topic: s.name for s in cfg.sources}
    t, seq, hseq, epoch, held, ticks = 0.0, 0, 0, 1, False, 0
    while t < 8.0:
        t = round(t + rng.choice([0.003, 0.007, 0.011, 0.02, 0.05, 0.2]), 6)
        r = rng.random()
        seq += 1
        if r < 0.55:
            topic = rng.choice(sorted(names))
            vx, vy, wz = _value(rng), _value(rng), _value(rng)
            lz, ax, ay = (rng.choice([0.0, 0.0, 0.0, 0.2, math.nan, "fast"]) for _ in range(3))
            e = msg(t, topic, {"linear": {"x": vx, "y": vy, "z": lz},
                               "angular": {"x": ax, "y": ay, "z": wz}}, seq=seq)
            ours.on_event(e, e.t_ns)
            theirs.on_source(names[topic], (vx, vy, lz), (ax, ay, wz), e.t_ns / NS)
        elif r < 0.75:
            held = not held if rng.random() < 0.15 else held
            epoch += rng.random() < 0.05
            hseq = hseq + 1 if rng.random() > 0.1 else max(0, hseq - 2)
            e = hold(t, held, seq, eseq=hseq, epoch=epoch)
            ours.on_event(e, e.t_ns)
            theirs.on_hold(held, "f" if held else "", epoch, hseq, e.t_ns / NS)
            if held:   # what arbiter_node._on_hold does
                a, b = ours.take_publication(e.t_ns), theirs.decide(e.t_ns / NS)
                assert (a.reason, a.source, a.robot_raw) == (
                    b.reason, b.source, (b.command.vx, b.command.vy, b.command.wz))
        else:
            ticks += 1
            ns = int(round(t * NS))
            a, b = ours.tick(ns), theirs.decide(ns / NS)
            assert (a.reason, a.source, a.robot_raw) == (
                b.reason, b.source, (b.command.vx, b.command.vy, b.command.wz)), (seed, t)
    assert ticks > 20


def test_scenario_traces_are_deterministic():
    cfg = build_config("helix_arbiter")
    for sc in SCENARIOS.values():
        a = run_trace(HelixArbiterAdapter(cfg), events_for(sc), sc.duration_s)
        b = run_trace(HelixArbiterAdapter(cfg), events_for(sc), sc.duration_s)
        assert a == b
