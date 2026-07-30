# Supervised Live GO2 Telemetry-Health Protocol

Status: prepared only. This protocol must not be executed without supervised
hardware access, an authorized operator, and a reviewed test site.

## Purpose

Evaluate whether the evidence-derived local telemetry contract admits a longer
healthy live interval and contains a harmless dependent after a supervised
`/utlidar/robot_pose` stall. This is not a locomotion, safety-controller, or
field-safety experiment.

## Safety prerequisites

- Robot stationary on a secured test surface with adequate clearance.
- Motion commands disabled or physically prevented for the entire experiment.
- Authorized GO2 operator at the emergency stop.
- Separate test lead, ROS operator, and observer where staffing permits.
- Charged robot and workstation, stable network, and no unrelated robots in the
  selected DDS domain.
- Approved mechanism to isolate only the telemetry source without unsafe motion
  or uncontrolled restart.
- Pre-reviewed topic, exact type, compatible QoS, declared context label, ROS
  domain, RMW, and rule pin. Domain and RMW are separate operator checks, not
  properties attested by the label.
- Output directory outside source repositories and bag directories.
- Only a harmless marker-and-sleep process is allowed as the dependent.
- CPU, memory, and network monitoring active before the guard starts.

Do not guard a locomotion, balance, actuator, navigation, or safety-critical
node in this first live protocol.

## Abort criteria

Abort immediately if the robot moves unexpectedly, the emergency stop is not
ready, another operator or robot enters the area, DDS traffic leaks to another
domain, system load exceeds the approved test bound, the telemetry-isolation
mechanism affects control or safety topics, the publisher cannot be restored,
or any dependent PID survives cleanup. Retain the fail-closed result. Do not
loosen the rule during the run.

## Preflight

1. Record the exact commit and require a clean worktree.
2. Record Python, ROS distribution, RMW, kernel, architecture, domain, network
   interface, and package versions.
3. Obtain the trusted rule fingerprint through a separate operator-controlled
   channel. Do not compute the trusted pin from the runtime rule being checked.
4. Validate the source bag, metadata, incident, evidence, thresholds, and rule:

```bash
python3 scripts/validate_telemetry_thresholds.py /path/to/read-only/bag \
  --rule /path/to/rule.yaml \
  --trusted-rule-fingerprint "$TRUSTED_RULE_FINGERPRINT" \
  --output "$OUT/offline-validation.json"
```

5. Inspect the live endpoint and receive one message:

```bash
ros2 topic info /utlidar/robot_pose --verbose
timeout 10 ros2 topic echo --once \
  /utlidar/robot_pose geometry_msgs/msg/PoseStamped
```

If topic, type, QoS, rate, header behavior, or reviewed context differs, stop
and investigate. Do not modify the adopted rule in place.

## Healthy qualification

Run at least three unique positive trials with a five-second supervision window:

```bash
python3 -m blackboxrs prevention guard \
  --rule "$RULE" \
  --result "$OUT/live-healthy-01.json" \
  --monitor-duration 5 \
  --context-label "$REVIEWED_CONTEXT_LABEL" \
  --trusted-rule-fingerprint "$TRUSTED_RULE_FINGERPRINT" \
  -- python3 -c 'import time; time.sleep(30)'
```

For every trial, verify the exact resolved topic, compatible publisher count,
observed rate, qualification duration, at least five seconds of post-launch
supervision, structured result, dependent cleanup, and no surviving PID.

Then run a longer healthy observation interval approved by the operator. Report
bounded counts such as `3/3 selected live positive trials admitted`; do not
extrapolate a population error rate.

## Controlled stall

1. Start a fresh guard with a unique result file and the same harmless dependent.
2. Verify healthy `/utlidar/robot_pose`.
3. Wait for qualification and dependent launch.
4. Induce the pre-approved telemetry isolation without moving the robot.
5. Where feasible, confirm the publisher process or graph endpoint remains
   structurally present.
6. Confirm the guard reports stale telemetry.
7. Confirm the dependent is terminated or held within the documented process
   model.
8. Confirm no dependent or descendant PID survives.
9. Restore telemetry using the pre-approved recovery step.
10. Terminate the completed guard and start a fresh guard. A failed guard is not
    reused.
11. Verify nearby healthy operation is admitted again.

Do not inject remapping, stop an unreviewed driver, or induce negative cases on
an operational robot DDS domain. Run remapping, QoS, type, context, provenance,
and process-escape attacks with controlled publishers in a separate
localhost-only domain.

## Measurements

Record monotonic and UTC timestamps for:

- guard and qualification start;
- qualification completion;
- dependent launch and supervision start;
- telemetry isolation onset;
- last received message;
- stale detection;
- enforcement start and completion;
- dependent exit;
- telemetry restoration;
- fresh-guard recovery.

Also record:

- healthy false blocks as bounded selected-run counts;
- stale-detection and enforcement latency;
- dependent exit code and application-visible behavior;
- recovery procedure and time;
- guard and publisher CPU percent and resident memory;
- system CPU, memory pressure, packet loss, and DDS discovery;
- network and CPU contention during both healthy and stall phases.

## Acceptance boundary

The live protocol succeeds only if all selected healthy trials qualify, the
approved stall is detected, the harmless dependent is contained, telemetry is
restored, a fresh guard admits healthy operation, and no safety prerequisite or
abort criterion is violated. A successful run would provide live physical
evidence for this one bounded condition. It would not establish field safety,
specific-producer health, payload correctness, or multi-robot generality.
