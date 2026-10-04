"""Smoke test for the two-minute replay-to-prevention demo."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "examples" / "demo_replay_prevention_loop.py"


def test_replay_prevention_demo_completes_from_external_cwd(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: recorded incident produced structured evidence" in result.stdout
    assert "PASS: prevention rule retained source provenance" in result.stdout
    assert "PASS: matching launch condition was blocked" in result.stdout
    assert "PASS: nearby valid configuration passed" in result.stdout
    assert "BOUNDARY: offline deterministic replay; no live robot validation" in result.stdout
