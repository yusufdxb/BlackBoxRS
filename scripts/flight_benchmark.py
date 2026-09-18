#!/usr/bin/env python3
"""Benchmark the Python flight recorder on this machine.

Two parts:

``core``  offline: synthetic records pushed through the recorder core and the
          bundle writer as fast as possible (no ROS). Measures ingest
          throughput and bundle finalization time for a full window.
``live``  real DDS: a publisher process sends a GO2-shaped workload (rates
          from docs/go2_field_notes.md, real unitree_go / nav_msgs / helix_msgs
          types) while ``robot-blackbox flight record`` runs as a separate
          process. Measures recorder CPU and RSS (psutil on its PID), messages
          published vs received per topic, middleware-reported losses,
          recorder receipt delay, and a bundle triggered under load.

``live`` publishes on /cmd_vel and /api/sport/request, so it refuses to run
unless ROS_LOCALHOST_ONLY=1 and ROS_DOMAIN_ID is non-zero, and no GO2 topic
is visible. Every message it sends is synthetic.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
# The recorder subprocess must import this checkout, not another install.
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(REPO)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])

GO2_RATES = {  # topic: (type, Hz)
    "/lowstate": ("unitree_go/msg/LowState", 500.0),
    "/sportmodestate": ("unitree_go/msg/SportModeState", 295.0),
    "/utlidar/robot_odom": ("nav_msgs/msg/Odometry", 151.0),
    "/lf/lowstate": ("unitree_go/msg/LowState", 50.0),
    "/lf/sportmodestate": ("unitree_go/msg/SportModeState", 50.0),
    "/nav/cmd_vel": ("geometry_msgs/msg/Twist", 20.0),
    "/cmd_vel": ("geometry_msgs/msg/Twist", 20.0),
    "/helix/arbiter/status": ("helix_msgs/msg/ArbiterStatus", 20.0),
    "/helix/hold": ("helix_msgs/msg/HelixHold", 10.0),
    "/helix/sink/trace": ("std_msgs/msg/String", 20.0),
    "/api/sport/request": ("unitree_api/msg/Request", 20.0),
    "/api/sport/response": ("unitree_api/msg/Response", 20.0),
}


def bench_core(n_seconds: float, rate: float, out: Path) -> dict:
    from blackboxrs.flight import load_profile
    from blackboxrs.flight.bundle import BundleWriter
    from blackboxrs.flight.core import FlightCore
    from blackboxrs.flight.analysis import analyze
    from blackboxrs.flight.render import render_markdown
    from blackboxrs.flight.records import make_msg_record

    prof = load_profile("go2", evidence_dir=str(out))
    sess = {"session_id": "bench_core", "synthetic": True}
    writers = []

    def factory():
        w = BundleWriter(out / "bench_core", prof, sess, lambda: {}, analyze=analyze,
                         render=render_markdown)
        writers.append(w)
        return w

    core = FlightCore(prof, factory)
    n = int(n_seconds * rate)
    step = int(1e9 / rate)
    t0m, t0w = 10**12, 1_789_000_000 * 10**9
    odom = {"header": {"stamp": {"sec": 1, "nanosec": 0}, "frame_id": "odom"},
            "child_frame_id": "base_link",
            "pose": {"pose": {"position": {"x": 0.0, "y": 0.0, "z": 0.3}}},
            "twist": {"twist": {"linear": {"x": 0.1, "y": 0.0, "z": 0.0}}}}
    proc = psutil.Process()
    proc.cpu_percent(None)
    start = time.perf_counter()
    trig_at = n // 2
    for i in range(n):
        m = t0m + i * step
        core.ingest(make_msg_record(topic="/utlidar/robot_odom", role="odometry",
                                    msg_type="nav_msgs/msg/Odometry", data=odom,
                                    t_mono_ns=m, t_wall_ns=t0w + i * step, dds_src_ns=t0w + i * step))
        if i == trig_at:
            core.mark(m, t0w + i * step, note="bench")
    ingest_s = time.perf_counter() - start
    f0 = time.perf_counter()
    core.shutdown("bench end")
    core.wait_writers()
    finalize_s = time.perf_counter() - f0
    b = Path(core.closed_bundles[0])
    return {"records": n, "ingest_records_per_s": round(n / ingest_s),
            "cpu_percent_during_ingest": proc.cpu_percent(None),
            "rss_mb": round(proc.memory_info().rss / 2**20, 1),
            "bundle_records": sum(1 for _ in (b / "records.jsonl").open()),
            "bundle_mb": round(sum(p.stat().st_size for p in b.iterdir()) / 2**20, 2),
            "finalize_after_shutdown_s": round(finalize_s, 3)}


def refuse(msg: str) -> None:
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(3)


def bench_live(duration: float, scale: float, out: Path) -> dict:
    if os.environ.get("ROS_LOCALHOST_ONLY") != "1":
        refuse("set ROS_LOCALHOST_ONLY=1")
    if os.environ.get("ROS_DOMAIN_ID", "0") in ("", "0"):
        refuse("set a non-zero ROS_DOMAIN_ID")
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from blackboxrs.flight.recorder import resolve_type

    rclpy.init()
    node = rclpy.create_node("bbrs_bench_publisher")
    time.sleep(1.5)
    live = [t for t in ("/lowstate", "/sportmodestate", "/utlidar/robot_odom")
            if t in dict(node.get_topic_names_and_types())]
    if live:
        refuse(f"GO2 topics already on this domain: {live}")
    session = f"bench_live_x{scale:g}"
    rec = subprocess.Popen(
        [sys.executable, "-m", "blackboxrs", "flight", "record", "--profile", "go2",
         "--evidence-dir", str(out), "--session", session, "--synthetic",
         "--experiment", f"benchmark x{scale:g}", "--duration", str(duration + 25)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rp = psutil.Process(rec.pid)
    time.sleep(4.0)
    pubs, counts = {}, {}
    for topic, (mtype, hz) in GO2_RATES.items():
        cls, why = resolve_type(mtype)
        if cls is None:
            refuse(f"{mtype}: {why}")
        pubs[topic] = (node.create_publisher(cls, topic, 50), cls(), hz * scale)
        counts[topic] = 0
    time.sleep(2.0)
    # one timer per topic on a dedicated executor
    for topic, (pub, msg, hz) in pubs.items():
        def cb(pub=pub, msg=msg, topic=topic):
            pub.publish(msg)
            counts[topic] += 1
        node.create_timer(1.0 / hz, cb)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    samples = []
    rp.cpu_percent(None)
    start = time.monotonic()
    marked = False
    next_sample = start + 1.0
    while time.monotonic() - start < duration:
        ex.spin_once(timeout_sec=0.001)
        now = time.monotonic()
        if now >= next_sample:
            samples.append({"t": round(now - start, 1), "cpu": rp.cpu_percent(None),
                            "rss_mb": round(rp.memory_info().rss / 2**20, 1)})
            next_sample += 1.0
        if not marked and now - start > duration / 2:
            subprocess.run([sys.executable, "-m", "blackboxrs", "flight", "mark",
                            "benchmark marker under load", "--evidence-dir", str(out)],
                           check=True, capture_output=True)
            marked = True
    pub_elapsed = time.monotonic() - start
    node.destroy_node()
    rclpy.shutdown()
    t_stop = time.monotonic()
    rec.send_signal(2)
    output, _ = rec.communicate(timeout=120)
    stop_s = time.monotonic() - t_stop
    sess = json.loads((out / session / "session.json").read_text())
    ts = sess["topic_status"]
    per_topic = {}
    for topic, n in counts.items():
        got = ts[topic].get("received", 0)
        per_topic[topic] = {"published": n, "received": got,
                            "missing": n - got, "message_lost_event": ts[topic].get("message_lost"),
                            "published_hz": round(n / pub_elapsed, 1)}
    bundles = [line.strip() for line in output.splitlines() if "/inc_" in line]
    bundle = {}
    if bundles:
        rep = json.loads((Path(bundles[0]) / "report.json").read_text())
        man = json.loads((Path(bundles[0]) / "manifest.json").read_text())
        bundle = {"path": bundles[0], "status": rep["status"],
                  "records": rep["window"]["records"],
                  "seconds_before_trigger": rep["window"]["seconds_before_trigger"],
                  "seconds_after_trigger": rep["window"]["seconds_after_trigger"],
                  "ring_evicted_in_window": rep["data_quality"]["recorder_ring_evicted_in_window"],
                  "writer_errors": man["writer"]["write_errors"],
                  "recorder_receipt_delay": rep["recorder_receipt_delay"],
                  "bundle_mb": round(sum(p.stat().st_size for p in Path(bundles[0]).iterdir())
                                     / 2**20, 2)}
    cpu = [s["cpu"] for s in samples[2:]]
    rss = [s["rss_mb"] for s in samples]
    total_pub = sum(v["published"] for v in per_topic.values())
    total_rx = sum(v["received"] for v in per_topic.values())
    return {"scale": scale, "duration_s": round(pub_elapsed, 1),
            "offered_msgs_per_s": round(total_pub / pub_elapsed, 1),
            "published": total_pub, "received": total_rx,
            "missing_total": total_pub - total_rx,
            "missing_fraction": round((total_pub - total_rx) / total_pub, 6) if total_pub else None,
            "recorder_cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
            "recorder_cpu_percent_max": max(cpu) if cpu else None,
            "recorder_rss_mb_start": rss[0] if rss else None,
            "recorder_rss_mb_max": max(rss) if rss else None,
            "recorder_stop_and_finalize_s": round(stop_s, 2),
            "per_topic": per_topic, "bundle": bundle, "samples": samples}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--part", choices=["core", "live", "both"], default="both")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--scale", type=float, action="append",
                    help="workload multiplier on GO2 rates (repeatable; default 1)")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    res = {"host": {"platform": platform.platform(), "cpu_count": psutil.cpu_count(),
                    "python": platform.python_version(),
                    "rmw": os.environ.get("RMW_IMPLEMENTATION")},
           "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.part in ("core", "both"):
        res["core"] = bench_core(60.0, 1200.0, out)
        print(json.dumps(res["core"], indent=1))
    if args.part in ("live", "both"):
        res["live"] = []
        for sc in args.scale or [1.0]:
            r = bench_live(args.duration, sc, out)
            res["live"].append(r)
            print(json.dumps({k: v for k, v in r.items() if k not in ("samples", "per_topic")},
                             indent=1))
            print(json.dumps(r["per_topic"], indent=1))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, indent=2) + "\n")


if __name__ == "__main__":
    main()
