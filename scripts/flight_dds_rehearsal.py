#!/usr/bin/env python3
"""Live DDS rehearsal of the flight recorder on SYNTHETIC traffic.

Starts ``robot-blackbox flight record`` as a separate process, then publishes
a synthetic GO2/HELIX STOP sequence (moving -> fault -> STOP_AND_HOLD ->
hold -> arbiter zero -> StopMove 1003 -> odometry deceleration -> stopped)
as real ROS 2 messages, in real time, and prints the resulting bundle.

This script is test tooling. It publishes on /cmd_vel and /api/sport/request,
so it refuses to run unless DDS is confined to this machine:
ROS_LOCALHOST_ONLY=1 and a non-zero ROS_DOMAIN_ID, and no GO2 topic
(/lowstate, /sportmodestate, /utlidar/*) is visible on the graph. The only
sport API id it ever publishes besides StopMove (1003) is the Move (1008)
that the synthetic sink "sends" before the fault, into a localhost-only
domain where no robot can be listening.

Needs helix_msgs and unitree_api importable (source their install/setup.bash).
The odometry is a first-order lag model. Nothing here is hardware evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
# The recorder subprocess must import this checkout, not another install.
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(REPO)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])

ROBOT_TOPICS = ("/lowstate", "/sportmodestate", "/lf/lowstate", "/utlidar/robot_odom",
                "/utlidar/imu", "/api/sport/response")


def refuse(msg: str) -> None:
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="evidence directory for the rehearsal")
    ap.add_argument("--scenario", default="stopmove")
    ap.add_argument("--duration", type=float, default=30.0)
    args = ap.parse_args()

    if os.environ.get("ROS_LOCALHOST_ONLY") != "1":
        refuse("set ROS_LOCALHOST_ONLY=1")
    if os.environ.get("ROS_DOMAIN_ID", "0") in ("", "0"):
        refuse("set a non-zero ROS_DOMAIN_ID reserved for rehearsal")

    import rclpy
    from rosidl_runtime_py import set_message_fields

    from blackboxrs.flight import synthetic
    from blackboxrs.flight.recorder import resolve_type

    rclpy.init()
    node = rclpy.create_node("bbrs_rehearsal_publisher")
    time.sleep(1.5)  # discovery
    seen = dict(node.get_topic_names_and_types())
    live_robot = [t for t in ROBOT_TOPICS if t in seen]
    if live_robot:
        refuse(f"GO2 topics visible on this domain: {live_robot}")

    sc = synthetic.scenario(args.scenario, duration_s=args.duration)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rec = subprocess.Popen(
        [sys.executable, "-m", "blackboxrs", "flight", "record", "--profile", "go2",
         "--evidence-dir", str(out), "--experiment", f"dds-rehearsal:{args.scenario}",
         "--session", f"dds_rehearsal_{args.scenario}", "--synthetic", "--duration",
         str(args.duration + 20.0)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3.0)  # recorder up and subscriptions matched

    t0 = time.time()
    em = synthetic.generate(sc, t_wall0=t0)
    pubs = {}
    from rclpy.qos import qos_profile_sensor_data
    for _, topic, mtype, _ in em.emissions:
        if topic not in pubs:
            cls, why = resolve_type(mtype)
            if cls is None:
                refuse(f"{mtype} not importable ({why}); source helix and unitree installs")
            qos = qos_profile_sensor_data if topic == "/utlidar/robot_odom" else 10
            pubs[topic] = (node.create_publisher(cls, topic, qos), cls)
    time.sleep(1.0)
    start = time.monotonic()
    sent = 0
    for t_rel, topic, mtype, data in sorted(em.emissions, key=lambda e: e[0]):
        delay = start + t_rel - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        pub, cls = pubs[topic]
        msg = cls()
        set_message_fields(msg, data)
        pub.publish(msg)
        sent += 1
    node.destroy_node()
    rclpy.shutdown()
    try:
        output, _ = rec.communicate(timeout=args.duration + 30)
    except subprocess.TimeoutExpired:
        rec.send_signal(signal.SIGINT)
        output, _ = rec.communicate(timeout=20)
    print(output)
    print(f"published {sent} synthetic messages; recorder exit {rec.returncode}")
    bundles = [line.strip() for line in output.splitlines() if "/inc_" in line]
    for b in bundles:
        rep = json.loads((Path(b) / "report.json").read_text())
        print(f"bundle {b}")
        print("  synthetic:", rep["synthetic"], "status:", rep["status"],
              "trigger:", rep["trigger"]["type"])
        for k in rep["chain_order"]:
            st = rep["chain"][k]
            print(f"  {k:<24} {st['status']}")
        for s in rep["key_spans"]:
            unc = f" +/- {s['uncertainty_s']}" if s.get("uncertainty_s") is not None else ""
            print(f"  {s['from']} -> {s['to']}: {s['value_s']}{unc} s [{s['basis']}; "
                  f"{s['clock_domain']}]")
        print("  recorder receipt delay:", rep["recorder_receipt_delay"])
        print("  verdicts:", {v["id"]: v["result"] for v in rep["verdicts"]})
    sys.exit(0 if bundles else 1)


if __name__ == "__main__":
    main()
