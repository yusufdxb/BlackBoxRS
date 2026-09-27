"""Evidence the recorder could not write completely is never replayed as complete.

``write_failed`` is written by both recorders when a record could not reach
the disk; ``complete_with_loss`` by the C++ recorder when messages were lost
before its core. Before this check the lab accepted both as finalized
evidence, so a replay of a damaged capture could PASS.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from blackboxrs.lab.engine import ReplayConfig, replay
from blackboxrs.lab.evidence import EvidenceError, load_evidence

EVIDENCE = Path(__file__).resolve().parents[3] / "examples/replay_lab/evidence/nominal_motion"


@pytest.mark.parametrize("status", ["write_failed", "complete_with_loss"])
def test_incomplete_status_is_refused_or_capped(tmp_path, status):
    b = tmp_path / "b"
    shutil.copytree(EVIDENCE, b)
    m = json.loads((b / "manifest.json").read_text())
    m["status"] = status
    (b / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(EvidenceError):
        load_evidence(b)
    ev = load_evidence(b, allow_partial=True)
    assert ev.partial
    result = replay(ev, ReplayConfig())
    assert result["verdict"]["result"] == "INCOMPLETE"
