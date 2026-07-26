# Runtime Telemetry-Health Demo

This is a 60 to 120 second terminal demonstration of one technical contrast:
structural topic presence does not guarantee semantic telemetry liveness.

The public demo uses a generated provenance fixture and a controlled
`PoseStamped` publisher. It does not replay genuine robot data. A small curated
summary describes the separate genuine GO2 validation without distributing the
681,996,932-byte source bag.

## One-command reproduction

Run from a BlackBoxRS source checkout on Linux with ROS 2 Humble and
`geometry_msgs` available:

```bash
source /opt/ros/humble/setup.bash
export ROS_LOCALHOST_ONLY=1
OUT="$(mktemp -d /tmp/blackboxrs-telemetry-demo.XXXXXX)"
python3 examples/demo_runtime_telemetry_health.py \
  --out "$OUT" --domain-start 180 | tee "$OUT/terminal.txt"
jq '.checks' "$OUT/demo_summary.json"
```

Use an unused pair of domain IDs if 180 and 181 are already active. The output
directory must be new or empty.

Expected bounded outcome:

```text
presence_check_passed: True
status: blocked
reason: stale
dependent_started: True
publisher_alive_after_guard: True
dependent_pid_survives: False
status: passed
PASS: nearby_healthy_passed
```

Exact PIDs, fingerprints, timing, and measured rate vary by run. The invariant
fields are asserted by `tests/integration/test_public_telemetry_health_demo.py`.

## 90-second recording script

| Time | Terminal action | Caption or narration |
|---|---|---|
| 0-8 s | Show this title and the genuine summary path. | “Structural topic presence does not guarantee telemetry liveness.” |
| 8-20 s | Start the command. Pause on incident ID, event reference, evidence fingerprint, rule fingerprint, topic, type, QoS, and thresholds. | “The public run builds a deterministic fixture through the production adoption path. It is not the genuine GO2 bag.” |
| 20-42 s | Let the 18.75 Hz stream qualify and the harmless dependent start. | “The topic qualifies over the two-second rate window. The dependent now runs under supervision.” |
| 42-58 s | Pause after the publisher becomes silent. Show that the process remains alive and `topic_present` passes. | “The ROS graph still has a publisher, but useful telemetry has stopped.” |
| 58-72 s | Show `status=blocked`, `reason=stale`, detection latency, dependent exit, and no surviving PID. | “The hardened guard detects arrival staleness and enforces the supported process boundary.” |
| 72-82 s | Show the nearby 18.75 Hz result. | “A selected nearby healthy stream completes one full second of post-launch supervision.” |
| 82-90 s | Show the linked attack results and limitations. | “Remapping, context, provenance, and process escape were tested separately. This remains aggregate-topic liveness, not payload semantics or field safety.” |

The dependent is only a Python marker writer followed by sleep. The demo does
not launch robot software or send control commands.

## Deterministic capture

Record a terminal directly when a capture tool is available:

```bash
script -q -c \
  "python3 examples/demo_runtime_telemetry_health.py --out '$OUT/run' --domain-start 180" \
  "$OUT/typescript"
```

For video, use a fixed-width terminal at 1280 by 720, 18 to 22 point monospace
text, and no desktop notifications. Do not cut between the presence result and
the hardened result. That contrast is the evidence.

## Static fallback

If video capture is unavailable, capture three terminal frames from one run:

1. Provenance: stage 2 showing incident, event, evidence, rule fingerprint, and
   selected thresholds.
2. Failure contrast: `topic_presence_comparison` beside
   `publisher_present_silence`.
3. Nearby valid case and boundaries: `nearby_healthy`, `.checks`, and the final
   aggregate-topic limitation.

These commands isolate the stable fields for frames 2 and 3:

```bash
jq '{
  presence: .topic_presence_comparison,
  hardened: (.publisher_present_silence | {
    status, reason, resolved_topic, dependent_started,
    dependent_exit_code, detection_latency_sec,
    publisher_alive_after_guard, dependent_pid_survives
  })
}' "$OUT/run/demo_summary.json"

jq '{
  nearby_healthy: (.nearby_healthy | {
    status, observed_rate_hz, dependent_started,
    dependent_supervision_sec, dependent_pid_survives
  }),
  checks
}' "$OUT/run/demo_summary.json"
```

Before publishing a transcript or screenshot, remove the temporary directory
path and any machine-specific shell prompt. Keep the generated-fixture label
visible.

## Genuine-data level

The separate physical-GO2 session contains 94,325 messages over 329.601840622
seconds. The `/utlidar/robot_pose` topic contributes 6,177
`geometry_msgs/msg/PoseStamped` records, with mean rate 18.746780976988813 Hz
and maximum healthy gap 0.070847572 seconds. Its combined identity SHA-256 is
`f6c15669dd5a1630578d4ab7931b24e93d22251b5bd08ba5b8cded1709e350c5`.

The bag is not committed because it is about 682 MB and comes from a private
hardware evaluation session. The curated hashes and statistics are in
`examples/telemetry_health/genuine_go2_evidence_summary.json`. Another user can
substitute a rosbag2 directory containing a fully qualified `PoseStamped` topic:

```bash
python3 scripts/characterize_go2_pose_telemetry.py \
  /path/to/your/bag \
  --topic /your/pose/topic \
  --graph-context your_reviewed_context \
  --output /new/external/output/healthy_telemetry_evidence.json
```

The resulting thresholds describe that session only. Adoption must use a
finalized incident for the same topic, and the trusted fingerprint must be
pinned independently of the runtime rule being checked.
