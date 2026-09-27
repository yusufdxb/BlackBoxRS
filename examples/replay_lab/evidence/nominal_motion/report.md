# BlackBoxRS flight incident `inc_20260910T002643.000000Z_manual_marker`

> **SYNTHETIC DATA.** This bundle was produced from generated traffic. Nothing in it is hardware evidence.

- Status: **complete**
- Trigger: `manual_marker` note=operator marker (synthetic)
- Session: `replay_lab_fixture` experiment `replay-lab golden evidence (synthetic)`
- Host: synthetic (synthetic), ROS humble, RMW rmw_cyclonedds_cpp
- BlackBoxRS fixture @ `None`; profile `replay_lab_fixture` sha256 `53e62296f5cd`
- Window: 3.0 s before, 3.0 s after the trigger; 660 messages

## Look here first

- robot clock differs from recorder receipt by -1.732 s (median); robot stamps are never compared with payload or recorder clocks

## HELIX stop chain

| Stage | Status | Times |
|---|---|---|
| fault | not_observed |  |
| diagnosis | not_observed |  |
| recovery_hint | not_observed |  |
| recovery_action | not_observed |  |
| helix_hold | not_observed |  |
| arbiter_forced_zero | not_observed |  |
| cmd_vel_zero | not_observed |  |
| sport_stopmove_request | not_observed |  |
| sport_response | not_observed |  |
| odometry_stopped | not_observed |  |

## Motion

- Input twist before hold: `{'topic': '/nav/cmd_vel', 'linear': {'x': 0.15, 'y': 0.0, 'z': 0.0}, 'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}}`
- Final /cmd_vel: `{'linear': {'x': 0.15, 'y': 0.0, 'z': 0.0}, 'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}}` (source: /cmd_vel)
- Nonzero outputs while held: None; Move requests while held: None
- StopMove request id: None; response: None
- Odometry: status **not_applicable** (no HELIX hold observed to time a stop from)
  - robot_clock_offset_s: -1.7315
  - speed_time_base: robot header stamps

## Topics

| Topic | Availability | Count | Hz before/during/after | Max gap s | Lost | Dup | OOO |
|---|---|---|---|---|---|---|---|
| /cmd_vel | subscribed | 120 | 6.0/20.0/20.0 | 0.052866 |  | 0 | 0 |
| /helix/arbiter/status | subscribed | 120 | 6.0/20.0/20.0 | 0.05271 | 0 | 0 | 0 |
| /helix/faults | absent | 0 | None/None/None | None |  |  |  |
| /helix/hold | subscribed | 60 | 3.0/10.0/10.0 | 0.102353 | 0 | 0 | 0 |
| /helix/recovery_actions | absent | 0 | None/None/None | None |  |  |  |
| /helix/recovery_hints | absent | 0 | None/None/None | None |  |  |  |
| /lowstate | subscribed | 120 | 6.0/20.0/20.0 | 0.052665 |  | 0 | 0 |
| /nav/cmd_vel | subscribed | 120 | 6.0/20.0/20.0 | 0.052534 |  | 0 | 0 |
| /teleop/cmd_vel | absent | 0 | None/None/None | None |  |  |  |
| /utlidar/robot_odom | subscribed | 120 | 6.0/20.0/20.0 | 0.052687 |  | 0 | 0 |

## Resources (max before/during/after)

- cpu_percent: 24.99 / 20.71 / 18.6
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
