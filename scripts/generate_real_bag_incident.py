"""Generate the real-bag incident bundle committed under examples/incidents/.

Unlike ``generate_sample_incident.py`` (which hand-writes a synthetic event
stream), this script replays a *real recorded* rosbag2 through the real
:class:`DeadTopicDetector`.  The bag (``examples/bags/go2_sim_odom_imu.mcap``)
is genuine GO2 odom/imu/cmd_vel data; the only synthetic element is an
injected dropout of ``/source/utlidar/robot_odom`` partway through, which the
detector then discovers on its own from bag timing.  The emitted anomaly is
produced by the detector, not seeded by hand.

Usage::

    python scripts/generate_real_bag_incident.py [--bag PATH]
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
DEFAULT_BAG = REPO / "examples" / "bags" / "go2_sim_odom_imu.mcap"
EXAMPLES_DIR = REPO / "examples" / "incidents"
TARGET = EXAMPLES_DIR / "inc_real_bag_odom_dropout"

DROP_TOPIC = "/source/utlidar/robot_odom"
DROP_AFTER_SEC = 5.0
TIMEOUT_SEC = 3.0


def main() -> Path:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", type=Path, default=DEFAULT_BAG,
                        help="Path to the recorded .mcap bag.")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="bbrs_realbag_"))
    log_dir = work / "logs"
    log_file = log_dir / "blackboxrs_realbag.jsonl"

    result = replay_bag(
        args.bag,
        log_file,
        session_id="real_bag_odom_dropout",
        dead_topic_timeout_sec=TIMEOUT_SEC,
        drop_topic=DROP_TOPIC,
        drop_after_sec=DROP_AFTER_SEC,
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
        title="Odometry dropout on /source/utlidar/robot_odom",
        notes=(
            "Real-bag replay. Source data is a genuine GO2 recording "
            f"({Path(args.bag).name}: odom, imu, cmd_vel over ~12 s). "
            f"A dropout of {DROP_TOPIC} was INJECTED at "
            f"{result.drop_time.isoformat()} (silenced for the remainder of "
            "the bag) into otherwise-real data. The dead-topic anomaly below "
            "was produced by the real DeadTopicDetector from bag timing, not "
            "hand-seeded. Reproduce with: "
            "python scripts/generate_real_bag_incident.py"
        ),
        tags=["real-bag", "odom-dropout", "dead-topic"],
    )

    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(bundle, TARGET)
    print(f"Real-bag bundle: {TARGET}")
    print(f"  events={result.event_count} topics={sorted(result.topics)}")
    for a in result.anomalies:
        print(f"  anomaly: {a.data.get('topic')} silent "
              f"{a.data.get('value'):.1f}s @ {a.timestamp.isoformat()}")
    return TARGET


if __name__ == "__main__":
    main()
