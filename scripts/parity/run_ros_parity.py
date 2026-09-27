#!/usr/bin/env python3
"""Run the frozen parity scenarios against the REAL arbiters under ROS 2.

Targets (each started as its own process, one fresh process per scenario):

  helix      HELIX ``helix_arbiter`` node, from a HELIX workspace install,
             with HELIX's own ``config/arbiter.yaml`` (autostart).
  twist_mux  the installed ``twist_mux`` binary with HELIX's
             ``config/twist_mux.yaml`` (legacy path), output remapped to
             /cmd_vel as helix_closedloop.launch.py does.
  twist_mux_tie   twist_mux with two equal-priority inputs, to measure its
             tie-break rule (not a HELIX configuration).

A scripted rclpy publisher sends each scenario's messages at their script
times; a subscriber records every output with its receipt time. The raw
record is written as JSON under docs/parity/ and is what the CI differential
test compares Replay Lab against. Nothing here judges parity; that is
blackboxrs.lab.parity.compare_* in tests/unit/lab/test_arbiter_parity.py.

Needs a sourced ROS 2 Humble (and, for helix, a sourced HELIX install):

    source /opt/ros/humble/setup.bash
    source ~/workspace/helix/install/setup.bash
    python3 scripts/parity/run_ros_parity.py --target helix \\
        --helix-src ~/workspace/helix --out docs/parity/helix_node.json

Runs on an isolated DDS domain with ROS_LOCALHOST_ONLY=1.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import rclpy  # noqa: E402
from geometry_msgs.msg import Twist  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.qos import QoSProfile, ReliabilityPolicy  # noqa: E402

from blackboxrs.lab.parity import HELIX_CMD, HOLD, NAV, SCENARIOS, TELEOP, Script, twist  # noqa: E402,E501

DOMAIN = "77"
TIE_YAML = """twist_mux:
  ros__parameters:
    topics:
      alpha:
        topic: /tie/a
        timeout: 0.5
        priority: 10
      bravo:
        topic: /tie/b
        timeout: 0.5
        priority: 10
    locks:
      unused:
        topic: /tie/_lock
        timeout: 0.0
        priority: 0
"""


# Same as TIE_YAML but the first-listed input sorts LAST by name, to tell file
# order from name order.
TIE_REVERSED_YAML = TIE_YAML.replace("alpha:", "zulu:").replace("bravo:", "alpha:")


def sink_scenarios() -> dict:
    from blackboxrs.lab.parity import Scenario
    s = Script().stream(NAV, 0.1, 1.0, twist(0.2), offset=0.007)   # within sink limit 0.25
    return {"input_stops": Scenario("input_stops", "nav stops; twist_mux goes silent; what "
                                    "does the HELIX sport sink do", s, 3.0)}


def tie_scenarios() -> dict:
    from blackboxrs.lab.parity import Scenario
    s = Script()
    s.stream("/tie/a", 0.1, 2.0, twist(0.1), offset=0.003)
    s.stream("/tie/b", 0.6, 2.0, twist(0.2), offset=0.028)
    return {"equal_priority_tie": Scenario("equal_priority_tie",
                                           "two live inputs with the same priority", s, 2.5)}


def git_sha(path: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--",
                                "src/helix_arbiter", "src/helix_msgs", "src/helix_bringup"],
                               capture_output=True, text=True).stdout.strip()
        return out + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _compact(data: dict) -> list:
    """A hold as [hold, epoch, seq, fault_id]; a twist as [lx, ly, lz, ax, ay, az]."""
    if "hold" in data:
        return [data["hold"], data["epoch"], data["seq"], data["fault_id"]]
    v = [data["linear"][a] for a in "xyz"] + [data["angular"][a] for a in "xyz"]
    return ["NaN" if isinstance(x, float) and math.isnan(x) else x for x in v]


def to_msg(data: dict) -> Twist:
    m = Twist()
    m.linear.x, m.linear.y, m.linear.z = (float(data["linear"][a]) for a in "xyz")
    m.angular.x, m.angular.y, m.angular.z = (float(data["angular"][a]) for a in "xyz")
    return m


class Probe:
    def __init__(self, target: str, topics: set[str]) -> None:
        self.node = rclpy.create_node(f"parity_probe_{os.getpid()}")
        self.target = target
        rel = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE)
        self.pubs = {}
        self.hold_type = None
        for t in sorted(topics):
            if t == HOLD:
                from helix_msgs.msg import HelixHold
                self.hold_type = HelixHold
                self.pubs[t] = self.node.create_publisher(HelixHold, t, rel)
            else:
                self.pubs[t] = self.node.create_publisher(Twist, t, rel)
        self.out: list[dict] = []
        self.t0: float | None = None
        self.lock = threading.Lock()
        if target == "helix":
            from helix_msgs.msg import ArbiterStatus
            self.node.create_subscription(ArbiterStatus, "/helix/arbiter/status",
                                          self._on_status, rel)
        else:
            self.node.create_subscription(Twist, "/cmd_vel", self._on_twist, rel)
        if target == "twist_mux_then_sink":
            from std_msgs.msg import String
            self.node.create_subscription(String, "/helix/sink/trace", self._on_trace, rel)
        self.ex = SingleThreadedExecutor()
        self.ex.add_node(self.node)
        self.stop = False
        self.th = threading.Thread(target=self._spin, daemon=True)
        self.th.start()

    def _spin(self) -> None:
        while not self.stop:
            self.ex.spin_once(timeout_sec=0.01)

    def _t(self) -> float | None:
        return None if self.t0 is None else time.monotonic() - self.t0

    def _on_status(self, m) -> None:
        with self.lock:
            # [t, reason, source, vx, vy, wz, rejected_total]
            self.out.append([self._t(), m.reason, m.selected_source, m.out_linear_x,
                             m.out_linear_y, m.out_angular_z, int(m.rejected_total)])

    def _on_trace(self, m) -> None:
        with self.lock:
            # [t, "sink", api_id, reason] for the sink trace
            try:
                d = json.loads(m.data)
            except ValueError:
                d = {}
            self.out.append([self._t(), "sink", d.get("api_id"), d.get("reason")])

    def _on_twist(self, m) -> None:
        v = [m.linear.x, m.linear.y, m.angular.z]
        with self.lock:
            # [t, vx, vy, wz]
            self.out.append([self._t()] + ["NaN" if math.isnan(x) else x for x in v])

    def send(self, topic: str, data: dict) -> None:
        if topic == HOLD:
            m = self.hold_type()
            m.hold, m.fault_id = bool(data["hold"]), str(data["fault_id"])
            m.epoch, m.seq = int(data["epoch"]), int(data["seq"])
            m.reason = "parity"
            self.pubs[topic].publish(m)
        else:
            self.pubs[topic].publish(to_msg(data))

    def close(self) -> None:
        self.stop = True
        self.th.join(timeout=2)
        self.node.destroy_node()


def start_target(target: str, helix_src: Path | None, tmp: Path) -> subprocess.Popen:
    if target == "twist_mux_then_sink":
        cfg = helix_src / "src/helix_bringup/config/twist_mux.yaml"
        # the sink in its default dry_run mode: it decides and traces, sends nothing
        cmd = ["bash", "-c",
               f"ros2 run twist_mux twist_mux --ros-args --params-file {cfg} "
               "-r cmd_vel_out:=/cmd_vel & "
               "ros2 run helix_arbiter helix_go2_sport_sink --ros-args -p mode:=dry_run & wait"]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    if target == "helix":
        cfg = helix_src / "src/helix_arbiter/config/arbiter.yaml"
        cmd = ["ros2", "run", "helix_arbiter", "helix_arbiter", "--ros-args",
               "--params-file", str(cfg), "-p", "autostart:=true"]
    elif target == "twist_mux":
        cfg = helix_src / "src/helix_bringup/config/twist_mux.yaml"
        cmd = ["ros2", "run", "twist_mux", "twist_mux", "--ros-args", "--params-file", str(cfg),
               "-r", "cmd_vel_out:=/cmd_vel"]
    else:
        cfg = tmp / "tie.yaml"
        cfg.write_text(TIE_REVERSED_YAML if target == "twist_mux_tie_reversed" else TIE_YAML)
        cmd = ["ros2", "run", "twist_mux", "twist_mux", "--ros-args", "--params-file", str(cfg),
               "-r", "cmd_vel_out:=/cmd_vel"]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def wait_ready(probe: Probe, target: str, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    watch = "/tie/a" if target.startswith("twist_mux_tie") else NAV
    while time.monotonic() < end:
        if target == "helix":
            if probe.out:
                return
        elif probe.pubs[watch].get_subscription_count() > 0 and (
                target != "twist_mux_then_sink" or probe.out):
            time.sleep(1.0)      # let discovery settle both ways
            return
        time.sleep(0.05)
    raise RuntimeError(f"{target} did not come up")


def run_one(target: str, sc, helix_src, tmp, legacy: bool) -> dict:
    steps = list(sc.script.steps)
    if legacy:
        steps += [(t, HELIX_CMD, twist(0.0)) for t, topic, d in sc.script.steps
                  if topic == HOLD and d["hold"]]
    steps.sort(key=lambda s: (s[0], {HOLD: 0, HELIX_CMD: 1}.get(s[1], 2), s[1]))
    topics = {topic for _, topic, _ in steps} | (
        set() if target.startswith("twist_mux_tie") else {NAV, TELEOP})
    if target == "helix":
        topics |= {HOLD}
    if target != "helix":
        topics.discard(HOLD)
        steps = [s for s in steps if s[1] != HOLD]
    proc = start_target(target, helix_src, tmp)
    probe = Probe(target, topics)
    try:
        wait_ready(probe, target)
        with probe.lock:
            probe.out.clear()
        probe.t0 = time.monotonic() + 0.3
        sent = []
        for t, topic, data in steps:
            while time.monotonic() < probe.t0 + t:
                time.sleep(max(0.0, min(0.0005, probe.t0 + t - time.monotonic())))
            probe.send(topic, data)
            sent.append([t, round(time.monotonic() - probe.t0, 6), topic, _compact(data)])
        while time.monotonic() < probe.t0 + sc.duration_s:
            time.sleep(0.005)
        with probe.lock:
            out = [[round(o[0], 6)] + o[1:] for o in probe.out
                   if o[0] is not None and o[0] >= 0.0]
        late = max((abs(s[1] - s[0]) for s in sent), default=0.0)
        return {"scenario": sc.name, "covers": sc.covers, "duration_s": sc.duration_s,
                "sent": sent, "outputs": out, "max_send_lateness_s": round(late, 6)}
    finally:
        probe.close()
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5)
        try:   # children of a wrapper shell outlive it; take down the whole group
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=["helix", "twist_mux", "twist_mux_tie",
                                                  "twist_mux_tie_reversed",
                                                  "twist_mux_then_sink"], required=True)
    ap.add_argument("--helix-src", type=Path, required=True)
    ap.add_argument("--scenario", action="append", default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    os.environ["ROS_DOMAIN_ID"] = DOMAIN
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    rclpy.init()
    scen = (tie_scenarios() if args.target.startswith("twist_mux_tie") else
            sink_scenarios() if args.target == "twist_mux_then_sink" else SCENARIOS)
    names = args.scenario or list(scen)
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in names:
            r = run_one(args.target, scen[n], args.helix_src.expanduser(), Path(tmp),
                        legacy=args.target == "twist_mux")
            r["config"] = ({"twist_mux_tie": TIE_YAML,
                            "twist_mux_tie_reversed": TIE_REVERSED_YAML}.get(args.target))
            print(f"{args.target:14s} {n:28s} sent={len(r['sent'])} out={len(r['outputs'])} "
                  f"send_lateness_max={r['max_send_lateness_s'] * 1000:.2f}ms", flush=True)
            results.append(r)
    rclpy.shutdown()
    twist_mux_ver = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "ros-humble-twist-mux"],
                                   capture_output=True, text=True).stdout.strip()
    doc = {
        "schema": "blackboxrs.lab.ros_parity.v1",
        "target": args.target,
        "recorded": "real process under ROS 2; times are the probe's monotonic clock, s "
                    "from scenario start",
        "format": {"sent": "[t_script, t_sent, topic, payload]",
                   "outputs": ("[t, reason, source, vx, vy, wz, rejected_total]"
                               if args.target == "helix" else "[t, vx, vy, wz]")},
        "environment": {"ros_distro": os.environ.get("ROS_DISTRO"),
                        "rmw": os.environ.get("RMW_IMPLEMENTATION") or "default",
                        "python": platform.python_version(),
                        "helix_commit": git_sha(args.helix_src.expanduser()),
                        "twist_mux_package": twist_mux_ver if "twist" in args.target else None},
        "scenarios": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
