# Incident `inc_2026-05-07T14-22-00_04ca9c43`

- **Severity**: error
- **Created**: 2026-05-17 23:44:32.674887Z
- **Window**: 2026-05-07 14:22:00.000000Z → 2026-05-07 14:22:20.000000Z
- **Session**: `demo_tf_break`
- **Host**: `mewtwo`
- **Tags**: demo, tf-break
- **Schema**: v1.0

## Summary

Topic /tf_static stopped emitting messages.

## Timeline

| t | subsystem | kind | summary | conf. | evidence |
|---|---|---|---|---|---|
| 2026-05-07 14:22:00.000000Z | ros | raw | frequency on /tf_static: 1.0 Hz | 1.00 | `events.jsonl#L1` |
| 2026-05-07 14:22:00.500000Z | system | raw | system.cpu: 32.5 % | 1.00 | `events.jsonl#L6` |
| 2026-05-07 14:22:01.000000Z | ros | raw | frequency on /tf_static: 1.0 Hz | 1.00 | `events.jsonl#L2` |
| 2026-05-07 14:22:01.500000Z | system | raw | system.cpu: 32.5 % | 1.00 | `events.jsonl#L7` |
| 2026-05-07 14:22:02.000000Z | ros | raw | frequency on /tf_static: 1.0 Hz | 1.00 | `events.jsonl#L3` |
| 2026-05-07 14:22:02.000000Z | ros | raw | ros.qos | 1.00 | `events.jsonl#L16` |
| 2026-05-07 14:22:02.000000Z | ros | derived | publisher count changed on /tf_static: 0 -> 1 | 0.85 | `snapshots.json#2` |
| 2026-05-07 14:22:02.500000Z | system | raw | system.cpu: 32.5 % | 1.00 | `events.jsonl#L8` |
| 2026-05-07 14:22:03.000000Z | ros | raw | frequency on /tf_static: 1.0 Hz | 1.00 | `events.jsonl#L4` |
| 2026-05-07 14:22:03.500000Z | system | raw | system.cpu: 32.5 % | 1.00 | `events.jsonl#L9` |
| 2026-05-07 14:22:04.000000Z | ros | raw | frequency on /tf_static: 1.0 Hz | 1.00 | `events.jsonl#L5` |
| 2026-05-07 14:22:04.500000Z | system | raw | system.cpu: 32.5 % | 1.00 | `events.jsonl#L10` |
| 2026-05-07 14:22:05.250000Z | system | raw | system.cpu: 95.0 % | 1.00 | `events.jsonl#L11` |
| 2026-05-07 14:22:05.250000Z | system | derived | cpu_percent excursion: peak 95.0%, duration 4.0s above 90.0% | 0.85 | `events.jsonl#L11` |
| 2026-05-07 14:22:06.000000Z | ros | raw | ros.qos | 1.00 | `events.jsonl#L17` |
| 2026-05-07 14:22:06.000000Z | ros | derived | publisher count changed on /tf_static: 1 -> 0 | 0.85 | `snapshots.json#6` |
| 2026-05-07 14:22:06.250000Z | system | raw | system.cpu: 95.0 % | 1.00 | `events.jsonl#L12` |
| 2026-05-07 14:22:07.250000Z | system | raw | system.cpu: 95.0 % | 1.00 | `events.jsonl#L13` |
| 2026-05-07 14:22:08.000000Z | anomaly | trigger | anomaly: anomaly.dead_topic: Topic /tf_static silent for 5.0 s (timeout exceeded). | 1.00 | `triggers.json#trg_df6aa081` |
| 2026-05-07 14:22:08.250000Z | system | raw | system.cpu: 95.0 % | 1.00 | `events.jsonl#L14` |
| 2026-05-07 14:22:09.250000Z | system | raw | system.cpu: 95.0 % | 1.00 | `events.jsonl#L15` |
| 2026-05-07 14:22:11.000000Z | system | raw | system.cpu: 30.0 % | 1.00 | `events.jsonl#L19` |
| 2026-05-07 14:22:12.000000Z | system | raw | system.cpu: 30.0 % | 1.00 | `events.jsonl#L20` |

## Triggers

### `trg_df6aa081`: DeadTopicDetector

- **t**: 2026-05-07 14:22:08.000000Z
- **subject**: `/tf_static`
- **severity**: error
- **message**: Topic /tf_static silent for 5.0 s (timeout exceeded).
- **data**:
  - `detector`: `'DeadTopicDetector'`
  - `message`: `'Topic /tf_static silent for 5.0 s (timeout exceeded).'`
  - `metric`: `'/tf_static'`
  - `threshold`: `5.0`
  - `topic`: `'/tf_static'`
  - `value`: `0.0`
- **source**: `events.jsonl#L18`

## Likely causes

1. **Topic /tf_static stopped emitting messages.** _(confidence 1.00, score 1.02, subsystem `ros`)_
   - **precursor chain**:
     - `2026-05-07 14:22:02.000000Z` (6.0s before trigger, subsystem `ros`, relevance 0.24): publisher count changed on /tf_static: 0 -> 1 [`snapshots.json#2`]
     - `2026-05-07 14:22:05.250000Z` (2.8s before trigger, subsystem `system`, relevance 0.06): cpu_percent excursion: peak 95.0%, duration 4.0s above 90.0% [`events.jsonl#L11`]
     - `2026-05-07 14:22:06.000000Z` (2.0s before trigger, subsystem `ros`, relevance 0.28): publisher count changed on /tf_static: 1 -> 0 [`snapshots.json#6`]
   - **reasoning**:
     - base score for DeadTopicDetector: 0.65.
     - severity bonus (error): +0.07.
     - precursor bonus: +0.30.
     - precursor: publisher count changed on /tf_static: 0 -> 1 (6.0s before trigger, subsystem=ros, relevance=0.24)
     - precursor: cpu_percent excursion: peak 95.0%, duration 4.0s above 90.0% (2.8s before trigger, subsystem=system, relevance=0.06)
     - precursor: publisher count changed on /tf_static: 1 -> 0 (2.0s before trigger, subsystem=ros, relevance=0.28)
     - precursor contribution capped at 0.30 (raw sum was 0.58).
     - diff bonus: +0.00.
     - no prior bundle on this host; signature diff not informative.
     - final confidence (clamped): 1.00.
   - **evidence**: `events.jsonl#L18`, `triggers.json#trg_df6aa081`, `snapshots.json#2`, `events.jsonl#L11`, `snapshots.json#6`

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

- **id**: `fpr_5aa6d4e95e5af33a`
- **algorithm**: v1
- **detectors**: blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector
- **subsystems**: ros
- **topology**: `6ce4faa1`

_This id will collide on a recurrence with the same surface._

## Recommended preflight rule

Adopt with `robot-blackbox prevention adopt --from-incident <id>`.

```yaml
check: topic_present
params:
  topic: '/tf_static'
  min_publishers: 1
severity_on_fail: block
rationale: |
  Generated from incident: Topic /tf_static stopped emitting messages.
```
