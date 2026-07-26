"""Public telemetry-health demo regression."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="public ROS 2 demo requires rclpy")
pytest.importorskip(
    "geometry_msgs.msg", reason="public ROS 2 demo requires geometry_msgs"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "examples" / "demo_runtime_telemetry_health.py"


def test_public_demo_proves_presence_gap_and_nearby_healthy_case(
    tmp_path: Path,
) -> None:
    domain_start = 20 + zlib.crc32(f"{os.getpid()}:{tmp_path}".encode()) % 180
    completed = subprocess.run(
        [
            sys.executable,
            str(DEMO),
            "--out",
            str(tmp_path / "demo"),
            "--domain-start",
            str(domain_start),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=24,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    summary = json.loads(
        (tmp_path / "demo" / "demo_summary.json").read_text(encoding="utf-8")
    )
    assert summary["fixture_is_genuine_go2_data"] is False
    assert summary["passed"] is True
    assert all(summary["checks"].values())
    assert summary["topic_presence_comparison"]["passed"] is True
    assert summary["publisher_present_silence"]["status"] == "blocked"
    assert summary["publisher_present_silence"]["reason"] == "stale"
    assert summary["nearby_healthy"]["status"] == "passed"
