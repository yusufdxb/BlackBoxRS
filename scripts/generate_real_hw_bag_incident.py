"""Generate the real GO2 hardware-bag incident bundle under examples/incidents/.

Unlike ``generate_real_bag_incident.py`` (which replays a *simulated*
odom/imu/cmd_vel bag), this script replays a rosbag2 recording captured on a
physical Unitree GO2 during a real hardware evaluation session. The source
recording (``extended_5min``, ~330 s, ~94k messages) is not checked into this
repository because of its size (over 600 MB); it lives outside the repo and
is passed in with ``--bag``. Its real topics are ``/utlidar/robot_pose``
(PoseStamped), ``/utlidar/imu`` (Imu), ``/utlidar/cloud`` (PointCloud2),
plus low-rate ``/gnss`` and ``/multiplestate`` string topics. There is no
``/odom`` topic in this recording, so the pose-equivalent stream for a
dead-topic demonstration is ``/utlidar/robot_pose``.

The recording itself is healthy end to end (verified: replaying the full,
untouched bag through the real DeadTopicDetector raises zero anomalies).
To exercise the detector honestly, this script injects a silence on
``/utlidar/robot_pose`` after a fixed cutoff, exactly as
``generate_real_bag_incident.py`` does for the simulated bag: every other
topic, and every message before the cutoff, is genuine recorded GO2
telemetry: only the injected silence is synthetic, and the anomaly it
produces comes from the real detector's own timing logic, not a
hand-written event.

The full recording is trimmed to the first ``MAX_DURATION_SEC`` seconds
(``--max-duration``) purely to keep the committed bundle small; this does
not change the content or timing of the messages that remain. Reproduce
the untrimmed, full-bag replay directly with:

    robot-blackbox replay-bag <path-to-extended_5min-bag-dir> \\
        --drop-topic /utlidar/robot_pose --drop-after 60 --timeout 3.0

Usage::

    python scripts/generate_real_hw_bag_incident.py --bag <path-to-bag-dir>
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident
from blackboxrs.recording.bag_replay import replay_bag

REPO = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO / "examples" / "incidents"
TARGET = EXAMPLES_DIR / "inc_real_hw_bag_pose_dropout"

DROP_TOPIC = "/utlidar/robot_pose"
DROP_AFTER_SEC = 5.0
TIMEOUT_SEC = 3.0
MAX_DURATION_SEC = 20.0


def main() -> Path:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", type=Path, required=True,
                        help="Path to the real GO2 rosbag2 recording "
                             "directory (e.g. .../extended_5min/).")
    parser.add_argument("--max-duration", type=float, default=MAX_DURATION_SEC,
                        help="Trim the replay to this many seconds from "
                             "the bag's first message (bundle-size cut "
                             "only; default: %(default)s s).")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="bbrs_realhwbag_"))
    log_dir = work / "logs"
    log_file = log_dir / "blackboxrs_realhwbag.jsonl"

    result = replay_bag(
        args.bag,
        log_file,
        session_id="real_hw_bag_pose_dropout",
        dead_topic_timeout_sec=TIMEOUT_SEC,
        drop_topic=DROP_TOPIC,
        drop_after_sec=DROP_AFTER_SEC,
        max_duration_sec=args.max_duration,
    )

    if not result.anomalies:
        raise SystemExit("Replay produced no anomaly; check bag/params.")

    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)

    bundle = build_incident(
        window_start=result.window_start,
        window_end=result.window_end,
        config=cfg,
        incidents_dir=work / "incidents",
        title="Robot-pose dropout on a real GO2 hardware bag",
        notes=(
            "Real-hardware replay. Source data is a genuine rosbag2 "
            f"recording from a physical Unitree GO2 ({Path(args.bag).name}, "
            "full recording ~330s / ~94k messages across "
            "/utlidar/robot_pose, /utlidar/imu, /utlidar/cloud, /gnss, "
            "/multiplestate; the full recording is not included in this "
            "repo due to size). This bundle replays the first "
            f"{args.max_duration:.0f}s of that recording (a size cut on "
            "the committed artifact, not a content change). The recording "
            "carries no /odom topic; /utlidar/robot_pose is the "
            "pose-equivalent liveness signal used here. A dropout of "
            f"{DROP_TOPIC} was INJECTED at {result.drop_time.isoformat()} "
            "(silenced for the remainder of the window) into otherwise-"
            "real data; verified separately that the untouched, full "
            "330s recording produces zero anomalies on its own. The "
            "dead-topic anomaly below was produced by the real "
            "DeadTopicDetector from bag timing, not hand-seeded. This "
            "validates PASSIVE forensic replay only: no control loop or "
            "closed-loop robot behavior was exercised. Reproduce with: "
            "python scripts/generate_real_hw_bag_incident.py --bag <path>"
        ),
        tags=["real-hw-bag", "go2", "pose-dropout", "dead-topic"],
    )

    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(bundle, TARGET)
    print(f"Real-hw-bag bundle: {TARGET}")
    print(f"  events={result.event_count} topics={sorted(result.topics)}")
    for a in result.anomalies:
        print(f"  anomaly: {a.data.get('topic')} silent "
              f"{a.data.get('value'):.1f}s @ {a.timestamp.isoformat()}")
    return TARGET


if __name__ == "__main__":
    main()
