#!/usr/bin/env python3
"""Regenerate the Replay Lab golden evidence bundles.

The bundles in examples/replay_lab/evidence/ are SYNTHETIC. They are made by
the flight recorder's own synthetic GO2 + HELIX traffic generator
(blackboxrs/flight/synthetic.py) fed through the real recorder core and
bundle writer, with a fixed session so the output is byte-identical on every
run. tests/unit/lab/test_fixtures.py regenerates them and fails if the
committed copies drift.

    python scripts/generate_replay_lab_evidence.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import replace
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blackboxrs.flight import load_profile, synthetic  # noqa: E402

PROFILE = ROOT / "examples" / "replay_lab" / "fixture_profile.yaml"
FILES = ("manifest.json", "records.jsonl", "report.json", "report.md")

SESSION = {
    "session_id": "replay_lab_fixture",
    "experiment": "replay-lab golden evidence (synthetic)",
    "hostname": "synthetic",
    "platform": "synthetic",
    "ros_distro": "humble",
    "rmw_implementation": "rmw_cyclonedds_cpp",
    "blackboxrs_version": "fixture",
    "blackboxrs_git_sha": None,
    "blackboxrs_git_dirty": None,
    "experiment_repos": [],
    "profile_name": "replay_lab_fixture",
}

# Both runs: 6.5 s of traffic, forward command 0.15 m/s from t = 2 s, odometry
# and lowstate at 20 Hz from the robot host with its clock 1.734 s ahead of
# the payload (the synthetic generator's default skew).
COMMON = {"duration_s": 6.5, "odom_hz": 20.0, "lowstate_hz": 20.0}
EVIDENCE = {
    # no fault; an operator marker at 3 s makes the recorder write the bundle
    "nominal_motion": dict(COMMON, name="normal_motion", fault_at_s=None, marker_at_s=3.0),
    # an injected benign HELIX fault at 3 s: STOP_AND_HOLD, arbiter zero, robot stops
    "clean_stop": dict(COMMON, name="stopmove", fault_at_s=3.0),
}


def _pin_finalize_time(bundle: Path) -> None:
    """The writer stamps the real wall time it finalized at; pin it to the last
    record's (synthetic) wall time so the committed bundle is reproducible."""
    last = json.loads((bundle / "records.jsonl").read_text().splitlines()[-1])
    mp = bundle / "manifest.json"
    text, n = re.subn(r'"finalized_wall_ns": \d+', f'"finalized_wall_ns": {last["t_wall_ns"]}',
                      mp.read_text())
    if n != 1:
        raise SystemExit(f"{bundle}: finalized_wall_ns not found in manifest")
    mp.write_text(text)


def generate(out: Path) -> list[Path]:
    # Record the profile by its repository path, not this checkout's absolute
    # path, so the committed bundles are identical on every machine.
    profile = replace(load_profile(str(PROFILE), evidence_dir=str(out)),
                      source=str(PROFILE.relative_to(ROOT)))
    made = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, kw in EVIDENCE.items():
            kw = dict(kw)
            sc = synthetic.scenario(kw.pop("name"), **kw)
            _, bundles = synthetic.run(profile, sc, Path(tmp) / name, session=dict(SESSION))
            if len(bundles) != 1:
                raise SystemExit(f"{name}: expected one bundle, got {len(bundles)}")
            dest = out / name
            dest.mkdir(parents=True, exist_ok=True)
            for f in FILES:
                shutil.copyfile(Path(bundles[0]) / f, dest / f)
            _pin_finalize_time(dest)
            made.append(dest)
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "examples" / "replay_lab" / "evidence")
    args = ap.parse_args()
    for d in generate(args.out):
        size = sum(p.stat().st_size for p in d.iterdir())
        print(f"{d}  {size / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
