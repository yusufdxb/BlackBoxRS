"""Replay determinism of the C++ engine: 100 runs per golden case, identical bytes.

``blackboxrs verify --repeat 100`` replays each case 100 times in one process
and requires identical canonical output; two separate processes must agree
too (nothing may depend on addresses, hash seeds or time).
"""

from __future__ import annotations

import json

from .conftest import needs_cpp, run_cpp

pytestmark = needs_cpp


def test_hundred_repeats_are_identical(tmp_path):
    out = tmp_path / "v.json"
    run_cpp("verify", "examples/replay_lab/cases", "--repeat", "100", "--json", str(out))
    rows = json.loads(out.read_text())["cases"]
    assert len(rows) == 30
    assert all(r["ok"] and r["deterministic"] and r["repeats"] == 100 for r in rows), \
        [r["case"] for r in rows if not (r["ok"] and r["deterministic"])]


def test_separate_processes_agree(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    run_cpp("verify", "--repeat", "1", "--json", str(a))
    run_cpp("verify", "--repeat", "1", "--json", str(b))
    da = {r["case"]: r["result_sha256"] for r in json.loads(a.read_text())["cases"]}
    db = {r["case"]: r["result_sha256"] for r in json.loads(b.read_text())["cases"]}
    assert da == db


def test_digests_equal_the_python_reference(tmp_path):
    from blackboxrs.lab.case import load_case
    from blackboxrs.lab.engine import replay
    from blackboxrs.lab.evidence import load_evidence
    from blackboxrs.lab.values import digest
    from .conftest import CASES

    out = tmp_path / "c.json"
    run_cpp("verify", "--repeat", "1", "--json", str(out))
    cpp = {r["case"]: r["result_sha256"] for r in json.loads(out.read_text())["cases"]}
    for f in sorted(CASES.glob("*.json")):
        case = load_case(f)
        ev = load_evidence(case.evidence, label=str(case.evidence.relative_to(CASES.parents[2])))
        assert cpp[case.name] == digest(replay(ev, case.config)), case.name
