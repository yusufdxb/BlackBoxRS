#!/usr/bin/env python3
"""Recorder cost on real ROS traffic: real message types, real CDR decode.

For each scale of the measured GO2 telemetry load (/lowstate 500 Hz,
/sportmodestate 295 Hz, /utlidar/robot_odom 151 Hz, published by the test
tool load_publisher with the real unitree_go / nav_msgs types), start the C++
recorder with the go2_helix profile, publish for --seconds, and measure the
recorder PROCESS (all its threads: executor callbacks, pipeline, writer):
CPU from /proc/<pid>/stat, peak and final RSS, messages received and dropped,
and whether the evidence validates. The load publisher runs in its own
process and is not counted.

Workstation numbers only. The Orin NX is measured at gate H1
(docs/CPP_HARDWARE_VALIDATION.md) with the same procedure.

    source /opt/ros/humble/setup.bash; source <unitree_ws>/install/setup.bash
    source install/setup.bash
    python3 scripts/cpp/ros_benchmark.py --scales 1,2,5 --seconds 30 --out docs/benchmarks/x.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ticks(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rfind(")") + 2:].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def status_mb(pid: int, key: str) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1]) / 1024.0
    return 0.0


def run_scale(scale: float, seconds: float, work: Path, prefix: str, build: str) -> dict:
    evidence = work / f"x{scale:g}"
    cfg = work / f"runtime_x{scale:g}.yaml"
    text = (ROOT / "configs" / "go2_hardware.yaml").read_text()
    text = re.sub(r"^profile: .*$", f"profile: {ROOT}/blackboxrs/flight/profiles/go2_helix.yaml",
                  text, flags=re.M)
    text = re.sub(r"^  evidence_dir: .*$", f"  evidence_dir: {evidence}", text, flags=re.M)
    text = re.sub(r"^  findings_file: .*$", "  findings_file: null", text, flags=re.M)
    text = re.sub(r"^  publish: true", "  publish: false", text, flags=re.M)
    cfg.write_text(text)
    log_path = work / f"recorder_x{scale:g}.log"
    with open(log_path, "w") as log:
        rec = subprocess.Popen([f"{prefix}/lib/blackboxrs_ros/recorder", "--config", str(cfg)],
                               stdout=log, stderr=subprocess.STDOUT)
        time.sleep(4.0)  # subscriptions and discovery
        hz = os.sysconf("SC_CLK_TCK")
        t0, c0 = time.monotonic(), ticks(rec.pid)
        pub = subprocess.run([f"{build}/load_publisher", "--scale", str(scale),
                              "--seconds", str(seconds)], capture_output=True, text=True)
        t1, c1 = time.monotonic(), ticks(rec.pid)
        rss_end, rss_peak = status_mb(rec.pid, "VmRSS"), status_mb(rec.pid, "VmHWM")
        rec.send_signal(signal.SIGINT)
        rc = rec.wait(timeout=60)
    sent = None
    if pub.returncode == 0:
        sent = int(re.search(r"total sent (\d+)", pub.stdout).group(1))
    m = re.search(r"stopped \(signal\): (\d+) received, (\d+) processed, (\d+) dropped at "
                  r"ingest, (\d+) dropped at shutdown", log_path.read_text())
    received = processed = dropped_ingest = dropped_shutdown = None
    if m:
        received, processed, dropped_ingest, dropped_shutdown = (int(x) for x in m.groups())
    validation = None
    bundles = sorted(evidence.glob("*/inc_*"))
    if bundles:
        v = subprocess.run([str(ROOT / "cpp/build/release/tools/blackboxrs"), "validate",
                            str(bundles[0]), "--json"], capture_output=True, text=True)
        validation = json.loads(v.stdout)["status"]
    return {
        "scale": scale,
        "offered_msgs_per_sec": None if sent is None else sent / seconds,
        "published": sent,
        "received": received,
        "processed": processed,
        "dropped_ingest": dropped_ingest,
        "dropped_at_shutdown": dropped_shutdown,
        "not_received": None if sent is None or received is None else sent - received,
        "recorder_cpu_percent_one_core": 100.0 * (c1 - c0) / hz / (t1 - t0),
        "recorder_rss_mb_end": rss_end,
        "recorder_rss_mb_peak": rss_peak,
        "exit_code": rc,
        "evidence": validation,
        "load_publisher": pub.stderr.strip().splitlines()[-3:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="1,2,5")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    os.environ.setdefault("ROS_DOMAIN_ID", "87")
    prefix = subprocess.run(["ros2", "pkg", "prefix", "blackboxrs_ros"], capture_output=True,
                            text=True, check=True).stdout.strip()
    build = str(ROOT / "build" / "blackboxrs_ros")
    rows = []
    with tempfile.TemporaryDirectory(prefix="bbrs_rosbench_") as tmp:
        for s in (float(x) for x in args.scales.split(",")):
            print(f"scale {s:g} for {args.seconds:g} s ...", file=sys.stderr)
            rows.append(run_scale(s, args.seconds, Path(tmp), prefix, build))
            print(json.dumps(rows[-1]), file=sys.stderr)
    out = {
        "schema": "blackboxrs.ros_benchmark.v1",
        "what": "C++ recorder process on real ROS 2 traffic (real types, real CDR decode), "
                "go2_helix profile, continuous capture with fsync; workstation, not the payload",
        "environment": {"machine": platform.machine(), "kernel": platform.release(),
                        "logical_cpus": os.cpu_count(),
                        "rmw": os.environ.get("RMW_IMPLEMENTATION", "default"),
                        "ros_distro": os.environ.get("ROS_DISTRO")},
        "seconds_per_scale": args.seconds,
        "results": rows,
    }
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
