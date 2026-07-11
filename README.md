# BlackBoxRS

**Incident intelligence and prevention for ROS 2 robots.**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![ROS 2 Humble (verified)](https://img.shields.io/badge/ROS%202-Humble%20(verified)-brightgreen)
![License MIT](https://img.shields.io/badge/license-MIT-green)

## Works on real GO2 telemetry

The offline bag-replay path has been run against a genuine rosbag2
recording captured on a physical Unitree GO2 (not simulation): topics
`/utlidar/robot_pose`, `/utlidar/imu`, `/utlidar/cloud`, `/gnss`, and
`/multiplestate`, ~94k messages over a 330-second session. The untouched
recording replays clean end to end (zero anomalies); to exercise the
detector honestly, a `/utlidar/robot_pose` dropout is injected into an
otherwise-real window of that same recording, and the real
`DeadTopicDetector` (not a stand-in) finds it from bag timing alone,
with no hand-written anomaly. This demonstrates passive, offline
forensic replay against real hardware data. It is **not** a claim that
BlackBoxRS has run in a closed control loop on the robot, or that it has
been validated live on hardware; the loop it closes here is
record-then-replay, off to the side of the robot's own control stack.

Reproduce against your own recording (the source bag is ~680&nbsp;MB and
is not checked into this repo):

```
robot-blackbox replay-bag <path-to-your-rosbag2-dir> \
  --drop-topic /utlidar/robot_pose --drop-after 60 --timeout 3.0
```

Real output from the committed example
(`scripts/generate_real_hw_bag_incident.py`, trimmed to a 20-second
window of the same recording so the checked-in artifact stays small):

```
$ python scripts/generate_real_hw_bag_incident.py --bag /path/to/extended_5min
Dead topic detected: /utlidar/robot_pose silent for 3.0s
Real-hw-bag bundle: examples/incidents/inc_real_hw_bag_pose_dropout
  events=5439 topics=['/gnss', '/multiplestate', '/utlidar/cloud', '/utlidar/imu', '/utlidar/robot_pose']
  anomaly: /utlidar/robot_pose silent 3.0s @ 2026-04-06T18:13:48.490684+00:00
```

The incident report BlackBoxRS generated from that run:

![Real GO2 hardware-bag incident report](docs/assets/real_go2_bag_incident_report.png)

Full bundle: `examples/incidents/inc_real_hw_bag_pose_dropout/`. The
sim-bag counterpart lives at
`examples/incidents/inc_real_bag_odom_dropout/`; both are replayed
through the same code path documented below.

---

> Status: **v0.4.1**. Incident-bundle pipeline, report generator,
> fingerprinting, prevention runner, and observer-mode (off-board
> capture) all work. All seven anomaly detectors are live, with
> hysteresis (`min_consecutive_samples`) to suppress single-sample
> noise. All seven `PreflightCheck` kinds (`topic_present`,
> `qos_match`, `node_running`, `env_var`, `param_value`,
> `resource_threshold`, `custom_python`) have real implementations;
> unknown kinds fail loudly at rule-load. Detector FPR/TPR is
> measured on a synthetic stream and published in
> `docs/DETECTOR_CHARACTERISTICS.md`. Observer-mode is exercised
> end-to-end against real DDS in the Docker Humble CI job.

When a ROS 2 robot fails, BlackBoxRS produces a reproducible incident
bundle: timeline, evidence, config and version signatures, a likely-cause
narrative grounded in the evidence, and a recommended preflight rule the
next launch can run to keep the same failure from happening again.

The bundle is the artifact. Postmortems collapse from an afternoon to a
paragraph.

---

## Where to run it

BlackBoxRS does not need to live on the robot.

| Mode | Where it runs | When to use |
|---|---|---|
| **Onboard** (default) | Same host as the ROS 2 node graph (Jetson, NUC, on-robot workstation). | You have shell access to the robot and want host metrics (CPU, memory, per-process CPU/RSS) in the bundle. |
| **Observer** | Any workstation that can `ros2 topic list` against the robot over DDS. No SSH, no per-robot install. | You're debugging a robot from a laptop, the robot's compute is locked down, or a whole team needs to capture bundles without each user setting up a daemon on the robot. |

Observer mode is a single config flag:

```yaml
# ~/.blackboxrs/config.yaml
runtime:
  role: observer
  observed_host: go2-edu-01     # free-form label, ends up in every bundle
```

DDS-bound detectors that are live today (frequency, dead topic, QoS
mismatch) keep running because they observe the robot's published
graph. Host-bound thresholds (CPU / memory on the local machine)
describe the observer, not the robot, so the system-monitor pipeline
that feeds them is auto-disabled. Every incident bundle records both
`observer_host` and `observed_host` so reports name the two sides
explicitly.

All seven anomaly detectors are live: `threshold`, `frequency`,
`dead_topic`, `qos_mismatch` (DDS-bound, work in both onboard and
observer mode), plus `tf_topology`, `clock_skew`, and
`process_signals`. `process_signals` auto-disables in observer
mode because it reads the local process table; `tf_topology` and
`clock_skew` stay active because they consume DDS-visible events
or local NTP state.

See `docs/QUICKSTART_REMOTE.md` for a 5-minute walkthrough from
`pip install` to the first remote-captured bundle.

---

## What problem this solves

Field robotics teams running ROS 2 lose hours per week to "why isn't
this running like yesterday?" Logs are not an answer; they are raw
material. Today, when a robot fails on a field test:

1. Engineer SSHs in, `ros2 topic list`, scrolls journalctl, greps. 20 to
   90 minutes per incident.
2. They paste log fragments into Slack. Three other engineers compare
   notes from memory.
3. The "fix" is a one-line config change with no record of *why*.
4. The same failure recurs on a different robot two weeks later.

After BlackBoxRS:

```
robot-blackbox start                   # already running on the robot
# robot fails
robot-blackbox incident build --since 5m
# → ~/.blackboxrs/incidents/inc_2026-05-07T14-22-00_a3f2/
#   ├── report.md
#   ├── incident.json
#   ├── timeline.json
#   ├── fingerprint.json
#   ├── signatures/{config.json, versions.json}
#   └── evidence/{events.jsonl, triggers.json, snapshots.json}
```

Engineer reads `report.md`, the likely cause is named with confidence
and the supporting evidence is hyperlinked into the bundle. They convert
it to a `PreventionRule` with one command. The next launch runs
`robot-blackbox preflight` and the rule fires before the failure.

---

## The loop

```mermaid
graph LR
    O[observe] --> E[explain]
    E --> R[replay]
    R --> P[prevent]

    O --> Od["daemon captures events,<br/>anomalies, host + GPU"]
    E --> Ed["incident builder produces<br/>bundle with timeline + fingerprint"]
    R --> Rd["bundle is portable;<br/>another engineer<br/>re-renders the report"]
    P --> Pd["preflight rule blocks<br/>next launch when the<br/>precursor reappears"]
```

`observe` was the v0.3 wedge. `explain → replay → prevent` is what makes
this a product.

---

## Sample incident

`examples/incidents/inc_demo_tf_break/` is a synthetic but realistic
TF-break incident bundle, committed to the repo. The top of its
`report.md`:

```
# Incident `inc_2026-05-07T14-22-00_04ca9c43`

- **Severity**: error
- **Window**: 2026-05-07 14:22:00.000Z → 2026-05-07 14:22:15.000Z
- **Session**: `demo_tf_break`
- **Host**: `dev-workstation`

## Summary

Topic /tf_static stopped emitting messages.

## Timeline

| t                          | subsystem | kind    | summary                        | conf. | evidence                  |
|----------------------------|-----------|---------|--------------------------------|-------|---------------------------|
| 2026-05-07 14:22:00.000Z   | ros       | raw     | frequency on /tf_static: 1.0Hz | 1.00  | events.jsonl#L1           |
| ...                        | ...       | ...     | ...                            | ...   | ...                       |
| 2026-05-07 14:22:08.000Z   | anomaly   | trigger | dead_topic on /tf_static       | 1.00  | triggers.json#trg_df6aa081 |

## Likely causes

1. **Topic /tf_static stopped emitting messages.** _(confidence 1.00)_
   - evidence: `events.jsonl#L11`, `triggers.json#trg_df6aa081`

## Fingerprint

- id: `fpr_68463b41f2ab8910`
- detectors: `DeadTopicDetector`
- subsystems: `anomaly`

## Recommended preflight rule

```yaml
check: topic_present
params:
  topic: '/tf_static'
  min_publishers: 1
severity_on_fail: block
```
```

Open `examples/incidents/inc_demo_tf_break/report.md` for the full
bundle.

---

## What works today (verified)

- **Existing v0.3 capture path** continues to work. ROS 2 topic
  introspection, host telemetry, GPU telemetry, four anomaly detectors
  (threshold, frequency drop, dead topic, QoS mismatch), JSONL logging
  with size + age rotation, optional anomaly-triggered rosbag2
  recording.
- **Incident bundle pipeline.** `IncidentBuilder` slices the JSONL log
  into a typed bundle: events, triggers, signatures, timeline,
  fingerprint, report.
- **Markdown report generator.** Every claim in `report.md` resolves to
  a file in the bundle (`events.jsonl#Ln`, `triggers.json#<id>`).
- **Config + version signatures.** Deterministic sha256 hashes of ROS
  distro, RMW, env subset, attached files, and OS / Python / NVIDIA
  driver state. Same inputs produce the same hash.
- **Failure fingerprinting (algorithm v1).** Stable id from detector
  classes, subsystems, signature fields, and topic-set topology. Two
  bundles seeded the same way collide; perturb any input and the id
  changes.
- **Likely-cause ranking.** Heuristic: detector-class weight + severity
  bonus. Confidence below 0.5 carries an explicit caveat. Confidence
  ≥ 0.7 is promoted to the bundle summary. Weights are hand-calibrated;
  see `blackboxrs/incident/cause.py:8-18` for the calibration
  philosophy.
- **Detector hysteresis.** All four threshold-based detectors
  (`threshold`, `frequency`, `clock_skew`, `process_signals`) require
  `min_consecutive_samples` (default 2) violating samples before firing.
  Single-sample noise is suppressed without changing the v0.4.0 wire
  format. See `docs/DETECTOR_CHARACTERISTICS.md` for the measured FPR/TPR.
- **Prevention rules.** `PreventionRule` + `PreflightCheck` YAML I/O,
  `PreflightRunner` with 0/1/2 exit codes (pass / block / warn).
  All seven check kinds ship: `topic_present`, `qos_match`,
  `node_running` are real rclpy graph queries; `env_var`,
  `param_value`, `resource_threshold`, `custom_python` are real
  implementations against `os.environ`, the ROS 2 parameter API,
  `psutil`, and a user-supplied import path respectively. Unknown
  kinds raise at rule-load (no silent `skipped`).
- **CLI.** `robot-blackbox incident build / show / list / attach`,
  `preflight`, `prevention adopt --from-incident / list`.
- **Sample bundle.** Reproducibly generated by
  `python scripts/generate_sample_incident.py`.
- **Runs onboard or off-board.** Same daemon, same bundle format. In
  observer mode the daemon attaches over DDS from any workstation that
  can already see the robot's topics, auto-disables host-bound
  collectors, and tags each bundle with both `observer_host` and
  `observed_host` so reports name the two sides explicitly.
- **Derived timeline events.** `timeline.json` folds silence-interval,
  resource-excursion, and graph-delta rows in alongside the raw and
  trigger rows; the cause ranker scores them as precursors.
- **Snapshot projection.** `SystemSnapshotter` projects the event
  stream into a typed `snapshots.json` series used by the derivers and
  the fingerprint topology signature.
- **Observer-mode E2E.** `tests/integration/test_observer_live.py` boots
  a real rclpy publisher and asserts a `runtime.role: observer` daemon
  fires `anomaly.dead_topic` over DDS within 5s of the publisher going
  silent. Runs inside the Docker Humble CI job.
- **Offline rosbag2 replay.** `robot-blackbox replay-bag` (and
  `blackboxrs.recording.bag_replay`) replay a recorded `.mcap` or
  `.db3` bag through the real detectors entirely offline, with a
  virtual clock pinned to bag time. Reads `.db3` split files (the
  format `ros2 bag record` chunks a long recording into) by merging
  every file `metadata.yaml` lists, not just the first one. Verified
  against a real GO2 hardware recording (~94k messages, 330s); see
  "Works on real GO2 telemetry" above.
- **547 tests pass + 2 gated skips on hosted runners** (one rclpy-gated,
  one gated on a local real-hardware bag path not present in CI). CI
  runs Python 3.10 / 3.11 / 3.12 lint + unit + integration, a benchmark
  regression gate, a detector-FPR smoke run, and a live ROS 2 Humble
  Docker job on every main commit.

## What is planned (not yet built)

- **Real-robot capture.** Every bundle in `examples/incidents/` is
  synthetic-but-real-code-output, produced by
  `scripts/generate_sample_incident.py`. A captured CaresLab GO2
  session bundle is owed; it is the single largest remaining gap.
- Cross-incident clustering (`cluster_id` reserved on
  `FailureFingerprint`; v0.5).
- `incident pack` / `unpack` for portable tarballs (M7).
- Web dashboard. Out of scope for v0.4. The bundle is the artifact.
- Multi-host capture. Single-host first; the bundle format is
  forward-compatible.

---

## Quick start

```bash
git clone https://github.com/yusufdxb/BlackBoxRS.git
cd BlackBoxRS
./setup.sh
source .venv/bin/activate

# 1. Initialise.
robot-blackbox init

# 2. Run the daemon (foreground for the demo; -f shows live output).
robot-blackbox start --foreground &

# 3. ... your robot does its thing, anomalies fire and get logged ...

# 4. Build an incident bundle from the last 5 minutes.
robot-blackbox incident build --since 5m

# 5. Read the report.
robot-blackbox incident show ~/.blackboxrs/incidents/inc_*

# 6. Adopt a prevention rule from the incident.
robot-blackbox prevention adopt --from-incident ~/.blackboxrs/incidents/inc_*

# 7. On the next launch, run preflight.
robot-blackbox preflight
```

For the sample bundle without running anything:

```bash
robot-blackbox incident show examples/incidents/inc_demo_tf_break/
```

### Observer mode (remote workstation)

The quick start above runs the daemon on the robot. To capture
incidents from a laptop that already sees the robot's topics over
DDS:

```bash
# On your workstation. Robot is on the same DDS domain
# (e.g. ROS_DOMAIN_ID matches, RMW_IMPLEMENTATION matches).
ros2 topic list                                # must return the robot's topics

robot-blackbox init
cat > ~/.blackboxrs/config.yaml <<'YAML'
runtime:
  role: observer
  observed_host: go2-edu-01
YAML

robot-blackbox start --foreground &
# ... drive the robot, watch a failure ...
robot-blackbox incident build --since 5m
robot-blackbox incident show ~/.blackboxrs/incidents/inc_*
```

The bundle's `report.md` header shows both sides:

```
- **Observer**: `my-laptop`
- **Observed**: `go2-edu-01`
```

Host-bound collectors (per-process CPU / RSS, host CPU / memory
thresholds) are skipped automatically because their values would
describe the observer laptop, not the robot. See
`docs/QUICKSTART_REMOTE.md` for DDS setup, troubleshooting, and what
each detector measures in observer mode.

---

## Architecture (high-level)

```mermaid
graph TD
    subgraph Daemon["BlackBoxRS daemon"]
        Monitors["ros_monitor + system_monitor + recording"]
        EventBus["core.event_bus"]
        Anomaly["anomaly_engine"]
        Writer["logging.RotatingJsonlWriter"]
        Monitors --> EventBus
        EventBus --> Anomaly
        EventBus --> Writer
    end

    Logs["~/.blackboxrs/logs/*.jsonl"]
    Writer --> Logs

    subgraph Build["blackboxrs incident build"]
        Builder["IncidentBuilder over the log slice"]
        Events["events.jsonl"]
        Triggers["triggers"]
        Signatures["signatures"]
        Snapshots["snapshots (M3.5)"]
        Timeline["timeline"]
        Fingerprint["fingerprint"]
        Cause["likely-cause"]
        Report["report.md"]
        Builder --> Events
        Events --> Triggers
        Events --> Signatures
        Events --> Snapshots
        Events --> Timeline
        Events --> Fingerprint
        Events --> Cause
        Events --> Report
    end

    Logs --> Builder

    IncidentDir["~/.blackboxrs/incidents/inc_(id)/"]
    Report --> IncidentDir

    subgraph Preflight["blackboxrs preflight"]
        Rules["Loaded PreventionRules (YAML)"]
        Checks["topic_present / qos_match / node_*"]
        PReport["PreflightReport: pass / warn / block"]
        Rules --> Checks
        Checks --> PReport
    end

    IncidentDir --> Rules
```

See `docs/ARCHITECTURE.md` for the full system design.

---

---

## License

MIT. See `LICENSE`.

## Author

Yusuf Guenena ([yusufdxb](https://github.com/yusufdxb)).
