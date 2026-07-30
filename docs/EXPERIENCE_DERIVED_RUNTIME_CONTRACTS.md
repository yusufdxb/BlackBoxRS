# From Robot Incidents to Runtime Contracts

## Experience-Derived Recurrence Prevention for ROS 2 Telemetry Failures

## 1. Motivation

A ROS 2 graph can report that a publisher exists while the publisher delivers
no new telemetry. Structural launch checks therefore miss a useful class of
semantic liveness failures. BlackBoxRS tests a narrower operational question:
can evidence from a recorded incident and a separate healthy session be turned
into a traceable runtime contract that holds a dependent process until telemetry
qualifies and stops it when the selected liveness condition fails?

The central distinction is:

> Structural topic presence does not guarantee semantic telemetry liveness.

## 2. Failure model

The selected failure is publisher-present silence on
`/utlidar/robot_pose`. Related selected failures include sustained under-rate
traffic, frozen header progression, wrong topic type or incompatible QoS,
mismatched declared context label, and topic substitution through remapping.
The implemented property is
aggregate topic arrival liveness plus monotonic header progress. It does not
inspect pose values, infer a specific producer, or establish payload-semantic
freshness.

## 3. BlackBoxRS architecture

The evaluated path has five boundaries:

```text
recorded runtime failure
    -> finalized incident and source event
    -> healthy-session characterization
    -> derived and locally approved telemetry rule
    -> ROS 2 qualification and dependent-process supervision
```

The incident bundle captures the trigger and event reference. Characterization
reads a rosbag2 source without altering it. Derivation checks that incident
topic, detector, event, evidence, exact type, compatible QoS, declared context
label, and selected thresholds agree. Runtime verification subscribes to the
exact contract topic and starts a Linux supervisor only after qualification.

## 4. Incident-to-contract derivation

The rule schema is predefined. BlackBoxRS does not learn a general policy or
temporal logic. It selects bounded numeric parameters from healthy evidence and
binds them to a dead-topic incident:

- startup grace: 0.5 seconds;
- stale and header-progress timeout: 0.15 seconds;
- hard minimum rate: 15.0 Hz;
- rate window: 2.0 seconds;
- exact topic and `PoseStamped` type, compatible QoS against the
  reliable/volatile keep-last subscription, and a reviewed declared context
  label.

The failure-to-prevention path is:

```text
runtime failure -> incident evidence -> derived contract
                -> trusted adoption -> runtime enforcement
```

## 5. Provenance and trust model

Traceability, integrity, and trusted local approval are separate:

- Traceability resolves the rule to one incident, trigger, event, bag,
  evidence record, topic, and thresholds.
- Integrity hashes the incident manifest and event plus a canonical v2 bag
  manifest containing normalized paths, roles, sizes, per-file hashes, storage
  relationships, and total size. It also hashes the evidence, selected
  thresholds, and rule.
- Trusted local approval requires the exact rule fingerprint supplied by the
  operator.

The fingerprint is a local pin. There is no signer identity, certificate chain,
institutional key, non-repudiation, or cryptographic-authenticity claim. The
model resembles a narrow provenance graph, but it does not implement the full
[W3C PROV-O recommendation](https://www.w3.org/TR/prov-o/).

## 6. Runtime guard and process supervision

The guard suppresses global ROS remapping, resolves the contract topic, checks
the caller-declared context label, exact type, compatible QoS, freshness,
interval-based rate, and header progress, and then launches a foreground
dependent. The label is not robot, host, deployment, DDS graph, or ROS-domain
attestation. Matching labels in an independently different actual environment
are outside the guarantee.

On Linux, a new session and process group contain the supervisor and supported
foreground descendants. The supervisor uses parent-death monitoring, subreaping,
`SIGTERM`, and then `SIGKILL` where necessary. New process groups, `setsid()`,
foreground exit with background descendants, double-forking, and daemonization
are rejected when observed. Cgroup ownership is not used, and universal
descendant cleanup is not claimed.

## 7. Genuine GO2 data

One read-only physical-GO2 rosbag2 session provided 94,325 messages across six
topics over 329.601840622 seconds. `/utlidar/robot_pose` contained 6,177
`geometry_msgs/msg/PoseStamped` messages. Its mean rate was
18.746780976988813 Hz, median rate 18.756079021048965 Hz, and maximum healthy
gap 0.070847572 seconds. No negative, frozen, or nonprogressing header deltas
were observed.

The bag is about 682 MB and is not distributed. Its combined identity SHA-256
is `f6c15669dd5a1630578d4ab7931b24e93d22251b5bd08ba5b8cded1709e350c5`.
The public repository contains only a curated summary and a generated fixture
that is explicitly labeled as non-genuine.

## 8. Experimental design

The evaluation has four layers:

1. Exact offline characterization of the genuine session.
2. A controlled dropout after five seconds of genuine pose records.
3. A local process-level comparison among no rule, topic presence, and the
   hardened telemetry-health rule.
4. Adversarial tests for ROS substitution, provenance tampering, rate and clock
   boundaries, and process escape.

Results are bounded selected-case counts, not deployment error rates or
population estimates.

## 9. Rule comparison

| Condition | No rule | Topic presence | Telemetry health |
|---|---|---|---|
| Healthy 18.75 Hz traffic | Proceeds | Proceeds | Qualifies and remains supervised |
| Publisher exists but is silent | Proceeds | Proceeds incorrectly | Blocks or terminates |
| Sustained 10 Hz traffic | Proceeds | Proceeds | Blocks below 15.0 Hz |
| Frozen header progress | Proceeds | Proceeds | Blocks |

The structural rule answers whether a publisher endpoint exists. The semantic
liveness rule answers whether aggregate compatible traffic continues to satisfy
the selected arrival and header contract.

## 10. Adversarial evaluation

The focused suites exercised missing and modified evidence, bag and metadata
manifests, incident and trigger substitutions, event and topic substitutions,
threshold and QoS changes, declared-label changes, recomputed untrusted fingerprints,
untrusted identities, and missing cross-references. They also exercised global
remapping, wrong namespace, type, QoS and context, aggregate multi-publisher
behavior, guard signals, descendant trees, session and daemon escape, result
write failure, monitor timing, rate phase sensitivity, ROS time resets, and
header progression.

The 15.0 Hz policy is a hard boundary with a numerical tolerance for binary
representation of the exact schedule. Across selected deterministic trials:

- 20/20 14.9 Hz constant cases blocked;
- 20/20 14.9 Hz jitter cases blocked;
- 20/20 15.0 Hz constant cases admitted;
- 20/20 15.1 Hz constant cases admitted;
- 20/20 15.1 Hz jitter cases admitted.

## 11. Results

The genuine healthy trace produced no guard failure. The controlled dropout was
detected 0.151 seconds after the final retained message. In the local process
matrix, 6/6 selected valid conditions were admitted and 7/7 selected invalid
conditions were blocked. Three additional 15.1 Hz multi-phase jitter cases and
the selected aggregate healthy-plus-stale publisher condition passed.

The clean baseline regression completed with 653 passed tests and four
optional-MCAP skips. Focused suites passed 16 ROS adversarial, 25 provenance,
18 process-supervision, and 25 rate and clock boundary tests. A post-integration
reproduction is required whenever the source commit changes.

## 12. Limitations

- One genuine session, one robot platform, and one `PoseStamped` contract.
- Local ROS 2 Humble with Fast DDS, not multiple distributions or RMWs.
- Host-monotonic arrival timing, not ROS simulated-time or accelerated-replay
  timing.
- Aggregate-topic health, not specific-producer identity.
- Arrival liveness and header progress, not pose correctness or payload
  semantics.
- Session-derived thresholds with no multi-session or live-robot calibration.
- No live physical prevention claim.
- Bounded Linux foreground process model without cgroups.
- Local fingerprint approval without institutional signer identity.
- No formal proof, hard real-time guarantee, autonomous repair, or safe
  controller handoff.

## 13. Threats to validity

The genuine session may not span temperature, load, radio, compute, and motion
conditions seen in deployment. Synthetic publishers approximate arrival timing
but not the entire GO2 data path. DDS discovery and host scheduling can affect
process-level timing. Fault injection reproduces the selected telemetry symptom,
not necessarily the full causal incident. Overlapping rate windows are
descriptive and not independent population samples. The implementation and
evaluation were developed together, so independent replication is still needed.

## 14. Closest work and differentiated contribution

| Area | Primary source | Relationship |
|---|---|---|
| Lifecycle and launch | ROS 2 [managed nodes](https://design.ros2.org/articles/node_lifecycle.html) and [launch design](https://design.ros2.org/articles/roslaunch.html) | Standardizes component states and process/event orchestration. BlackBoxRS qualifies external telemetry before starting an arbitrary foreground dependent. |
| Diagnostics and QoS | ROS 2 [`diagnostic_updater`](https://docs.ros.org/en/ros2_packages/humble/api/diagnostic_updater/index.html) and [QoS policies](https://docs.ros.org/en/humble/Concepts/Intermediate/About-Quality-of-Service-Settings.html) | Reports frequency and communication health. BlackBoxRS is an external, incident-linked launch and supervision boundary. |
| Runtime verification | [ROSRV](https://doi.org/10.1007/978-3-319-11164-3_20), [ROSMonitoring](https://doi.org/10.1007/978-3-030-63486-5_40), and [ROSMonitoring 2.0](https://doi.org/10.4204/EPTCS.411.3) | Evaluates user-specified formal properties. BlackBoxRS uses one predefined telemetry contract and does not claim general formal monitor semantics. |
| Requirements-derived assurance | [Monitoring ROS2](https://doi.org/10.4204/EPTCS.371.15) and [SOTER](https://doi.org/10.1007/978-3-030-60508-7_10) | Derives monitors from requirements or safety rules and can switch controllers. BlackBoxRS derives parameters and traceability from recorded evidence and contains a dependent. |
| Diagnosis and repair | [Model-based diagnosis and repair](https://doi.org/10.1109/ICRA.2013.6630618) and [MROS](https://doi.org/10.1080/01691864.2022.2039761) | Diagnoses and adapts configurations. BlackBoxRS neither finds root cause nor repairs the producer. |
| Incident recording and tracing | [Modular robot black-box recorder](https://doi.org/10.1109/LRA.2022.3193633) and [`ros2_tracing`](https://doi.org/10.1109/LRA.2022.3174346) | Preserves evidence for investigation. BlackBoxRS uses evidence to instantiate a narrow preventive guard. |
| Trace specification mining | [Daikon](https://doi.org/10.1016/j.scico.2007.01.015) and [Perracotta](https://doi.org/10.1145/1134285.1134325) | Infers candidate invariants or temporal properties from traces. BlackBoxRS selects parameters inside a fixed rule schema and does not infer a general specification. |
| Provenance-aware verification | [Provenance-aware runtime verification](https://doi.org/10.1002/cpe.4263) and [Black Block Recorder](https://doi.org/10.1109/LRA.2019.2928780) | Attaches provenance to monitored events or protects robot logs. BlackBoxRS binds incident, event, bag, context, thresholds, and rule to a local fingerprint pin. |

No reviewed source in this focused comparison was found to combine the exact
incident-linked evidence derivation, local trusted adoption, ROS graph and
context enforcement, launch gating, and bounded Linux process supervision used
here. This is a gap statement, not a priority or “first” claim. The defensible
contribution is the composition and its adversarial evaluation, not watchdog
monitoring, hashing, bag replay, or process supervision individually.

Claims must stay narrower than self-healing, formal safety, root-cause diagnosis,
general policy synthesis, cryptographic identity, or universal recurrence
prevention. The best immediate venue fit is a future Robotics Software
Engineering workshop or ROSCon technical presentation. TAROS is plausible after
live-robot validation. FMAS would require formal monitor semantics. RA-L or a
full journal paper would require multiple sessions, comparative baselines,
broader contracts, and independent evaluation.

## Future live-robot validation

The next study should keep the robot stationary, guard only a harmless
dependent, measure a longer healthy interval, and induce a supervised telemetry
stall without unsafe motion. It should report startup qualification, false
blocks as bounded run counts, stale and enforcement latency, dependent outcome,
recovery, resource overhead, network contention, and CPU contention. The
detailed safety protocol is in `docs/LIVE_GO2_TELEMETRY_PROTOCOL.md`.
