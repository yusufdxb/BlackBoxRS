# Incident `inc_2026-05-17T14-22-00_7c674770`

- **Severity**: error
- **Created**: 2026-05-17 23:45:05.491247Z
- **Window**: 2026-05-17 14:22:00.000000Z → 2026-05-17 14:22:15.000000Z
- **Session**: `demo_observer_dead_topic`
- **Observer**: `mewtwo`
- **Observed**: `go2-edu-01`
- **Tags**: demo, observer, dead-topic
- **Schema**: v1.0

## Summary

Topic /scan stopped emitting messages.

## Timeline

| t | subsystem | kind | summary | conf. | evidence |
|---|---|---|---|---|---|
| 2026-05-17 14:22:00.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L1` |
| 2026-05-17 14:22:01.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L2` |
| 2026-05-17 14:22:01.000000Z | ros | raw | ros.qos | 1.00 | `events.jsonl#L7` |
| 2026-05-17 14:22:01.000000Z | ros | derived | publisher count changed on /scan: 0 -> 1 | 0.85 | `snapshots.json#1` |
| 2026-05-17 14:22:02.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L3` |
| 2026-05-17 14:22:03.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L4` |
| 2026-05-17 14:22:04.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L5` |
| 2026-05-17 14:22:05.000000Z | ros | raw | frequency on /scan: 10.0 Hz | 1.00 | `events.jsonl#L6` |
| 2026-05-17 14:22:06.000000Z | ros | raw | ros.qos | 1.00 | `events.jsonl#L8` |
| 2026-05-17 14:22:06.000000Z | ros | derived | publisher count changed on /scan: 1 -> 0 | 0.85 | `snapshots.json#6` |
| 2026-05-17 14:22:11.000000Z | anomaly | trigger | anomaly: anomaly.dead_topic: Topic /scan silent for 5.0 s (timeout exceeded). | 1.00 | `triggers.json#trg_1b0c2dbc` |

## Triggers

### `trg_1b0c2dbc`: DeadTopicDetector

- **t**: 2026-05-17 14:22:11.000000Z
- **subject**: `/scan`
- **severity**: error
- **message**: Topic /scan silent for 5.0 s (timeout exceeded).
- **data**:
  - `detector`: `'DeadTopicDetector'`
  - `message`: `'Topic /scan silent for 5.0 s (timeout exceeded).'`
  - `metric`: `'/scan'`
  - `threshold`: `5.0`
  - `topic`: `'/scan'`
  - `value`: `0.0`
- **source**: `events.jsonl#L9`

## Likely causes

1. **Topic /scan stopped emitting messages.** _(confidence 1.00, score 1.02, subsystem `ros`)_
   - **precursor chain**:
     - `2026-05-17 14:22:01.000000Z` (10.0s before trigger, subsystem `ros`, relevance 0.20): publisher count changed on /scan: 0 -> 1 [`snapshots.json#1`]
     - `2026-05-17 14:22:06.000000Z` (5.0s before trigger, subsystem `ros`, relevance 0.25): publisher count changed on /scan: 1 -> 0 [`snapshots.json#6`]
   - **reasoning**:
     - base score for DeadTopicDetector: 0.65.
     - severity bonus (error): +0.07.
     - precursor bonus: +0.30.
     - precursor: publisher count changed on /scan: 0 -> 1 (10.0s before trigger, subsystem=ros, relevance=0.20)
     - precursor: publisher count changed on /scan: 1 -> 0 (5.0s before trigger, subsystem=ros, relevance=0.25)
     - precursor contribution capped at 0.30 (raw sum was 0.45).
     - diff bonus: +0.00.
     - no prior bundle on this host; signature diff not informative.
     - final confidence (clamped): 1.00.
   - **evidence**: `events.jsonl#L9`, `triggers.json#trg_1b0c2dbc`, `snapshots.json#1`, `snapshots.json#6`

## Signatures

**Config signature**

- hash: `51d82a6394b7e27053ebd56e7e29c7c4a0305dd45c7d300de8db975b96c0be37`
- ros_distro: `humble`  rmw: `None`  domain_id: `None`

**Version signature**

- hash: `7e3844a710c94cba6949762ee83cd3ba46f2d3052917da304d2420178082e00c`
- os: `Ubuntu 22.04` (kernel `6.8.0-111-generic`)
- python: `3.10.12`
- blackboxrs: `0.4.0.dev0`
- nvidia driver: `570.211.01`

## Config / version diff vs prior session

_No prior bundle found on this host. Diff is the full current signature payload (omitted for brevity; see `signatures/diff.json`)._

## Fingerprint

- **id**: `fpr_1033d956d9f634a7`
- **algorithm**: v1
- **detectors**: blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector
- **subsystems**: ros
- **topology**: `926cd3ef`

_This id will collide on a recurrence with the same surface._

## Recommended preflight rule

Adopt with `robot-blackbox prevention adopt --from-incident <id>`.

```yaml
check: topic_present
params:
  topic: '/scan'
  min_publishers: 1
severity_on_fail: block
rationale: |
  Generated from incident: Topic /scan stopped emitting messages.
```
