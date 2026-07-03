# Incident `inc_2026-05-26T05-05-33_3ffbdf62`

- **Severity**: warning
- **Created**: 2026-07-03 02:35:54.117200Z
- **Window**: 2026-05-26 05:05:33.336270Z → 2026-05-26 05:05:43.842968Z
- **Session**: `real_bag_odom_dropout`
- **Host**: `mewtwo`
- **Tags**: real-bag, odom-dropout, dead-topic
- **Schema**: v1.0

## Summary

Topic /source/utlidar/robot_odom stopped emitting messages.

## Timeline

| t | subsystem | kind | summary | conf. | evidence |
|---|---|---|---|---|---|
| 2026-05-26 05:05:33.336270Z | ros | raw | frequency on /source/utlidar/imu: ? Hz | 1.00 | `events.jsonl#L1` |
| 2026-05-26 05:05:33.336287Z | ros | raw | frequency on /source/utlidar/robot_odom: ? Hz | 1.00 | `events.jsonl#L2` |
| 2026-05-26 05:05:33.336305Z | ros | raw | frequency on /source/utlidar/imu: ? Hz | 1.00 | `events.jsonl#L3` |
| 2026-05-26 05:05:33.336306Z | ros | raw | frequency on /source/utlidar/robot_odom: ? Hz | 1.00 | `events.jsonl#L4` |
| 2026-05-26 05:05:33.336311Z | ros | raw | frequency on /source/utlidar/imu: ? Hz | 1.00 | `events.jsonl#L5` |
| 2026-05-26 05:05:33.336313Z | ros | raw | frequency on /source/utlidar/robot_odom: ? Hz | 1.00 | `events.jsonl#L6` |
| 2026-05-26 05:05:33.336323Z | ros | raw | frequency on /source/utlidar/imu: ? Hz | 1.00 | `events.jsonl#L7` |
| 2026-05-26 05:05:33.336324Z | ros | raw | frequency on /source/utlidar/robot_odom: ? Hz | 1.00 | `events.jsonl#L8` |
| 2026-05-26 05:05:33.336329Z | ros | raw | frequency on /source/utlidar/imu: ? Hz | 1.00 | `events.jsonl#L9` |
| 2026-05-26 05:05:33.336330Z | ros | raw | frequency on /source/utlidar/robot_odom: ? Hz | 1.00 | `events.jsonl#L10` |
| 2026-05-26 05:05:33.348315Z | ros | raw | frequency on /source/utlidar/imu: 83.42757868805793 Hz | 1.00 | `events.jsonl#L11` |
| 2026-05-26 05:05:33.348324Z | ros | raw | frequency on /source/utlidar/robot_odom: 83.37999833907044 Hz | 1.00 | `events.jsonl#L12` |
| 2026-05-26 05:05:33.368612Z | ros | raw | frequency on /source/utlidar/imu: 49.26888667343128 Hz | 1.00 | `events.jsonl#L13` |
| 2026-05-26 05:05:33.368622Z | ros | raw | frequency on /source/utlidar/robot_odom: 49.264323540488995 Hz | 1.00 | `events.jsonl#L14` |
| 2026-05-26 05:05:33.389976Z | ros | raw | frequency on /source/utlidar/imu: 46.808745877144176 Hz | 1.00 | `events.jsonl#L15` |
| 2026-05-26 05:05:33.389982Z | ros | raw | frequency on /source/utlidar/robot_odom: 46.815539144836464 Hz | 1.00 | `events.jsonl#L16` |
| 2026-05-26 05:05:33.414763Z | ros | raw | frequency on /source/utlidar/imu: 40.34284641857592 Hz | 1.00 | `events.jsonl#L17` |
| 2026-05-26 05:05:33.414772Z | ros | raw | frequency on /source/utlidar/robot_odom: 40.34011232784958 Hz | 1.00 | `events.jsonl#L18` |
| 2026-05-26 05:05:33.438079Z | ros | raw | frequency on /source/utlidar/imu: 42.89027988738385 Hz | 1.00 | `events.jsonl#L19` |
| 2026-05-26 05:05:33.438092Z | ros | raw | frequency on /source/utlidar/robot_odom: 42.88084493789052 Hz | 1.00 | `events.jsonl#L20` |
| 2026-05-26 05:05:33.458432Z | ros | raw | frequency on /source/utlidar/imu: 49.131442083978904 Hz | 1.00 | `events.jsonl#L21` |
| 2026-05-26 05:05:33.458448Z | ros | raw | frequency on /source/utlidar/robot_odom: 49.125721810371275 Hz | 1.00 | `events.jsonl#L22` |
| 2026-05-26 05:05:33.481946Z | ros | raw | frequency on /source/utlidar/imu: 42.52755370836653 Hz | 1.00 | `events.jsonl#L23` |
| 2026-05-26 05:05:33.481957Z | ros | raw | frequency on /source/utlidar/robot_odom: 42.536309099788355 Hz | 1.00 | `events.jsonl#L24` |
| 2026-05-26 05:05:33.506271Z | ros | raw | frequency on /source/utlidar/imu: 41.11135503848434 Hz | 1.00 | `events.jsonl#L25` |
| 2026-05-26 05:05:33.506278Z | ros | raw | frequency on /source/utlidar/robot_odom: 41.117930748358674 Hz | 1.00 | `events.jsonl#L26` |
| 2026-05-26 05:05:33.526591Z | ros | raw | frequency on /source/utlidar/imu: 49.21089105914386 Hz | 1.00 | `events.jsonl#L27` |
| 2026-05-26 05:05:33.526599Z | ros | raw | frequency on /source/utlidar/robot_odom: 49.20917170384051 Hz | 1.00 | `events.jsonl#L28` |
| 2026-05-26 05:05:33.550956Z | ros | raw | frequency on /source/utlidar/imu: 41.04374236842916 Hz | 1.00 | `events.jsonl#L29` |
| 2026-05-26 05:05:33.550963Z | ros | raw | frequency on /source/utlidar/robot_odom: 41.04456783353637 Hz | 1.00 | `events.jsonl#L30` |
| 2026-05-26 05:05:33.575922Z | ros | raw | frequency on /source/utlidar/imu: 40.05453505056204 Hz | 1.00 | `events.jsonl#L31` |
| 2026-05-26 05:05:33.575931Z | ros | raw | frequency on /source/utlidar/robot_odom: 40.05161531769222 Hz | 1.00 | `events.jsonl#L32` |
| 2026-05-26 05:05:33.599467Z | ros | raw | frequency on /source/utlidar/imu: 42.47145291909906 Hz | 1.00 | `events.jsonl#L33` |
| 2026-05-26 05:05:33.599479Z | ros | raw | frequency on /source/utlidar/robot_odom: 42.46555523267578 Hz | 1.00 | `events.jsonl#L34` |
| 2026-05-26 05:05:33.623340Z | ros | raw | frequency on /source/utlidar/imu: 41.88813622424026 Hz | 1.00 | `events.jsonl#L35` |
| 2026-05-26 05:05:33.623364Z | ros | raw | frequency on /source/utlidar/robot_odom: 41.86751210012967 Hz | 1.00 | `events.jsonl#L36` |
| 2026-05-26 05:05:33.643373Z | ros | raw | frequency on /source/utlidar/imu: 49.91633024723958 Hz | 1.00 | `events.jsonl#L37` |
| 2026-05-26 05:05:33.643389Z | ros | raw | frequency on /source/utlidar/robot_odom: 49.93759299004035 Hz | 1.00 | `events.jsonl#L38` |
| 2026-05-26 05:05:33.663818Z | ros | raw | frequency on /source/utlidar/imu: 48.913087606372514 Hz | 1.00 | `events.jsonl#L39` |
| 2026-05-26 05:05:33.663831Z | ros | raw | frequency on /source/utlidar/robot_odom: 48.91973963349136 Hz | 1.00 | `events.jsonl#L40` |
| 2026-05-26 05:05:33.689760Z | ros | raw | frequency on /source/utlidar/imu: 38.546613803326544 Hz | 1.00 | `events.jsonl#L41` |
| 2026-05-26 05:05:33.689773Z | ros | raw | frequency on /source/utlidar/robot_odom: 38.54673267100795 Hz | 1.00 | `events.jsonl#L42` |
| 2026-05-26 05:05:33.709973Z | ros | raw | frequency on /source/utlidar/imu: 49.47356661967007 Hz | 1.00 | `events.jsonl#L43` |
| 2026-05-26 05:05:33.709984Z | ros | raw | frequency on /source/utlidar/robot_odom: 49.478021714320434 Hz | 1.00 | `events.jsonl#L44` |
| 2026-05-26 05:05:33.730748Z | ros | raw | frequency on /source/utlidar/imu: 48.13450629423651 Hz | 1.00 | `events.jsonl#L45` |
| 2026-05-26 05:05:33.730754Z | ros | raw | frequency on /source/utlidar/robot_odom: 48.14709051705979 Hz | 1.00 | `events.jsonl#L46` |
| 2026-05-26 05:05:33.754987Z | ros | raw | frequency on /source/utlidar/imu: 41.256933743427254 Hz | 1.00 | `events.jsonl#L47` |
| 2026-05-26 05:05:33.754994Z | ros | raw | frequency on /source/utlidar/robot_odom: 41.253342552080284 Hz | 1.00 | `events.jsonl#L48` |
| 2026-05-26 05:05:33.775882Z | ros | raw | frequency on /source/utlidar/imu: 47.858414899626304 Hz | 1.00 | `events.jsonl#L49` |
| 2026-05-26 05:05:33.775892Z | ros | raw | frequency on /source/utlidar/robot_odom: 47.85207125537086 Hz | 1.00 | `events.jsonl#L50` |

_Truncated: 657 additional rows in `timeline.json`._

## Triggers

### `trg_21d6c9a8`: dead_topic

- **t**: 2026-05-26 05:05:41.332176Z
- **subject**: `/source/utlidar/robot_odom`
- **severity**: warning
- **message**: Topic /source/utlidar/robot_odom has been silent for 3.0s (timeout 3.0s)
- **data**:
  - `detector`: `'dead_topic'`
  - `message`: `'Topic /source/utlidar/robot_odom has been silent for 3.0s (timeout 3.0s)'`
  - `metric`: `'dead_topic:/source/utlidar/robot_odom'`
  - `threshold`: `3.0`
  - `topic`: `'/source/utlidar/robot_odom'`
  - `value`: `3.004076`
- **source**: `events.jsonl#L637`

## Likely causes

1. **Topic /source/utlidar/robot_odom stopped emitting messages.** _(confidence 0.98, score 0.98, subsystem `ros`)_
   - **precursor chain**:
     - `2026-05-26 05:05:34.336270Z` (7.0s before trigger, subsystem `ros`, relevance 0.23): topic /source/utlidar/robot_odom appeared on the graph [`snapshots.json#1`]
     - `2026-05-26 05:05:36.336270Z` (5.0s before trigger, subsystem `ros`, relevance 0.17): topic /source/cmd_vel appeared on the graph [`snapshots.json#3`]
   - **reasoning**:
     - base score for DeadTopicDetector: 0.65.
     - severity bonus (warning): +0.03.
     - precursor bonus: +0.30.
     - precursor: topic /source/utlidar/robot_odom appeared on the graph (7.0s before trigger, subsystem=ros, relevance=0.23)
     - precursor: topic /source/cmd_vel appeared on the graph (5.0s before trigger, subsystem=ros, relevance=0.17)
     - precursor contribution capped at 0.30 (raw sum was 0.40).
     - diff bonus: +0.00.
     - no prior bundle on this host; signature diff not informative.
     - final confidence (clamped): 0.98.
   - **evidence**: `events.jsonl#L637`, `triggers.json#trg_21d6c9a8`, `snapshots.json#1`, `snapshots.json#3`

## Signatures

**Config signature**

- hash: `51d82a6394b7e27053ebd56e7e29c7c4a0305dd45c7d300de8db975b96c0be37`
- ros_distro: `humble`  rmw: `None`  domain_id: `None`

**Version signature**

- hash: `f26ee280c64df72ccefea42d3afb9076a4460a78b7e2bbad319896386bc80f1d`
- os: `Ubuntu 22.04` (kernel `6.8.0-124-generic`)
- python: `3.10.12`
- blackboxrs: `0.4.1`
- nvidia driver: `570.211.01`

## Config / version diff vs prior session

_No prior bundle found on this host. Diff is the full current signature payload (omitted for brevity; see `signatures/diff.json`)._

## Fingerprint

- **id**: `fpr_f2e0c1de554d1078`
- **algorithm**: v1
- **detectors**: blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector
- **subsystems**: ros
- **topology**: `c68caae3`

_This id will collide on a recurrence with the same surface._

## Recommended preflight rule

Adopt with `robot-blackbox prevention adopt --from-incident <id>`.

```yaml
check: topic_present
params:
  topic: '/source/utlidar/robot_odom'
  min_publishers: 1
severity_on_fail: block
rationale: |
  Generated from incident: Topic /source/utlidar/robot_odom stopped emitting messages.
```
