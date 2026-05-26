# Orphan Detector Producers: Design Spec

Status: draft, v0.4.0.dev0 (post observer-mode pivot, 371 tests green).
Owner: Yusuf Guenena.
Audience: contributors picking up producer work for the three currently
orphaned detectors.

## Context

After the observer-mode pivot (commits c7f2bce..84e3210), three detectors
ship with full unit-test coverage but are intentionally not wired into the
live engine, because no module currently emits the events they consume:

| Detector                      | Consumed event type        | Source label      |
|-------------------------------|----------------------------|-------------------|
| `TfTopologyDetector`          | `ros.tf`                   | `ros_monitor`     |
| `ClockSkewDetector`           | `system.clock_skew`        | `system_monitor`  |
| `ProcessSignalsDetector`      | `system.process_signals`   | `system_monitor`  |

The detector source files (`blackboxrs/anomaly_engine/detectors/{tf_topology,
clock_skew, process_signals}.py`) already specify the producer payload
shapes in their module docstrings. This document elevates those shapes to
a contract, plus failure-mode coverage, observer-mode constraints, cost,
tests, and gating.

A producer here is a small in-process collector that lives inside either
`blackboxrs/ros_monitor/` or `blackboxrs/system_monitor/collectors/`,
samples on a fixed cadence, normalizes the reading, and publishes one
`BlackBoxEvent` per sample to the shared `EventBus`. Producers do not
talk to detectors directly; the bus does that.

---

## 1. TF Producer

### 1.1 What the detector needs

`TfTopologyDetector` consumes one event per TF snapshot:

```
BlackBoxEvent.ros_event(
    event_type="ros.tf",
    data={
      "expected_frames": ["base_link", "odom", "map", ...],  # optional
      "edges": [
        {"parent": "odom", "child": "base_link",
         "last_update_sec_ago": 0.05, "is_static": False},
        {"parent": "base_link", "child": "imu_link",
         "last_update_sec_ago": 0.0, "is_static": True},
        ...
      ],
    },
)
```

Field requirements:

- `edges[*].parent`, `edges[*].child`: non-empty strings, exact frame_id
  values as published.
- `edges[*].last_update_sec_ago`: float, seconds since the last `TFMessage`
  carrying this `(parent, child)` pair landed on the wire. For
  `is_static: True` edges this is set to `0.0`.
- `edges[*].is_static`: bool. True if the edge came from `/tf_static`.
- `expected_frames`: optional list of frames the operator declares must
  exist. Without this, orphan detection is silently skipped (see detector
  source). Producer reads from config (see 1.3).

Snapshot cadence: 1 Hz default, configurable in `RosMonitorConfig`.
Cheaper than per-message because the detector is stateless across
snapshots; one snapshot per second gives stale-edge detection within the
configured `stale_timeout_sec` (default plan: 2.0 s).

### 1.2 Producer responsibilities

The TF producer (`blackboxrs/ros_monitor/tf_snapshotter.py`, new) must:

1. Subscribe to `/tf` (sensor data QoS, depth 100) and `/tf_static`
   (transient_local QoS, depth 1).
2. Maintain an in-memory map keyed by `(parent_frame_id, child_frame_id)`,
   storing the last `header.stamp` and an `is_static` flag.
3. On each snapshot tick: walk the map, compute `last_update_sec_ago` as
   `now - last_stamp` (use the node clock, the same clock the detector
   sees), and emit one `ros.tf` event.
4. Merge `expected_frames` from `RosMonitorConfig.tf.expected_frames`
   (new field) into the emitted payload.
5. Garbage-collect entries older than 60 s for non-static edges so the
   snapshot does not grow unbounded across a multi-hour session.

The producer must not run TF graph traversal itself; structural analysis
is the detector's job. The producer only normalizes raw `TFMessage`
streams into the snapshot shape.

### 1.3 API and topic contract

- Topics consumed: `/tf` (`tf2_msgs/msg/TFMessage`), `/tf_static`
  (`tf2_msgs/msg/TFMessage`).
- QoS:
  - `/tf`: `RELIABLE`, `VOLATILE`, history `KEEP_LAST` depth 100. Matches
    the de-facto convention from `tf2_ros` broadcasters.
  - `/tf_static`: `RELIABLE`, `TRANSIENT_LOCAL`, depth 1. Required so
    late subscribers (observer mode, started after the robot) still
    receive the static edges.
- Event published: `BlackBoxEvent` with `source="ros_monitor"`,
  `event_type="ros.tf"`, payload exactly as in 1.1.
- Config addition (`RosMonitorConfig.tf`):
  - `snapshot_hz: float = 1.0`
  - `expected_frames: list[str] = []`
  - `gc_age_sec: float = 60.0`
- No new `.msg` definitions. Existing `tf2_msgs/TFMessage` is sufficient.

### 1.4 Failure modes caught

| Pattern                              | Detector `failure_kind` | Incident shape                                                                 |
|--------------------------------------|-------------------------|--------------------------------------------------------------------------------|
| Missing expected frame               | `orphan_frame`          | `summary: "Expected TF frame 'X' is missing from /tf graph"`, tags `[tf, orphan]` |
| Stale dynamic edge past timeout      | `stale_edge`            | `summary: "TF edge odom -> base_link not updated for 3.4s"`, tags `[tf, stale]`  |
| Same child under two parents         | `multi_parent`          | `summary: "TF frame 'imu_link' has multiple parents (base_link, body)"`        |
| `/tf_static` republished mid-session | (via `dead_topic` + `tf_topology` correlation) | precursor: pub count 1->0 then back, fingerprint stable on `topic=/tf_static` |
| Frame ID typo (e.g. `base_link` vs `baseLink`) | `orphan_frame` | fires only when typo'd frame is in `expected_frames`                          |
| Clock-jump-induced negative `last_update_sec_ago` | logged WARN, dropped | producer guards against clock rewinds                                          |

Sample incident YAML (matching `examples/incidents/inc_demo_tf_break/`
layout):

```
incident_id: inc_<timestamp>_<short>
title: "Stale TF edge odom -> base_link"
severity: error
summary: "TF edge 'odom' -> 'base_link' has not been updated for 3.42s (timeout 2.0s)."
tags: [tf, stale_edge]
fingerprint:
  payload:
    detector_classes:
      - blackboxrs.anomaly_engine.detectors.tf_topology.TfTopologyDetector
    signature_fields:
      tf_topology:
        - [failure_kind, stale_edge]
        - [frame, base_link]
        - [parent, odom]
    subsystems: [ros]
triggers: [trg_<short>]
```

### 1.5 Observer-mode compatibility

Compatible with `runtime.role: observer`. `/tf` and `/tf_static` are
ordinary DDS topics; an off-board workstation that can `ros2 topic echo
/tf` against the robot can subscribe to them. The `transient_local`
durability on `/tf_static` is the load-bearing detail: without it, late
observer attaches would miss static frames and false-positive on
`orphan_frame`. No on-robot install required.

### 1.6 Implementation cost

Size: **M**.

Key risks:

- `/tf_static` QoS mismatch: if the robot publishes static frames with a
  non-default profile, the observer subscription needs to match or it
  will silently drop. Mitigation: log subscription compatibility, surface
  via existing `qos_mismatch` detector.
- Multi-publisher `/tf` is common (controller, localization, sensors all
  broadcast). Producer must dedupe by `(parent, child)` last-writer-wins
  on `header.stamp`, not by publisher GID.
- Bag playback with `--clock`: `last_update_sec_ago` must use the node
  clock (which honors `/clock`), not wall time.

### 1.7 Test plan

- Unit: `tests/unit/ros_monitor/test_tf_snapshotter.py`
  - Empty graph -> emits snapshot with empty `edges`.
  - One dynamic edge -> appears with correct `last_update_sec_ago`.
  - Static edge from `/tf_static` -> `is_static: True`, age `0.0`.
  - Multi-parent input -> both edges preserved (detector does the rest).
  - GC: edges older than `gc_age_sec` removed from in-memory map.
  - Clock rewind: negative ages clamped to `0.0` with WARN.
- Integration: `tests/integration/test_tf_producer_to_detector.py`
  - Spin a real `rclpy` node in a fixture, publish synthetic `TFMessage`
    streams, assert the detector raises the expected `failure_kind`.
  - Observer-mode variant: producer on host A, detector pipeline on host
    B, loopback DDS, assert end-to-end fingerprint stability.

---

## 2. Clock Producer

### 2.1 What the detector needs

`ClockSkewDetector` consumes:

```
BlackBoxEvent.system_event(
    event_type="system.clock_skew",
    data={
      "sources": [
        {"name": "system",            "epoch_sec": 1234567890.000},
        {"name": "ntp:pool.ntp.org",  "epoch_sec": 1234567890.412},
        {"name": "ros:/clock",        "epoch_sec": 1234567889.180},
        {"name": "peer:go2-edu-01",   "epoch_sec": 1234567890.050},
      ],
    },
)
```

Field requirements:

- `sources[*].name`: string, free-form but stable per source so
  fingerprinting collides across snapshots. Conventions:
  - `system`: local `time.time()`.
  - `ntp:<peer>`: from `ntpq -p` or `chronyc tracking`, one entry per
    selected peer.
  - `ros:/clock`: latest message stamp on `/clock` (sim time bridge).
  - `peer:<observed_host>`: optional, requires a `time_query` service on
    the robot. Out of scope for v1.
- `sources[*].epoch_sec`: float seconds since UNIX epoch, all sampled at
  the same producer tick.

Cadence: 1 Hz. The detector compares pairwise within a snapshot, so
inter-tick jitter does not matter.

### 2.2 Producer responsibilities

The clock producer (`blackboxrs/system_monitor/collectors/clock.py`, new):

1. On each tick, sample all configured sources within a tight window
   (target: <5 ms across all reads, to keep skew computation honest).
2. Read `system` from `time.time()`.
3. Read NTP peers by shelling `chronyc -c tracking` (preferred,
   machine-readable) or `ntpq -p` (fallback). Parse offset, add to local
   epoch.
4. Read `/clock` only if `runtime.use_sim_time` is true, by subscribing
   to `/clock` (`rosgraph_msgs/msg/Clock`, `BEST_EFFORT`, depth 1) and
   caching the last stamp.
5. Emit one `system.clock_skew` event per tick. Empty source lists are
   dropped, not emitted, so the detector never sees junk.

### 2.3 API and topic contract

- Topics consumed: `/clock` (optional, `rosgraph_msgs/msg/Clock`,
  `BEST_EFFORT`, `VOLATILE`, depth 1).
- Subprocess: `chronyc tracking` or `ntpq -p`. No long-running daemon
  needed.
- Event published: `source="system_monitor"`,
  `event_type="system.clock_skew"`, payload as in 2.1.
- Config addition (`SystemMonitorConfig.clock`):
  - `enabled: bool = True`
  - `sample_hz: float = 1.0`
  - `include_ntp: bool = True`
  - `include_ros_clock: bool = "auto"` (auto = mirrors `use_sim_time`)
  - `ntp_tool: Literal["chronyc", "ntpq", "auto"] = "auto"`
- No new `.msg` definitions.

### 2.4 Failure modes caught

| Pattern                                    | Sample incident                                                              |
|--------------------------------------------|------------------------------------------------------------------------------|
| System clock drift vs NTP peer >100 ms     | `summary: "Clock skew between 'system' and 'ntp:pool.ntp.org' is 0.42s"`     |
| `/clock` lag vs system clock during sim    | `summary: "Sim clock 'ros:/clock' lags 'system' by 1.21s"`, tag `sim-time`   |
| Two NTP peers disagree (stratum split)     | `source_a=ntp:peer-a, source_b=ntp:peer-b` fingerprint stable across reruns  |
| Observer vs robot clock divergence         | requires `peer:<host>` source (v2)                                           |
| Sudden clock step (NTP slam)               | one-shot anomaly, precursor to TF `ExtrapolationException`                   |

Sample incident YAML:

```
incident_id: inc_<timestamp>_<short>
title: "Clock skew between system and NTP"
severity: warning
summary: "Clock skew between 'ntp:pool.ntp.org' and 'system' is 0.412s (tolerance 0.100s)."
tags: [clock, skew]
fingerprint:
  payload:
    detector_classes:
      - blackboxrs.anomaly_engine.detectors.clock_skew.ClockSkewDetector
    signature_fields:
      clock_skew:
        - [source_a, ntp:pool.ntp.org]
        - [source_b, system]
    subsystems: [system]
```

### 2.5 Observer-mode compatibility

Partially compatible.

- `system` source reflects the observer's clock, not the robot's. That
  is honest: a workstation's clock is what timestamps the bundle.
- `ntp:*` reflects observer-side NTP peers. Useful for catching observer
  drift, less useful for the robot.
- `ros:/clock` works in observer mode if the robot publishes `/clock`
  (sim or bag playback). DDS-bound, no install.
- `peer:<host>` source (true robot clock) requires either an on-robot
  time service or `ssh date +%s.%N` polling. **REQUIRES on-robot install
  or SSH for direct robot-clock comparison.** Without it, observer-mode
  clock skew is observer-relative only.

Producer must annotate the emitted event with the observer/observed
distinction. The bundle assembler already records both hosts; the
detector's `all_sources` payload preserves enough context for the report.

### 2.6 Implementation cost

Size: **S** (without `peer:` source), **M** (with on-robot time peer).

Key risks:

- `chronyc` absent on minimal images; fallback to `ntpq` (Ubuntu has
  `ntp` or `chrony`, not both). Skip NTP cleanly if neither is present.
- Sampling skew: reading 4 sources sequentially can itself take 50 ms.
  Producer must record the read window and skip the snapshot if the
  window exceeds half of `max_skew_sec`.
- `/clock` durability: late observer attach misses last stamp. Acceptable;
  next message arrives within one sim step.

### 2.7 Test plan

- Unit: `tests/unit/system_monitor/test_clock_collector.py`
  - Parse fixtures of `chronyc -c tracking` output.
  - Parse fixtures of `ntpq -p` output.
  - Fallback when neither tool is installed (skip cleanly, no event).
  - Read-window guard: synthetic 200 ms sample window with 100 ms
    threshold drops the snapshot.
- Integration: `tests/integration/test_clock_producer_to_detector.py`
  - Inject synthetic source lists, assert worst-pair selection and
    fingerprint stability.
  - Observer-mode: confirm `peer:` source absence does not break the
    producer or the detector.

---

## 3. Process Signals Producer

### 3.1 What the detector needs

`ProcessSignalsDetector` consumes:

```
BlackBoxEvent.system_event(
    event_type="system.process_signals",
    data={
      "sampling_interval_sec": 1.0,
      "processes": [
        {"pid": 1234, "name": "scan_node",  "cpu_percent": 95.0, "rss_mb": 412.0},
        {"pid": 1235, "name": "controller", "cpu_percent": 12.0, "rss_mb":  98.0},
        ...
      ],
    },
)
```

Field requirements:

- `processes[*].pid`: int, OS pid. Not used for fingerprinting (pids
  recycle on restart).
- `processes[*].name`: string, process command name (e.g. argv[0]
  basename). This is the fingerprint key, so it must be stable across
  launches of the same node.
- `processes[*].cpu_percent`: float, normalized to 100% per logical CPU
  (psutil default). 800% on an 8-core box is fine.
- `processes[*].rss_mb`: float, resident set size in megabytes.
- `sampling_interval_sec`: float, the wall interval since the prior
  sample. Required because `psutil.Process.cpu_percent()` is delta-based.

Cadence: 1 Hz. Aligns with existing `system_monitor` collectors.

### 3.2 Producer responsibilities

The process producer (`blackboxrs/system_monitor/collectors/process.py`,
new):

1. On startup, snapshot all processes whose `cmdline` matches one of
   `tracked_patterns` (glob list from config; default
   `["*ros2*", "*rclpy*", "*controller*", "*nav2*", "*moveit*"]`).
2. On each tick, refresh the matched set (new pids appear, dead pids
   drop), call `proc.cpu_percent(interval=None)` and `proc.memory_info()`
   for each.
3. Normalize: `name` is `proc.name()` (not full cmdline). Drop processes
   with `cpu_percent is None` (first-sample artifact).
4. Emit one `system.process_signals` event per tick. If the matched set
   is empty, skip emission (no event is better than an empty payload
   that the detector has to ignore).
5. Maintain a small LRU of dead pids to avoid log spam on every tick.

### 3.3 API and topic contract

- No ROS topics consumed.
- Subprocess: none. Uses `psutil` (already a hard dependency, per the
  existing collectors and the detector docstring).
- Event published: `source="system_monitor"`,
  `event_type="system.process_signals"`, payload as in 3.1.
- Config addition (`SystemMonitorConfig.process_signals`):
  - `enabled: bool = True`
  - `sample_hz: float = 1.0`
  - `tracked_patterns: list[str] = ["*ros2*", "*rclpy*", ...]`
  - `max_tracked: int = 64` (hard cap to keep snapshot size bounded)
- No new `.msg` definitions.

### 3.4 Failure modes caught

| Pattern                                | Sample incident                                                       |
|----------------------------------------|-----------------------------------------------------------------------|
| Runaway CPU loop in a tracked node     | `summary: "Process 'scan_node' cpu_percent=95.0%, threshold 90.0%"`   |
| Memory leak (RSS climb)                | `summary: "Process 'planner' rss_mb=4096, threshold 2048"`            |
| Sustained 100% on one core, IPC starve | precursor for `dead_topic` on the node's published topics             |
| Sibling node OOM-killed                | producer notices pid drop; emits separate `process.lifecycle` event in v2 |
| Wrong node taking the CPU              | `process_name` fingerprint distinguishes runaway candidates           |

Sample incident YAML:

```
incident_id: inc_<timestamp>_<short>
title: "Runaway CPU in scan_node"
severity: warning
summary: "Process 'scan_node' (pid 1234) cpu_percent=95.0%, exceeding threshold 90.0%."
tags: [process, cpu]
fingerprint:
  payload:
    detector_classes:
      - blackboxrs.anomaly_engine.detectors.process_signals.ProcessSignalsDetector
    signature_fields:
      process_signals:
        - [process_name, scan_node]
        - [metric, cpu_percent]
    subsystems: [system]
```

### 3.5 Observer-mode compatibility

**REQUIRES on-robot install.** `psutil` reports the host it runs on; a
workstation cannot enumerate processes on the robot over DDS. There is no
DDS-native equivalent of `ps`. Options:

1. **Onboard mode only** (recommended for v1). When
   `runtime.role == "observer"`, the producer auto-disables and emits one
   informational log line at startup, matching the existing pattern for
   host CPU/memory collectors after the pivot.
2. **Bridge node** on the robot (out of scope for v1). A thin
   `blackboxrs_robot_bridge` `rclpy` node publishes the same payload over
   a DDS topic `/blackboxrs/process_signals`
   (`std_msgs/msg/String` JSON for v1, or a custom `.msg` in v2). The
   observer-side producer then subscribes instead of calling `psutil`.
   This is the only producer of the three where a custom `.msg` may be
   justified later.

For v1, ship option 1. Observer-mode users get TF and clock coverage but
not process signals, which matches the README's current honesty about
the observer/onboard split.

### 3.6 Implementation cost

Size: **S** for onboard-only v1, **M** for the bridge node in v2.

Key risks:

- `psutil.Process.cpu_percent()` returns 0 on the first call per pid;
  producer must seed the baseline at startup or accept one warm-up
  sample per new pid.
- `cmdline()` access on processes owned by other users can `AccessDenied`
  on Linux; producer must catch and skip silently.
- `tracked_patterns` glob matching against full cmdline (not just name)
  is what catches `python3 -m my_pkg.node`. Document this.
- Container / cgroup scoping: if BlackBoxRS runs in a different PID
  namespace than ROS nodes, the producer sees nothing. Document, do not
  try to escape.

### 3.7 Test plan

- Unit: `tests/unit/system_monitor/test_process_collector.py`
  - Fixture: monkeypatch `psutil.process_iter` to return a fake process
    set; assert event shape, name normalization, RSS conversion.
  - Pattern matching: ensure `*ros2*` matches `ros2 run foo bar`.
  - `AccessDenied` swallowed; producer continues.
  - First-sample warm-up: cpu_percent=0.0 entries dropped, not emitted.
  - Empty matched set: no event emitted.
  - Observer mode (`runtime.role == "observer"`): producer disables,
    logs once, does not emit.
- Integration: `tests/integration/test_process_producer_to_detector.py`
  - Spawn a CPU-burner subprocess in a fixture (Python `while True:`
    loop), assert `cpu_percent` excursion fires the detector within
    `2 / sample_hz` seconds, fingerprint matches expected
    `process_name`.

---

## Rollout Order

Build in this order:

1. **TF producer first.** Highest evidence value per dollar. TF breaks
   are the single most common failure mode in the existing demo bundles
   (`inc_demo_tf_break` already exists), and the detector covers three
   distinct failure kinds. Fully observer-compatible, zero new on-robot
   install. Unlocks the third demo scenario and a real-world incident
   class.
2. **Clock producer second.** Small, mostly subprocess parsing. Clock
   skew is a documented upstream cause of TF `ExtrapolationException`,
   so it pairs naturally with item 1 in the cause-ranker precursor
   chain. Observer-mode partial coverage is acceptable for v1.
3. **Process signals producer last.** Highest constraint (onboard-only
   for v1), narrowest new failure-mode coverage relative to existing
   CPU/memory collectors. Wait until the observer-mode user base has
   asked for it before investing in the bridge node.

## Gating Criteria for Re-enabling Detectors

A detector returns to the live engine (registered in
`anomaly_engine/detectors/loader.py` and listed in the default config)
when **all** of the following hold for its producer:

- Producer is merged on `main` with unit + integration tests green.
- Producer has been dogfooded on at least one live ROS 2 stack (GO2,
  TurtleBot, or sim) for **7 consecutive days** of session capture.
- Over those 7 days, the detector has **fewer than 1 false positive per
  day** averaged, measured against a small hand-labeled incident set
  (target: 20 labeled snapshots per detector, mixed real and synthetic).
- Fingerprint stability check: replaying the same captured session twice
  produces identical `fingerprint_id` for every emitted anomaly.
- Observer-mode behavior matches Section 5 of each producer's spec:
  either fully compatible (TF, clock) or cleanly disabled with a log
  line (process signals).
- README's "What is planned" table is updated to move the detector from
  planned to verified, and `STATUS_AND_LIMITATIONS_REWRITE.md` is
  updated in the same PR.

Detector-specific additions:

- **TF**: at least one captured incident must include all three
  `failure_kind` values (`orphan_frame`, `multi_parent`, `stale_edge`)
  across the dogfood window.
- **Clock**: at least one captured incident with a real (not injected)
  NTP-source pair triggering, to confirm the parser handles production
  `chronyc` / `ntpq` output, not just fixtures.
- **Process signals**: at least one captured incident from a real
  runaway-CPU node, not a synthetic burner, before enabling by default.
