"""Replay Lab never reads the wall clock, randomness or ids for a result.

Checked on the import graph (AST), not by pattern-matching call text: a
module that never imports a clock, random or id source cannot read one.
Only clock.py may import ``time``, and only to sleep for pacing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import blackboxrs.lab as lab

FORBIDDEN = {"time", "datetime", "random", "uuid", "secrets", "timeit"}
FORBIDDEN_OS = {"urandom", "getrandom", "times"}


def _modules() -> list[Path]:
    pkg = Path(lab.__file__).parent
    flight = pkg.parent / "flight"
    # the flight modules the replay drives: recorder core, feeder, analyzer, records, profile
    reused = [flight / n for n in ("core.py", "replay.py", "analysis.py", "records.py",
                                   "profile.py")]
    # the vendored HELIX arbiter the replay executes
    return sorted(pkg.glob("*.py")) + sorted((pkg / "vendor" / "helix").glob("*.py")) + reused


def _imports(tree: ast.AST) -> list[tuple[str, str, int]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name.split(".")[0], "", node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out += [(node.module.split(".")[0], a.name, node.lineno) for a in node.names]
    return out


def test_no_clock_randomness_or_id_imports():
    hits = []
    for f in _modules():
        tree = ast.parse(f.read_text())
        for mod, name, line in _imports(tree):
            if mod in FORBIDDEN and not (f.name == "clock.py" and f.parent.name == "lab"
                                         and mod == "time" and name == ""):
                hits.append(f"{f.parent.name}/{f.name}:{line} imports {mod} {name}".strip())
            if mod == "os" and name in FORBIDDEN_OS:
                hits.append(f"{f.parent.name}/{f.name}:{line} imports os.{name}")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "os" and node.attr in FORBIDDEN_OS):
                hits.append(f"{f.parent.name}/{f.name}:{node.lineno} uses os.{node.attr}")
    assert hits == []


def test_clock_py_uses_time_only_to_sleep():
    tree = ast.parse((Path(lab.__file__).parent / "clock.py").read_text())
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "time"}
    assert used == {"sleep"}


def test_the_lint_catches_what_it_should():
    bad = "import time\nfrom time import monotonic_ns\nimport random as r\nimport uuid\n"
    mods = {m for m, _, _ in _imports(ast.parse(bad))}
    assert {"time", "random", "uuid"} <= mods
