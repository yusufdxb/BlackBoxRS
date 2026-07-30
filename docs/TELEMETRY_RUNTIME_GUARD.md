# Telemetry Runtime Guard Boundary

The telemetry runtime guard is a bounded local ROS 2 launch guard. Its validated
classification is `LOCAL_SEMANTIC_REPEAT_PREVENTION_VALIDATED`.

> In a bounded local ROS 2 evaluation, BlackBoxRS derived a telemetry-health contract from genuine GO2 bag evidence and prevented selected semantic arrival-liveness failures while admitting selected nearby healthy conditions. The hardened guard rejected topic remapping, mismatched declared context labels, trusted-evidence tampering, and unsupported dependent-process escape within its documented Linux process model. It enforces exact topic type and compatible QoS. Thresholds remain session-derived and require multi-session and live-robot validation.

## Telemetry contract

The guard qualifies and monitors one fully qualified topic using:

- exact caller-declared context label;
- exact resolved topic and message type;
- compatible QoS;
- arrival freshness;
- a hard minimum-rate boundary over arrival intervals;
- monotonic header timestamp progress.

The 15.0 Hz minimum is a hard boundary. A measured rate below 15.0 Hz fails.
A measured rate equal to or above 15.0 Hz passes the rate check. A 1e-9 Hz
numeric tolerance prevents binary floating-point representation from moving an
otherwise exact 15.0 Hz schedule below the mathematical boundary.

This contract does not inspect pose values or claim semantic pose freshness.
Old timestamps that arrive live and continue increasing satisfy the header
progress check. Frozen or reset ROS time fails when the header stops exceeding
its previous high-water mark for the configured timeout. Wall-clock changes do
not affect enforcement because arrival timing uses the monotonic clock.
The runtime guard does not scale arrival timing to ROS simulated time or
accelerated replay. Those modes require separate offline replay validation and
are outside this local runtime contract.

The declared context label is compared exactly with the label embedded in the
approved rule. It does not verify the actual robot, host, deployment, DDS graph,
or ROS domain. Compatible traffic in another ROS domain can qualify when the
caller supplies the same label, so independently verified environment identity
is outside this guard's guarantee.

## Multiple publishers

The declared behavior is `aggregate_topic`. Traffic from all compatible
publishers on the topic is combined. The topic can remain healthy when one
publisher is stale or disappears if another compatible publisher sustains the
contract. This does not protect, identify, or attest to a specific producer.

## Process model

The guard supports Linux only. It launches a supervisor as a new session and
process-group leader. The supervisor owns one synchronous foreground command
and its descendants while they remain in that session and process group. It
acts as a child subreaper and applies `SIGTERM`, followed by `SIGKILL` when
needed, before reaping adopted descendants.

The following behaviors are outside the supported command model and are
rejected when observed:

- creation of another process group;
- `setsid()` or another session escape;
- a foreground command exiting while background descendants remain;
- double-fork backgrounding;
- daemonization.

These violations exit with code 125 after cleanup. A dependent launch failure
exits with code 127. Linux cgroup ownership is not used. Cleanup is not claimed
for a process that escapes `/proc` visibility or the process namespace, or
when the supervisor itself is terminated with an uncatchable signal.

## Provenance boundary

Runtime adoption and enforcement separate three properties:

- Traceability: the rule resolves to one finalized incident, trigger, source
  event, topic, and healthy evidence record.
- Integrity: the canonical bag-manifest-v2 file records, metadata, evidence
  content, selected thresholds, rule content, and their recorded hashes must
  match. The bag manifest records each normalized path, role, size, file hash,
  storage relationship, and total size.
- Trusted local approval: the operator supplies the exact trusted rule
  fingerprint and the runtime rule must match it.

The trusted fingerprint is a local pin. There is no signature identity model,
certificate chain, or cryptographic-authenticity claim.

## Timing boundary

Qualification time and supervision time are independent. Each invocation first
replaces any prior result with an atomic `starting` record and unique run ID.
Prelaunch refusals and unexpected failures atomically replace that record;
readers can require the expected run ID. Supervision begins
only after the dependent launches. A zero monitor duration launches and then
immediately stops the dependent. With no monitor duration, supervision
continues until telemetry failure or natural dependent exit. Result files are
written atomically and contain timestamps for guard start, qualification start
and completion, dependent launch, supervision start and end, dependent exit,
enforcement, and completion.
