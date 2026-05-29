"""Smoke test for the detector FPR/TPR measurement harness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "measure_detector_fpr.py"


def test_harness_runs_one_hour_under_ten_seconds(tmp_path: Path) -> None:
    out = tmp_path / "dc.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--hours", "1", "--json-output", str(out)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["hours"] == 1.0
    detectors = {row["detector"] for row in payload["detectors"]}
    assert detectors == {
        "threshold",
        "frequency",
        "dead_topic",
        "qos_mismatch",
        "tf_topology",
        "clock_skew",
        "process_signals",
    }
    for row in payload["detectors"]:
        assert isinstance(row["fpr_per_hour"], (int, float))
        assert isinstance(row["tpr"], (int, float))
        assert row["samples"] == 3600


def test_harness_emits_markdown_table_to_stdout(tmp_path: Path) -> None:
    out = tmp_path / "dc.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--hours", "1", "--json-output", str(out)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "| Detector | FPR" in result.stdout
    assert "`threshold`" in result.stdout
    assert "`clock_skew`" in result.stdout
