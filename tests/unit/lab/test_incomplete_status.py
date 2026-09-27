"""Evidence the recorder could not write completely is never replayed as complete.

``write_failed`` is written by both recorders when a record could not reach
the disk; ``complete_with_loss`` by the C++ recorder when messages were lost
before its core. Before this check the lab accepted both as finalized
evidence, so a replay of a damaged capture could PASS.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from blackboxrs.lab.engine import ReplayConfig, replay
from blackboxrs.lab.evidence import EvidenceError, load_evidence

EVIDENCE = Path(__file__).resolve().parents[3] / "examples/replay_lab/evidence/nominal_motion"


@pytest.mark.parametrize("status", ["write_failed", "complete_with_loss", "interrupted",
                                    "pipeline_failed"])
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


def test_unknown_manifest_schema_is_refused(tmp_path):
    b = tmp_path / "b"
    shutil.copytree(EVIDENCE, b)
    m = json.loads((b / "manifest.json").read_text())
    m["schema"] = "blackboxrs.flight.manifest.v9"
    (b / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(EvidenceError, match="unsupported evidence schema"):
        load_evidence(b, allow_partial=True)


def _with_integrity(tmp_path, *, complete=True):
    """The golden bundle plus an integrity.json that matches it (as the C++ recorder writes)."""
    b = tmp_path / "b"
    shutil.copytree(EVIDENCE, b)
    data = (b / "records.jsonl").read_bytes()
    (b / "integrity.json").write_text(json.dumps({
        "schema": "blackboxrs.integrity.v1", "records": data.count(b"\n"), "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "chunks": [], "complete": complete}))
    return b


def test_matching_integrity_record_is_accepted(tmp_path):
    assert not load_evidence(_with_integrity(tmp_path)).partial


def test_records_that_disagree_with_integrity_are_refused(tmp_path):
    b = _with_integrity(tmp_path)
    data = bytearray((b / "records.jsonl").read_bytes())
    i = data.index(b'"linear"')
    data[i + 1] = ord("L")  # still valid JSON, same length
    (b / "records.jsonl").write_bytes(bytes(data))
    with pytest.raises(EvidenceError, match="sha256 does not match integrity.json"):
        load_evidence(b)
    (b / "records.jsonl").write_bytes(bytes(data) + b"\n")
    ev = load_evidence(b, allow_partial=True)
    lines = data.count(b"\n")
    assert ev.read_info["problems"] == [
        f"records.jsonl is {len(data) + 1} bytes, integrity.json says {len(data)}",
        f"records.jsonl has {lines + 1} lines, integrity.json says {lines}",
        "records.jsonl sha256 does not match integrity.json"]


def test_integrity_marked_incomplete_is_refused(tmp_path):
    with pytest.raises(EvidenceError, match="marks the capture incomplete"):
        load_evidence(_with_integrity(tmp_path, complete=False))
