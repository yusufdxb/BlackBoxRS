#!/usr/bin/env python3
"""Arbiter Parity Gate: PASS only when Replay Lab's arbitration matches the deployed code.

Criteria (all must hold; anything unexplained is FAIL, anything unverifiable
is UNRESOLVED, never PASS):

  G1 helix_provenance   the vendored HELIX files equal HELIX at the pinned commit
                        (needs --helix-src; without it: UNRESOLVED)
  G2 helix_parity       the helix_arbiter adapter reproduces every recorded real
                        arbiter_node run (docs/parity/helix_arbiter_node.json)
  G3 twist_mux_parity   the twist_mux model reproduces every recorded run of the real
                        twist_mux binary, including silences and the tie-break
  G4 scenario_freeze    every recording used the frozen scenario scripts
  G5 determinism        every golden case replays byte-identically (3 repeats)

The existing test suite is the CI "Lint + tests" job; this gate does not repeat it.

    python scripts/parity/gate.py --helix-src ~/workspace/helix [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from blackboxrs.lab.case import load_case  # noqa: E402
from blackboxrs.lab.engine import replay  # noqa: E402
from blackboxrs.lab.evidence import load_evidence  # noqa: E402
from blackboxrs.lab.helix import VENDOR, HelixArbiterAdapter, provenance  # noqa: E402
from blackboxrs.lab.parity import (  # noqa: E402
    HOLD,
    SCENARIOS,
    compare_helix,
    compare_twist_mux,
    events_for,
)
from blackboxrs.lab.sut import TwistMuxModel, build_config  # noqa: E402
from blackboxrs.lab.values import canonical_json  # noqa: E402

PARITY = ROOT / "docs" / "parity"


def load(name: str) -> dict:
    return json.loads((PARITY / f"{name}.json").read_text())


def g1(helix_src: Path | None) -> tuple[str, list[str]]:
    if helix_src is None:
        return "UNRESOLVED", ["no --helix-src: vendored files not compared with HELIX"]
    prov = provenance()
    bad = []
    for name, meta in prov["files"].items():
        r = subprocess.run(["git", "-C", str(helix_src), "show",
                            f"{prov['commit']}:{meta['path']}"], capture_output=True)
        if r.returncode != 0:
            return "UNRESOLVED", [f"commit {prov['commit']} not in {helix_src}"]
        if (VENDOR / name).read_bytes() != r.stdout:
            bad.append(f"{name} differs from HELIX {prov['commit'][:7]}:{meta['path']}")
    return ("FAIL" if bad else "PASS"), bad or [f"{len(prov['files'])} files identical to "
                                                  f"HELIX {prov['commit'][:7]}"]


def g2() -> tuple[str, list[str]]:
    rec = load("helix_arbiter_node")
    cfg = build_config("helix_arbiter")
    res = [compare_helix(r, lambda: HelixArbiterAdapter(cfg)) for r in rec["scenarios"]]
    bad = [f"{r['scenario']}: {p}" for r in res for p in r["problems"]]
    return ("FAIL" if bad else "PASS"), bad or [
        f"{len(res)}/{len(res)} scenarios: decision sequence and transition times match the "
        f"real arbiter_node (HELIX {rec['environment']['helix_commit'][:7]})"]


def g3() -> tuple[str, list[str]]:
    rec = load("twist_mux")
    res = [compare_twist_mux(r, lambda: TwistMuxModel(build_config("twist_mux_legacy")))
           for r in rec["scenarios"]]
    for name in ("twist_mux_tie", "twist_mux_tie_reversed"):
        r = load(name)["scenarios"][0]
        topics = yaml.safe_load(r["config"])["twist_mux"]["ros__parameters"]["topics"]
        cfg = build_config("twist_mux_legacy", {"sources": [
            {"name": n, "topic": c["topic"], "priority": c["priority"],
             "timeout_s": c["timeout"]} for n, c in topics.items()]})
        res.append(compare_twist_mux({**r, "scenario": name}, lambda: TwistMuxModel(cfg)))
    bad = [f"{r['scenario']}: {p}" for r in res for p in r["problems"]]
    return ("FAIL" if bad else "PASS"), bad or [
        f"{len(res)}/{len(res)} runs: every message, value and silence matches the real "
        f"twist_mux {rec['environment']['twist_mux_package']} on HELIX twist_mux.yaml"]


def g4() -> tuple[str, list[str]]:
    bad = []
    for name, legacy in (("helix_arbiter_node", False), ("twist_mux", True)):
        for r in load(name)["scenarios"]:
            want = [(round(e.t_ns / 1e9, 6), e.topic)
                    for e in events_for(SCENARIOS[r["scenario"]], legacy=legacy)
                    if not (legacy and e.topic == HOLD)]
            if [(s[0], s[2]) for s in r["sent"]] != want:
                bad.append(f"{name}/{r['scenario']}: recorded script differs from the frozen one")
    return ("FAIL" if bad else "PASS"), bad or ["recordings match the frozen scenario scripts"]


def g5() -> tuple[str, list[str]]:
    bad = []
    cases = sorted((ROOT / "examples" / "replay_lab" / "cases").glob("*.json"))
    for p in cases:
        c = load_case(p)
        ev = load_evidence(c.evidence, label=str(c.evidence.relative_to(ROOT)))
        runs = {canonical_json(replay(ev, c.config)) for _ in range(3)}
        if len(runs) != 1:
            bad.append(f"{c.name}: nondeterministic")
    return ("FAIL" if bad else "PASS"), bad or [f"{len(cases)} cases x3 byte-identical"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--helix-src", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    src = args.helix_src.expanduser() if args.helix_src else None
    checks = {"G1 helix_provenance": g1(src), "G2 helix_parity": g2(),
              "G3 twist_mux_parity": g3(), "G4 scenario_freeze": g4(), "G5 determinism": g5()}
    for name, (status, notes) in checks.items():
        print(f"{status:<10} {name}")
        for n in notes[:10]:
            print(f"           {n}")
    states = {s for s, _ in checks.values()}
    verdict = "FAIL" if "FAIL" in states else "UNRESOLVED" if "UNRESOLVED" in states else "PASS"
    print(f"\nARBITER PARITY GATE: {verdict}")
    if args.json:
        args.json.write_text(json.dumps({"verdict": verdict, "checks": {
            k: {"status": s, "notes": n} for k, (s, n) in checks.items()}}, indent=2) + "\n")
    return {"PASS": 0, "FAIL": 1, "UNRESOLVED": 2}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
