#!/usr/bin/env bash
# Off-robot rehearsal: the C++ recorder and monitor beside HELIX's own
# A-F rehearsal (real HELIX stack, stage runner and sink; helix_fake_go2 in
# place of the robot). NOT hardware evidence: HELIX labels every stage
# REHEARSAL, and nothing here can reach a robot.
#
#   HELIX_SRC=~/workspace/helix scripts/cpp/helix_rehearsal.sh OUT_DIR
#
# Safety: loopback-only DDS (ROS_LOCALHOST_ONLY=1) on a private domain, and
# the script refuses to start if any GO2 topic is visible.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HELIX=${HELIX_SRC:?set HELIX_SRC to a built HELIX checkout}
UNITREE_WS=${UNITREE_WS:-$HOME/workspace/unitree_ros2/cyclonedds_ws}
OUT=${1:?output directory}
mkdir -p "$OUT/logs"
OUT=$(cd "$OUT" && pwd)
export ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-86}
set +u
source /opt/ros/humble/setup.bash
source "$UNITREE_WS/install/setup.bash"
source "$HELIX/install/setup.bash"
source "$ROOT/install/setup.bash"
set -u

ros2 daemon stop >/dev/null 2>&1
if timeout 5 ros2 topic list --no-daemon 2>/dev/null | grep -qE '^/(lowstate|sportmodestate|utlidar/)'; then
  echo "a GO2 topic is visible on domain $ROS_DOMAIN_ID: refusing to run" >&2
  exit 3
fi

sed -e "s#^profile: .*#profile: $ROOT/blackboxrs/flight/profiles/go2_helix.yaml#" \
    -e "s#  evidence_dir: .*#  evidence_dir: $OUT/evidence#" \
    -e "s#  findings_file: .*#  findings_file: $OUT/online_findings.jsonl#" \
    -e "s#  expect_rmw: .*#  expect_rmw: null#" \
    "$ROOT/configs/go2_hardware_stage_e.yaml" > "$OUT/runtime.yaml"

ros2 run blackboxrs_ros recorder --config "$OUT/runtime.yaml" > "$OUT/logs/recorder.log" 2>&1 &
REC=$!
ros2 run blackboxrs_ros monitor --config "$OUT/runtime.yaml" > "$OUT/logs/monitor.log" 2>&1 &
MON=$!
sleep 3

bash "$HELIX/scripts/hw_rehearsal.sh" "$OUT/helix_session" > "$OUT/logs/helix_rehearsal.log" 2>&1
HELIX_RC=$?
echo "HELIX rehearsal exit $HELIX_RC"
sleep 16   # the post-trigger window (15 s) of the last incident
kill -INT "$MON" "$REC"
wait "$REC"; echo "recorder exit $?"
wait "$MON"; echo "monitor exit $?"
grep -E "REHEARSAL COMPLETE|did not PASS|PASS|FAIL" "$OUT/logs/helix_rehearsal.log" | tail -12
