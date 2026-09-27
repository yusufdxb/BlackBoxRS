#!/usr/bin/env python3
"""Replay one case (or bundle + options) with Python Replay Lab and the C++
engine and print every difference between the two result documents.

    python scripts/cpp/diff_replay.py examples/replay_lab/cases/nominal_motion.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_BIN = ROOT / "cpp" / "build" / "release" / "tools" / "blackboxrs"


def cpp_bin() -> Path:
    return Path(os.environ.get("BLACKBOXRS_BIN", DEFAULT_BIN))


def python_result(target: str, extra_faults: list[str]) -> dict:
    from blackboxrs.lab.case import load_case
    from blackboxrs.lab.engine import ReplayConfig, replay
    from blackboxrs.lab.evidence import load_evidence
    from blackboxrs.lab.faults import parse_cli_fault
    from dataclasses import replace

    if target.endswith(".json"):
        case = load_case(target)
        cfg, ev_path = case.config, case.evidence
    else:
        cfg, ev_path = ReplayConfig(), Path(target)
    if extra_faults:
        cfg = replace(cfg, faults=cfg.faults + tuple(
            parse_cli_fault(t, len(cfg.faults) + i) for i, t in enumerate(extra_faults)))
    ev = load_evidence(ev_path, label=str(ev_path))
    return json.loads(json.dumps(replay(ev, cfg)))


def cpp_result(target: str, extra_faults: list[str]) -> dict:
    cmd = [str(cpp_bin()), "replay", target, "--json", "-"]
    for f in extra_faults:
        cmd += ["--inject", f]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if out.returncode not in (0, 1, 3, 4):
        raise RuntimeError(f"C++ replay failed ({out.returncode}): {out.stderr}")
    return json.loads(out.stdout)


def diff(a, b, path="$", out=None):
    out = [] if out is None else out
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append(f"{path}: python={a!r} cpp={b!r}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in {'cpp' if k not in a else 'python'}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length python={len(a)} cpp={len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: python={a!r} cpp={b!r}")
    return out


def main() -> int:
    target, faults = sys.argv[1], sys.argv[2:]
    d = diff(python_result(target, faults), cpp_result(target, faults))
    for line in d[:80]:
        print(line)
    print(f"{len(d)} difference(s)")
    return 1 if d else 0


if __name__ == "__main__":
    sys.exit(main())
