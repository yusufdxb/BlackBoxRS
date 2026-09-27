"""The frozen HELIX decision traces are current, and the C++ model matches HELIX live.

Runs only with HELIX_SRC set (HELIX is a separate repository). The frozen
file examples/replay_lab/arbiter_parity/helix_arbiter_core.json records the
SHA-256 of the arbiter_core.py it was generated from; if HELIX changed, this
fails until the traces are regenerated. The live check regenerates traces
from the checkout and runs the C++ parity test binary on them.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import BIN, ROOT, needs_cpp

HELIX = os.environ.get("HELIX_SRC")
CORE = Path(HELIX or "/nonexistent") / "src/helix_arbiter/helix_arbiter/arbiter_core.py"
FROZEN = ROOT / "examples/replay_lab/arbiter_parity/helix_arbiter_core.json"
pytestmark = [needs_cpp, pytest.mark.skipif(not CORE.is_file(), reason="set HELIX_SRC")]


def test_frozen_traces_match_the_helix_checkout():
    frozen = json.loads(FROZEN.read_text())["helix_arbiter_core"]["sha256"]
    assert frozen == hashlib.sha256(CORE.read_bytes()).hexdigest(), \
        "HELIX arbiter_core.py changed: run scripts/cpp/make_arbiter_traces.py"


def test_cpp_model_matches_helix_live(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts" / "cpp"))
    import make_arbiter_traces as mat

    monkeypatch.setattr(mat, "OUT", tmp_path / "traces.json")
    assert mat.main() == 0
    test_bin = BIN.parents[1] / "tests" / "test_helix_parity"
    if not test_bin.is_file():
        pytest.skip("C++ tests not built")
    env = {**os.environ, "BLACKBOXRS_ARBITER_TRACES": str(tmp_path / "traces.json")}
    out = subprocess.run([str(test_bin)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout[-3000:]
