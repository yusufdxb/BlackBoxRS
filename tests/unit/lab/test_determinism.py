"""Determinism gate: same evidence + config + faults -> byte-identical result.

Compared in full (canonical JSON of the whole result: event order, findings,
invariants, verdict, causal timeline). Nothing is filtered out: the result
carries no wall time, no temporary path and no random id. Repeats run in
this process and in fresh interpreters with different hash seeds, which
would expose any ordering that leaks from set or dict iteration.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from blackboxrs.lab import load_evidence, replay
from blackboxrs.lab.case import load_case
from blackboxrs.lab.clock import RealtimePacer
from blackboxrs.lab.values import canonical_json

from .conftest import CASES, LAB, ROOT

REPEATS = 3


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_repeated_replay_is_identical(path):
    c = load_case(path)
    ev = load_evidence(c.evidence)
    runs = {canonical_json(replay(ev, c.config)) for _ in range(REPEATS)}
    assert len(runs) == 1
    # a freshly loaded copy of the evidence gives the same bytes too
    assert canonical_json(replay(load_evidence(c.evidence), c.config)) in runs


def test_pacing_and_stepping_do_not_change_the_result():
    c = load_case(LAB / "cases" / "teleop_vs_stop__twist_mux_legacy.json")
    ev = load_evidence(c.evidence)
    fast = canonical_json(replay(ev, c.config))
    slept: list[float] = []
    seen: list[dict] = []
    paced = canonical_json(replay(ev, c.config, pacer=RealtimePacer(1.0, sleep=slept.append),
                                  observer=seen.append))
    assert paced == fast
    assert abs(sum(slept) - 6.0) < 0.05, "real-time pacing covers the replayed span"
    assert len(seen) == len(__import__("json").loads(fast)["timeline"])


CLI_CASES = ["stale_command__twist_mux_legacy", "duplicate_and_reorder_hold",
             "transport_loss__robot_host", "clock_skew__stamp_freshness"]


@pytest.mark.parametrize("name", CLI_CASES)
def test_identical_across_processes_and_hash_seeds(name):
    case = os.path.join("examples", "replay_lab", "cases", f"{name}.json")
    outs = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", "from blackboxrs.cli.app import cli; cli()",
             "lab", "replay", case, "--json", "-"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode in (0, 1, 3, 4), proc.stderr
        outs.add(proc.stdout)
    assert len(outs) == 1
