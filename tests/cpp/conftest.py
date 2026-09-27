"""Differential tests: the C++ runtime against the Python reference.

These need the C++ CLI. They use $BLACKBOXRS_BIN, else the first of
cpp/build/{release,debug}/tools/blackboxrs, and skip when none exists (a
Python-only checkout).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "examples" / "replay_lab" / "cases"
EVIDENCE = ROOT / "examples" / "replay_lab" / "evidence"


def _find_bin() -> Path | None:
    env = os.environ.get("BLACKBOXRS_BIN")
    if env:
        return Path(env)
    for build in ("release", "debug"):
        p = ROOT / "cpp" / "build" / build / "tools" / "blackboxrs"
        if p.is_file():
            return p
    return None


BIN = _find_bin()
needs_cpp = pytest.mark.skipif(BIN is None or not BIN.is_file(),
                               reason="C++ blackboxrs not built (set BLACKBOXRS_BIN)")


def run_cpp(*args: str, check_codes=(0,)) -> subprocess.CompletedProcess:
    out = subprocess.run([str(BIN), *args], capture_output=True, text=True, cwd=ROOT)
    if out.returncode not in check_codes:
        raise AssertionError(f"blackboxrs {' '.join(args)} exited {out.returncode}:\n"
                             f"{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    return out


def cpp_replay_json(target: str, *extra: str) -> str:
    out = run_cpp("replay", target, "--json", "-", *extra, check_codes=(0, 1, 3, 4))
    return out.stdout.strip()


def python_replay_json(target: str, faults: tuple[str, ...] = (), sut: str | None = None) -> str:
    from dataclasses import replace

    from blackboxrs.lab.case import load_case
    from blackboxrs.lab.engine import ReplayConfig, replay
    from blackboxrs.lab.evidence import load_evidence
    from blackboxrs.lab.faults import parse_cli_fault
    from blackboxrs.lab.values import canonical_json

    if target.endswith(".json"):
        case = load_case(target)
        cfg, ev_path = case.config, case.evidence
    else:
        cfg, ev_path = ReplayConfig(), Path(target)
    if sut == "observed":
        cfg = replace(cfg, sut_mode="observed", overrides={})
    elif sut:
        cfg = replace(cfg, sut_mode="reference", preset=sut)
    if faults:
        cfg = replace(cfg, faults=cfg.faults + tuple(
            parse_cli_fault(t, len(cfg.faults) + i) for i, t in enumerate(faults)))
    ev = load_evidence(ev_path, label=str(ev_path))
    return canonical_json(replay(ev, cfg))


def first_difference(a: str, b: str) -> str:
    da, db = json.loads(a), json.loads(b)

    def walk(x, y, path="$"):
        if type(x) is not type(y) and not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
            return f"{path}: python={x!r} cpp={y!r}"
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                if k not in x or k not in y:
                    return f"{path}.{k}: missing on one side"
                r = walk(x[k], y[k], f"{path}.{k}")
                if r:
                    return r
        elif isinstance(x, list):
            if len(x) != len(y):
                return f"{path}: lengths python={len(x)} cpp={len(y)}"
            for i, (p, q) in enumerate(zip(x, y)):
                r = walk(p, q, f"{path}[{i}]")
                if r:
                    return r
        elif x != y:
            return f"{path}: python={x!r} cpp={y!r}"
        return ""
    return walk(da, db) or "texts differ but values are equal (number formatting)"
