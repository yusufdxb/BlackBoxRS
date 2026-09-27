"""The committed golden evidence regenerates byte for byte."""

from __future__ import annotations

import subprocess
import sys

from .conftest import LAB, ROOT


def test_committed_evidence_matches_generator(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_replay_lab_evidence.py"),
                    "--out", str(tmp_path)], check=True, capture_output=True, timeout=300)
    committed = sorted(p.relative_to(LAB / "evidence")
                       for p in (LAB / "evidence").rglob("*") if p.is_file())
    fresh = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert committed == fresh
    for rel in committed:
        assert (LAB / "evidence" / rel).read_bytes() == (tmp_path / rel).read_bytes(), rel


def test_evidence_is_labelled_synthetic():
    import json
    for d in (LAB / "evidence").iterdir():
        m = json.loads((d / "manifest.json").read_text())
        assert m["session"]["synthetic"] is True, d.name


def test_every_case_is_documented():
    from .conftest import CASES
    readme = (LAB / "README.md").read_text()
    for p in CASES:
        assert f"`{p.stem}`" in readme, p.stem
