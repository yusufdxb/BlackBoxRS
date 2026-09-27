# BlackBoxRS flight incident `inc_20260910T002643.015420Z_recovery_action_stop`

> **SYNTHETIC DATA.** This bundle was produced from generated traffic. Nothing in it is hardware evidence.

- Status: **complete**
- Trigger: `recovery_action_stop` topic=/helix/recovery_actions fault_id=rate_hz/utlidar_helix_injected
- Also fired: `arbiter_forced_zero`, `helix_hold_asserted`
- Session: `replay_lab_fixture` experiment `replay-lab golden evidence (synthetic)`
- Host: synthetic (synthetic), ROS humble, RMW rmw_cyclonedds_cpp
- BlackBoxRS fixture @ `None`; profile `replay_lab_fixture` sha256 `53e62296f5cd`
- Window: 3.015 s before, 2.992 s after the trigger; 670 messages

## Look here first

- no_move_while_held: INCOMPLETE
- stopmove_acknowledged: INCOMPLETE
- robot clock differs from recorder receipt by -1.732 s (median); robot stamps are never compared with payload or recorder clocks

## Verdicts (explicit criteria only)

| Check | Result | Evidence |
|---|---|---|
| fault, hint, action, hold, arbiter zero, output zero all observed | **PASS** | `{'missing': []}` |
| 0 nonzero /cmd_vel outputs while held | **PASS** | `0` |
| no Move (1008) request sent while held | **INCOMPLETE** | `None` |
| StopMove reached the robot and was answered with code 0 | **INCOMPLETE** | `{'request_id': None, 'response': None, 'request_stage': 'topic_not_in_profile', 'response_stage': 'topic_not_in_profile'}` |
| robot moving (> 0.05 m/s) when the fault was raised | **PASS** | `{'speed_at_fault_mps': 0.148731, 'odom_age_at_fault_s': 0.009317}` |
| robot physically stopped (< 0.03 m/s) within 1.5 s of the hold | **PASS** | `{'status': 'stopped', 'reason': None, 'stop_latency_s': 0.437278, 'uncertainty_s': 0.051622}` |

## HELIX stop chain

| Stage | Status | Times |
|---|---|---|
| fault | observed | rx_mono=5003.012428, dds_src=1789000003.011000, stamp=1789000003.011000 (helix_payload_wall) |
| diagnosis | observed | rx_mono=5003.014997, dds_src=1789000003.012100 |
| recovery_hint | observed | rx_mono=5003.014997, dds_src=1789000003.012100 |
| recovery_action | observed | rx_mono=5003.015420, dds_src=1789000003.012900, stamp=1789000003.012900 (helix_payload_wall) |
| helix_hold | observed | rx_mono=5003.015708, dds_src=1789000003.013400, stamp=1789000003.013400 (helix_payload_wall) |
| arbiter_forced_zero | observed | rx_mono=5003.015614, dds_src=1789000003.014000, stamp=1789000003.014000 (helix_payload_wall) |
| cmd_vel_zero | observed | rx_mono=5003.015643, dds_src=1789000003.014200 |
| sport_stopmove_request | topic_not_in_profile |  |
| sport_response | topic_not_in_profile |  |
| odometry_stopped | observed | rx_mono=5003.452545, dds_src=1789000005.184000, stamp=1789000005.184000 (robot_clock) |

### Intervals

| From | To | Seconds | Basis | Clock |
|---|---|---|---|---|
| fault | helix_hold | 0.0024 | emission | publisher host wall clock (DDS source timestamps); both roles declared co-hosted in the profile |
| helix_hold | cmd_vel_zero | 0.0008 | emission | publisher host wall clock (DDS source timestamps); both roles declared co-hosted in the profile |
| fault | cmd_vel_zero | 0.0032 | emission | publisher host wall clock (DDS source timestamps); both roles declared co-hosted in the profile |
| helix_hold | odometry_stopped | 0.437278 ± 0.051622 | emission (offset-mapped) | emission times on one clock: hold DDS source time and the stopped sample's robot stamp, each mapped to the recorder wall clock by its median receipt offset (robust to recorder stalls; assumes each offset is constant over the window and the two median transport delays are similar) |

`emission` intervals compare publisher stamps on one clock. `receipt` intervals are when the recorder saw each message: they include transport and are not causal latencies.

## Motion

- Input twist before hold: `{'topic': '/nav/cmd_vel', 'linear': {'x': 0.15, 'y': 0.0, 'z': 0.0}, 'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}}`
- Final /cmd_vel: `{'linear': {'x': 0.0, 'y': 0.0, 'z': 0.0}, 'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}}` (source: /cmd_vel)
- Nonzero outputs while held: 0; Move requests while held: None
- StopMove request id: None; response: None
- Odometry: status **stopped**
  - speed_at_fault_mps: 0.148731
  - speed_at_hold_mps: 0.148731
  - stop_latency_s: 0.437278
  - stop_latency_uncertainty_s: 0.051622
  - robot_clock_stop_duration_s: 0.4
  - stop_distance_m: 0.024455
  - robot_clock_offset_s: -1.7315
  - speed_time_base: robot header stamps

## Topics

| Topic | Availability | Count | Hz before/during/after | Max gap s | Lost | Dup | OOO |
|---|---|---|---|---|---|---|---|
| /cmd_vel | subscribed | 121 | 6.0/20.5/20.165 | 0.059558 |  | 0 | 0 |
| /helix/arbiter/status | subscribed | 121 | 6.0/20.5/19.157 | 0.059918 | 0 | 0 | 0 |
| /helix/faults | subscribed | 1 | 0.1/0.0/0.0 | None |  | 0 | 0 |
| /helix/hold | subscribed | 62 | 3.1/10.5/10.083 | 0.102435 | 0 | 0 | 0 |
| /helix/recovery_actions | subscribed | 1 | 0.0/0.5/0.0 | None |  | 0 | 0 |
| /helix/recovery_hints | subscribed | 1 | 0.1/0.0/0.0 | None |  | 0 | 0 |
| /lowstate | subscribed | 121 | 6.1/20.0/20.165 | 0.052665 |  | 0 | 0 |
| /nav/cmd_vel | subscribed | 121 | 6.1/20.0/20.165 | 0.052534 |  | 0 | 0 |
| /teleop/cmd_vel | absent | 0 | None/None/None | None |  |  |  |
| /utlidar/robot_odom | subscribed | 121 | 6.1/20.0/20.165 | 0.052687 |  | 0 | 0 |

## Resources (max before/during/after)

- cpu_percent: 24.99 / 18.25 / 18.6
- mem_percent: 41.0 / 41.0 / 41.0
- recorder_cpu_percent: 4.0 / 4.0 / 4.0
- recorder_rss_mb: 60.0 / 60.0 / 60.0
- gpu_load_percent: 12.0 / 12.0 / 12.0
- gpu_temp_c: 45.0 / 45.0 / 45.0
- thermal max C: {'cpu-thermal:thermal_zone0': 48.0}

## Nodes

- Publishers on profiled topics: <synthetic>
- Disappeared: none
- Appeared: none
