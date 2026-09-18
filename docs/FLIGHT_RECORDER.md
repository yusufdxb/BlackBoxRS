# Flight recorder for GO2 hardware experiments

`robot-blackbox flight` is a passive recorder for Unitree GO2 experiments
(HELIX motion-arbitration stages, Phoenix sim2real runs). When a run fails or
behaves unexpectedly, the incident bundle should be enough to work out what
happened without repeating the lab session.

It is **observation only**. The recorder node creates subscriptions and graph
queries, nothing else: no publishers, no services, no parameter services, no
rosout. rclpy itself creates the node's `/parameter_events` publisher (Humble
has no switch for it); the recorder never sets a parameter.
`tests/unit/flight/test_no_publish.py` checks both the source tree and a live
node, and preflight re-checks the live node on the robot (`no_publish`).

It runs on the existing Python backend. The native capture work is separate
and is not used here.

## Commands

| Command | What it does |
|---|---|
| `robot-blackbox flight preflight --profile go2_helix` | zero-motion GO / GO WITH WARNINGS / NO-GO (exit 0 / 2 / 1) |
| `robot-blackbox preflight --profile go2_helix` | same, through the top-level preflight |
| `robot-blackbox flight record --profile go2_helix` | record until Ctrl-C |
| `robot-blackbox flight mark "note"` | manual marker (file drop into `<evidence>/control/`; also `kill -USR1 <pid>`) |
| `robot-blackbox flight show <bundle>` | print `report.md` |
| `robot-blackbox flight replay <bundle> [--retrigger] [--write]` | regenerate the report offline and compare |
| `robot-blackbox flight rehearse [--scenario all]` | offline rehearsal on synthetic traffic |

Common options: `--evidence-dir`, `--experiment <label>`,
`--experiment-repo <path>` (records that repo's SHA and dirty flag with a
read-only git query), `--exclude-topic <name>` (drop a topic for one run;
recorded in the session and the report).

## Profiles

Profiles are YAML files in `blackboxrs/flight/profiles/`. The bundle stores
the profile's SHA-256 and its full resolved text, so a bundle carries the exact
capture contract it ran under.

* `go2`: every topic below.
* `go2_helix`: `go2` without `/cmd_vel`. **Use this whenever HELIX runs.**
  HELIX preflight check C6 requires `/cmd_vel` to have exactly one consumer
  (the GO2 sport sink), and `ArbiterStatus.sink_subscribers` counts `/cmd_vel`
  subscribers. Verified: with `go2`, HELIX's stage A preflight reported
  `[FAIL] C6 final sink on /cmd_vel: consumers=['/blackbox/blackboxrs_flight_recorder',
  '/helix_go2_sport_sink']` and refused the stage. Under `go2_helix` the report
  rebuilds `/cmd_vel` from `ArbiterStatus.out_*` (what the arbiter published)
  and from the sink trace `input` field (what the sink received), and labels
  the source. BlackBoxRS preflight fails with this remedy if HELIX is running
  and the profile subscribes to `/cmd_vel`.

Every topic is optional. A topic that is absent, has no publisher, has a
different type, or whose message package cannot be imported is recorded in the
manifest with that status and reason. It is never replaced by synthetic data,
and report sections that depend on it say `topic_absent` or similar rather
than `not_observed`.

| Topic | Type | Role | Notes |
|---|---|---|---|
| `/api/sport/request` | `unitree_api/msg/Request` | sport_request |  |
| `/api/sport/response` | `unitree_api/msg/Response` | sport_response |  |
| `/utlidar/robot_odom` | `nav_msgs/msg/Odometry` | odometry | expected 150 Hz; stale after 0.5 s |
| `/sportmodestate` | `unitree_go/msg/SportModeState` | go2_state | expected 295 Hz; payload stored at <= 50 Hz |
| `/lf/sportmodestate` | `unitree_go/msg/SportModeState` | go2_state |  |
| `/lowstate` | `unitree_go/msg/LowState` | go2_state | expected 500 Hz; payload stored at <= 20 Hz |
| `/lf/lowstate` | `unitree_go/msg/LowState` | go2_state |  |
| `/wirelesscontroller` | `unitree_go/msg/WirelessController` | operator_remote |  |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | cmd_vel_out |  |
| `/nav/cmd_vel` | `geometry_msgs/msg/Twist` | cmd_vel_source |  |
| `/teleop/cmd_vel` | `geometry_msgs/msg/Twist` | cmd_vel_source |  |
| `/helix/faults` | `helix_msgs/msg/FaultEvent` | helix_fault |  |
| `/helix/recovery_hints` | `helix_msgs/msg/RecoveryHint` | recovery_hint |  |
| `/helix/recovery_actions` | `helix_msgs/msg/RecoveryAction` | recovery_action |  |
| `/helix/hold` | `helix_msgs/msg/HelixHold` | helix_hold | stale after 0.5 s |
| `/helix/arbiter/status` | `helix_msgs/msg/ArbiterStatus` | arbiter_status | stale after 0.5 s |
| `/helix/sink/trace` | `std_msgs/msg/String` | sink_trace |  |
| `/helix/node_health` | `diagnostic_msgs/msg/DiagnosticArray` | helix_health |  |
| `/helix/explanations` | `std_msgs/msg/String` | helix_diagnosis_text |  |
| `/phoenix/estop` | `std_msgs/msg/Bool` | phoenix_safety |  |
| `/phoenix/shield` | `std_msgs/msg/Float64MultiArray` | phoenix_safety |  |
| `/joint_group_position_controller/command` | `std_msgs/msg/Float64MultiArray` | phoenix_action | payload stored at <= 50 Hz |
| `/imu/data` | `sensor_msgs/msg/Imu` | phoenix_observation | payload stored at <= 50 Hz |
| `/joint_states` | `sensor_msgs/msg/JointState` | phoenix_observation | payload stored at <= 50 Hz |
| `/phoenix/foot_force` | `std_msgs/msg/Float32MultiArray` | phoenix_observation | payload stored at <= 50 Hz |

Also captured: ROS graph changes (a full node/topic/publisher snapshot every
5 s plus every change in between), host CPU (total and per core), RAM, swap,
load, recorder CPU and RSS, GPU load and temperature (nvidia-smi, or the Jetson
sysfs load node, probed across the known L4T paths), all thermal zones, and
evidence-disk free space. They are sampled at 2 Hz on a separate thread.

Rates come from `docs/go2_field_notes.md` (measured on the robot). Stop
criteria (0.03 m/s stopped, 1.5 s deadline, 0.05 m/s moving) are copied from
HELIX `hw_stage.py` so the report judges a stop the way the stage runner does.

## Preflight (zero motion)

Preflight starts the same subscribe-only node with every trigger disabled,
listens for `preflight.listen_sec` (3 s), and checks:

| Check | FAIL when | WARN when |
|---|---|---|
| `ros_env` | rclpy not importable | ROS_DISTRO unset |
| `rmw` | RMW differs from the profile's `expect_rmw` (Cyclone) | RMW_IMPLEMENTATION unset |
| `cyclonedds_config` | pinned interface missing or down | CYCLONEDDS_URI unset |
| `evidence_writable` | 4 KiB write + fsync + unlink fails | |
| `disk_capacity` | < 256 MB free | < `min_free_disk_mb` (2048) |
| `clock_wall` | before 2026 or before HEAD commit time (no RTC) | |
| `clock_sync` | | NTP sync not confirmed |
| `clock_stepping` | wall clock moved > 50 ms against monotonic while listening | |
| `message_types` | a required type not importable | any profile type not importable |
| `no_publish` | recorder node has any publisher besides `/parameter_events` | |
| `topics_present` | a required topic absent | HELIX chain stages unobservable; topic published but its type not importable |
| `topic_types` | publisher type differs from the profile | |
| `freshness_rates` | a required topic silent | rate < 80 % of expected, stale, or silent publisher |
| `frame_ids` | frame id differs (odom: `odom` / `base_link`) | no message to check |
| `qos_compat` | | incompatible publisher/subscriber pair among other nodes (HELIX C4 gates motion edges) |
| `expected_nodes` | | HELIX nodes not running |
| `helix_compat` | HELIX running and the profile subscribes to `/cmd_vel` | |
| `recorder_load` | recorder > 50 % of one core or > 500 MB RSS while listening | |
| `bundle_roundtrip` | open, write, finalize, re-read and re-analyze a bundle fails or the report differs | |

Any FAIL is **NO-GO**, else any WARN is **GO WITH WARNINGS**, else **GO**. Each
reason is printed, and the full result is written to
`<evidence>/preflight/<stamp>/preflight.json`.

## Triggers

| Trigger | Fires when |
|---|---|
| `helix_hold_asserted` | `/helix/hold` goes false to true (or is already true at first observation; flagged `observed_edge: false`) |
| `recovery_action_stop` | RecoveryAction `STOP_AND_HOLD` with status `ACCEPTED` |
| `arbiter_forced_zero` | ArbiterStatus reason enters HELIX_HOLD, HELIX_STATE_STALE or HELIX_STATE_MISSING |
| `node_disappeared` | a watched node leaves the graph (profile `expected_nodes` plus any node seen publishing a profile topic) |
| `topic_stale` | a topic with `stale_after_sec` (odometry, hold, arbiter status) goes silent after it was seen; re-arms on the next message |
| `manual_marker` | `flight mark` or SIGUSR1 |

A trigger while a bundle is open is attached to it as a secondary trigger and
extends the post window, capped at 3 x `post_trigger_sec` after the first
trigger. So one fault chain gives one bundle.

## Rolling window

Every record enters a ring buffer on the recorder monotonic clock. The ring
holds at least `pre_trigger_sec` (10 s) and is capped by `max_records` and
`max_bytes`. Evictions inside the window are counted and reported. On a
trigger, the window before it goes to the bundle at once, and later records
stream into the same bundle until `post_trigger_sec` (15 s) after the last
attached trigger. Topics with `store_max_hz` keep a record for every arrival
(so rates, gaps and losses stay exact) but a payload only at that rate; they
are taken serialized, and only stored samples are deserialized.

## Bundle format

```
<evidence>/<session_id>/<bundle_id>/          (<bundle_id>.partial while capturing)
    manifest.json   schema blackboxrs.flight.manifest.v1: status, session provenance,
                    profile (name, sha256, resolved text), triggers, pre-window,
                    per-topic availability, recorder and writer counters
    records.jsonl   every record in the window, in ingest order
    report.json     schema blackboxrs.flight.report.v1 (regenerable)
    report.md       human-readable summary
<evidence>/<session_id>/session.json          session-level status and topic availability
```

Bundle I/O, fsync (at least every second) and the final analysis run on a
writer thread, so the recorder's executor never waits on the disk. Status is
`complete`, `interrupted` (recorder stopped inside the post window),
`write_failed` (disk error; records kept in memory and the report still
written if possible) or, for a directory left as `.partial` by a killed
recorder, `interrupted_unfinalized`. A torn last line is tolerated and
counted. Verified by killing a live recorder with SIGKILL 3 s after a marker:
the `.partial` bundle replayed to a report.

Record fields: `kind` (msg, sys, graph, trigger, marker, health, clock_jump),
`seq`, `t_mono_ns`, `t_wall_ns`, `t_ros_ns`, and for messages `topic`, `role`,
`type`, `dds_src_ns`, `dds_rx_ns`, `pub_stamp_s`, `pub_stamp_domain`, `data`.

Report contents: provenance (BlackBoxRS SHA and dirty flag, experiment repo
SHAs, profile hash, host, platform, Jetson model and L4T release when present,
ROS distro, RMW, domain, session and experiment id), triggers, participating,
disappeared and appeared nodes, per-topic availability, counts, rates
before / during (first 2 s) / after the trigger, gaps, duplicates,
out-of-order stamps and publisher sequence losses, HELIX fault, diagnosis,
hint, action, hold and arbiter state, the stop chain, intervals, input and
final twist, StopMove request id and response code, odometry speed, stop
latency and distance, CPU / RAM / GPU / thermal, data-quality and clock
warnings, and verdicts.

**Verdicts** are only given for criteria with a written source (HELIX
HW_MOTION_TEST.md stage E): full chain observed, 0 nonzero outputs while held,
no Move (1008) while held, StopMove answered with code 0, moving at the fault,
stopped within 1.5 s. Each is PASS, FAIL or INCOMPLETE. INCOMPLETE means the
evidence cannot decide, for example odometry absent, stale or with a gap, the
bundle ending too early, or the robot not moving at the hold. A bundle with no
HELIX stop gets no verdicts.

## Timing methodology

HELIX found that observer receive times had been read as causal emission
times. Here the two are kept apart.

Clock domains:

| Field | Clock | Taken by |
|---|---|---|
| `t_mono_ns` | recorder CLOCK_MONOTONIC | recorder callback |
| `t_wall_ns` | recorder CLOCK_REALTIME | recorder callback |
| `dds_src_ns` | **publisher host** wall clock | DDS, at the publisher's write |
| `dds_rx_ns` | recorder host wall clock | DDS, at reception (Fast DDS only; Cyclone on Humble reports 0, stored as null) |
| `pub_stamp_s` | declared per role: `helix_payload_wall` (HELIX stamps, sink `t_wall`), `robot_clock` (odometry and sport-state headers) | the publisher, inside the message |

Humble's executor drops the rmw message info, so the recorder uses a small
executor subclass that passes the DDS timestamps to its callbacks.

Rules:

1. **Emission intervals** are only computed when both stages share a clock:
   both roles are in the profile's `co_hosted_roles` (HELIX, arbiter, sink,
   `/cmd_vel`, sport request: all on the payload) and both carry a DDS source
   timestamp, or both carry an embedded stamp in the same declared domain.
2. Everything else is a **receipt interval**: DDS reception times if both
   exist and no wall-clock jump lies between them, else recorder monotonic
   callback times. It is labelled `receipt`, carries a caveat, and carries
   `uncertainty_s` = the min-max spread of the recorder's own receipt delay
   in that bundle. Request to response and hold to stopped are always receipt
   intervals, because the response and the odometry come from the GO2
   computer, whose clock is skewed against the payload.
3. Robot stamps are never compared with payload or recorder clocks. The
   report gives the robot clock offset (median receipt wall time minus
   odometry header stamp) only as a warning.
4. **Stop latency** (hold to physically stopped). "Stopped" uses HELIX's
   definition: the first odometry sample after which speed stays below
   0.03 m/s, with speed = max of pose-differenced speed over a >= 50 ms
   baseline and twist speed. Only samples before the hold is released count.
   The hold is published on the payload and the odometry on the GO2, so the
   two emission times are on different clocks. The report maps both onto the
   recorder wall clock with **median** receipt offsets: the hold's DDS source
   time plus the median payload receipt delay, and the stopped sample's robot
   stamp plus the median (receipt minus robot stamp) offset. Medians are not
   moved by a recorder stall. Uncertainty = one odometry period + p95
   deviation of each offset. This is `stop_latency_s` whenever both offsets
   are measurable. The plain receipt interval is kept as
   `stop_latency_receipt_s`, and its uncertainty adds the largest odometry
   receipt stall and the hold's own excess receipt delay.

   Why: in HELIX's A-F rehearsal the recorder stalled for about 72 ms right at
   the hold. The receipt interval read 0.163 s. The single-host truth
   (odometry emission minus hold emission, one clock) was 0.2403 s, which
   HELIX's stage runner also measured (0.2404 s). The offset-mapped value was
   0.2404 +/- 0.007 s. The fixture `recorder_stall_at_hold` keeps this as a
   regression test.

   Pose differencing uses robot header stamps when they are strictly
   increasing, otherwise receipt times, and says which. **No stop is
   claimed** without odometry after the hold, with a gap larger than
   max(0.25 s, 5 periods) within the deadline, with odometry older than that at
   the hold, or with less than 0.5 s of odometry confirming the stop. **Stop
   distance** is the straight-line odometry displacement between the first
   post-hold sample and the stopped sample, and is omitted if the pose jumps
   (> 1 m/s between samples).
5. `recorder_receipt_delay` in the report (callback wall time minus DDS
   source time on co-hosted topics) is the recorder's own transport plus
   queueing delay when it runs on the payload with HELIX (the Stage E setup).
   Otherwise it also contains the clock offset between the hosts.
6. Wall-clock steps larger than 50 ms against the monotonic clock are
   recorded as `clock_jump` records and flagged.

Two measured examples of why this matters, both from the live DDS rehearsal
on the workstation. (a) With the resource sampler on the executor thread,
nvidia-smi (~25 ms) held message callbacks. A StopMove request and its
response emitted 17.9 ms apart were received 0.2 ms apart. Sampling moved to
its own thread. (b) Opening a bundle wrote the whole pre-window on the executor
thread and delayed the next message by 17 ms, at exactly the moment that
matters. Bundle I/O moved to a writer thread. Worst-case receipt delay is now
5.5 ms at GO2 rates in the benchmark. It is reported per bundle and bounds
every receipt interval. Stalls still happen (72 ms at the hold in the HELIX
rehearsal, while another bundle was finalizing), which is why stop latency
is not taken from receipt times.

## Replay

`robot-blackbox flight replay <bundle>` re-runs the analysis on
`manifest.json` + `records.jsonl`. The analysis is a pure function of those
two files, so the report must be identical. `--retrigger` streams the
recorded messages, graph snapshots and markers through a fresh recorder core
built from the profile text in the manifest, and checks that it fires the
recorded primary trigger. Neither needs ROS.

## Rehearsals

* **Offline, one command:** `robot-blackbox flight rehearse` runs a
  synthetic GO2/HELIX STOP sequence (moving, HELIX fault, STOP_AND_HOLD, hold,
  arbiter zero, StopMove 1003, first-order odometry deceleration, stopped)
  through the real core, writer and analysis, then replays it. Every bundle
  is marked SYNTHETIC. `--scenario all` also runs the fault fixtures:
  normal motion, StopMove, stale odometry, missing sport response, node
  death, dropped, duplicated and out-of-order messages, clock jump, high rate,
  interrupted capture, robot not moving, robot ignoring StopMove, and a
  recorder stall at the hold. Disk pressure and a disk filling up mid-bundle
  are unit tests.
* **Live DDS:** `scripts/flight_dds_rehearsal.py` runs `flight record` as a
  separate process and publishes the same sequence as real `helix_msgs`,
  `unitree_api` and `nav_msgs` messages. It refuses to run unless
  ROS_LOCALHOST_ONLY=1, ROS_DOMAIN_ID is non-zero and no GO2 topic is visible.
* **Beside HELIX's own off-robot rehearsal:** HELIX's `scripts/hw_rehearsal.sh`
  (real HELIX stack, stage runner and sink; `helix_fake_go2` for the robot)
  ran stages A to F with `flight record --profile go2_helix` beside it, on a
  loopback-only Cyclone config. All six HELIX stages passed with the recorder
  running. The Stage E bundle has the full chain, including the same first
  StopMove request id HELIX recorded (0.37 ms after the hold) and response
  code 0. See [HELIX Stage E companion mode](#helix-stage-e-companion-mode).

## Performance (Python backend, development workstation)

Measured 2026-09-18 on an x86_64 workstation (24 logical CPUs, Ubuntu 22.04,
ROS 2 Humble, Cyclone DDS). This is **not the GO2 payload**. The Orin NX is
unmeasured, and preflight's `recorder_load` check measures it on the robot.
Raw numbers: [`benchmarks/flight_python_2026-09-18.json`](benchmarks/flight_python_2026-09-18.json).
Workload: GO2 rates from the field notes (`/lowstate` 500 Hz,
`/sportmodestate` 295 Hz, `/utlidar/robot_odom` 151 Hz, `/lf/*` 50 Hz, HELIX
and sport API topics 10 to 20 Hz), with real message types and default-valued
payloads. Each run is 60 s sustained with a marker-triggered bundle under load.

| Load | Offered | Received / published | Recorder CPU (mean / max, % of one core) | Recorder RSS | Receipt delay p50 / p95 / max | Bundle (25 s window) |
|---|---|---|---|---|---|---|
| 1x GO2 | 1,172 msg/s | 70,302 / 70,302 (0 missing) | 40.6 / 50 | 70 to 119 MB | 0.46 / 0.99 / 5.5 ms | complete, 29,205 records, 10.2 MB |
| 3x GO2 | 3,515 msg/s | 210,470 / 210,908 (438 missing, 0.21 %) | 89.2 / 97 | 73 to 214 MB | 0.38 / 0.82 / 76 ms | complete, 87,493 records, 29.9 MB |

* Before only stored samples were deserialized, 1x ran at 46.4 % mean CPU,
  also with 0 missing.
* At 3x, the losses are all on `/lowstate` and `/sportmodestate`, and
  Cyclone's `message_lost` event reported **0**. Losses are therefore judged
  from publisher sequence counters (HELIX hold and arbiter status), not that
  event.
* Offline core: 162,000 records/s ingest; a full window at 1,200 msg/s
  (30,004 records, 14 MB) finalizes 0.22 s after shutdown.
* Stopping the recorder and finalizing takes 0.07 s at 1x and 0.13 s at 3x.

On this machine the Python backend meets the GO2 workload with no loss. If
preflight on the payload reports the recorder over its CPU limit, drop the
heaviest streams for that run (`--exclude-topic /lowstate --exclude-topic
/sportmodestate`; the `/lf/*` versions stay).

## HELIX Stage E companion mode

HELIX is not changed. BlackBoxRS runs as a fifth process beside the four
HELIX terminals, and starts before any stage.

Environment in the BlackBoxRS terminal (the same as the HELIX terminals, so
the message types resolve and DDS matches):

```bash
source /opt/ros/humble/setup.bash
source <UNITREE_WS>/install/setup.bash     # unitree_api, unitree_go types
source <HELIX>/install/setup.bash          # helix_msgs types (nothing from HELIX runs here)
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
# same CYCLONEDDS_URI as the HELIX terminals
SESSION=~/helix_hw/$(date +%Y%m%d)_motion   # the HELIX session dir
```

Order:

1. **T1** HELIX stack (`ros2 launch helix_bringup helix_closedloop.launch.py ...`).
2. **T2** HELIX GO2 sink (`ros2 run helix_arbiter helix_go2_sport_sink ...`).
3. **T3** BlackBoxRS, preflight and then record:

   ```bash
   robot-blackbox flight preflight --profile go2_helix \
       --experiment helix-stage-E --experiment-repo <HELIX> \
       --evidence-dir $SESSION/blackboxrs
   # GO or GO WITH WARNINGS (read every warning) -> start recording:
   robot-blackbox flight record --profile go2_helix \
       --experiment helix-stage-E --experiment-repo <HELIX> \
       --evidence-dir $SESSION/blackboxrs
   ```

4. **T4** HELIX stage runner (`ros2 run helix_arbiter helix_hw_stage --stage E ...`).
   HELIX's own preflight must still say GO. The recorder does not subscribe
   to `/cmd_vel`, so C6 and `ArbiterStatus.sink_subscribers` are unaffected.
5. After the stage ends, wait at least 15 s (the post window) before Ctrl-C in
   T3. The bundle path is printed. Then read it:
   `robot-blackbox flight show $SESSION/blackboxrs/<session>/inc_*`.

If anything looks wrong during the run, `robot-blackbox flight mark "what you
saw" --evidence-dir $SESSION/blackboxrs` from any shell adds a marker.

## What only the GO2 can answer

* Recorder CPU, RSS and receipt delay on the Orin NX beside the running HELIX
  stack (preflight `recorder_load`, report `recorder_receipt_delay`).
* Which Jetson GPU load node answers on the payload's L4T (report
  `gpu_backend`), and the thermal zone names.
* Real `/utlidar/robot_odom` rate, stamps and frame ids on the payload link,
  and whether its header stamps are monotonic (the speed time base).
* The robot clock offset and its stability over a session.
* Whether Cyclone on the payload supplies DDS source timestamps for every
  profile topic (it did on the workstation).
* Real stop latency and distance, and every other Stage E criterion.
* Whether a GO2 fault chain ever reaches the recorder in an order or timing
  the synthetic fixtures did not cover.

## Known limitations

* `message_lost` events from Cyclone did not report measured losses; only
  topics with a publisher sequence counter (HELIX hold, arbiter status) have
  exact loss counts. Other topics show gaps and rates.
* Nothing ties a `/api/sport/request` publisher to a host. The sport request
  is treated as co-hosted because the HELIX sink runs on the payload. The
  stock robot-side publishers on that topic are recorded but not separated.
* The recorder's BEST_EFFORT subscriptions can themselves miss samples under
  overload (0.21 % at 3x GO2 rates on the workstation). This is deliberate: a
  best-effort reader sends no acknowledgements and cannot slow a HELIX writer.
