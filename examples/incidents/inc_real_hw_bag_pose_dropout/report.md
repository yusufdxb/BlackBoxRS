# Incident `inc_2026-04-06T18-13-40_eb8d6333`

- **Severity**: warning
- **Created**: 2026-07-03 15:30:55.484611Z
- **Window**: 2026-04-06 18:13:40.497738Z → 2026-04-06 18:14:01.496987Z
- **Session**: `real_hw_bag_pose_dropout`
- **Host**: `mewtwo`
- **Tags**: real-hw-bag, go2, pose-dropout, dead-topic
- **Schema**: v1.0

## Summary

Topic /utlidar/robot_pose stopped emitting messages.

## Timeline

| t | subsystem | kind | summary | conf. | evidence |
|---|---|---|---|---|---|
| 2026-04-06 18:13:40.497738Z | ros | raw | frequency on /utlidar/imu: ? Hz | 1.00 | `events.jsonl#L1` |
| 2026-04-06 18:13:40.501725Z | ros | raw | frequency on /utlidar/imu: 250.80432948465727 Hz | 1.00 | `events.jsonl#L2` |
| 2026-04-06 18:13:40.506151Z | ros | raw | frequency on /utlidar/imu: 225.9633722412132 Hz | 1.00 | `events.jsonl#L3` |
| 2026-04-06 18:13:40.510720Z | ros | raw | frequency on /utlidar/imu: 218.87465595640015 Hz | 1.00 | `events.jsonl#L4` |
| 2026-04-06 18:13:40.515959Z | ros | raw | frequency on /utlidar/cloud: ? Hz | 1.00 | `events.jsonl#L5` |
| 2026-04-06 18:13:40.515980Z | ros | raw | frequency on /utlidar/imu: 190.11667079853564 Hz | 1.00 | `events.jsonl#L6` |
| 2026-04-06 18:13:40.519748Z | ros | raw | frequency on /utlidar/imu: 265.34447284809613 Hz | 1.00 | `events.jsonl#L7` |
| 2026-04-06 18:13:40.520974Z | ros | raw | frequency on /utlidar/imu: 815.5755353437813 Hz | 1.00 | `events.jsonl#L8` |
| 2026-04-06 18:13:40.525588Z | ros | raw | frequency on /utlidar/imu: 216.75390650146838 Hz | 1.00 | `events.jsonl#L9` |
| 2026-04-06 18:13:40.529836Z | ros | raw | frequency on /utlidar/imu: 235.40628181663027 Hz | 1.00 | `events.jsonl#L10` |
| 2026-04-06 18:13:40.533710Z | ros | raw | frequency on /utlidar/imu: 258.10328157674263 Hz | 1.00 | `events.jsonl#L11` |
| 2026-04-06 18:13:40.536132Z | ros | raw | frequency on /utlidar/robot_pose: ? Hz | 1.00 | `events.jsonl#L12` |
| 2026-04-06 18:13:40.538393Z | ros | raw | frequency on /utlidar/imu: 213.5627281784274 Hz | 1.00 | `events.jsonl#L13` |
| 2026-04-06 18:13:40.543164Z | ros | raw | frequency on /utlidar/imu: 209.6057713691512 Hz | 1.00 | `events.jsonl#L14` |
| 2026-04-06 18:13:40.547670Z | ros | raw | frequency on /utlidar/imu: 221.92494143400796 Hz | 1.00 | `events.jsonl#L15` |
| 2026-04-06 18:13:40.552237Z | ros | raw | frequency on /utlidar/imu: 218.95128465287246 Hz | 1.00 | `events.jsonl#L16` |
| 2026-04-06 18:13:40.553182Z | ros | raw | frequency on /utlidar/imu: ? Hz | 1.00 | `events.jsonl#L17` |
| 2026-04-06 18:13:40.557292Z | ros | raw | frequency on /utlidar/imu: 243.30663448530916 Hz | 1.00 | `events.jsonl#L18` |
| 2026-04-06 18:13:40.562234Z | ros | raw | frequency on /utlidar/imu: 202.3562360121252 Hz | 1.00 | `events.jsonl#L19` |
| 2026-04-06 18:13:40.566184Z | ros | raw | frequency on /utlidar/imu: 253.182632279064 Hz | 1.00 | `events.jsonl#L20` |
| 2026-04-06 18:13:40.570725Z | ros | raw | frequency on /utlidar/imu: 220.18497299211123 Hz | 1.00 | `events.jsonl#L21` |
| 2026-04-06 18:13:40.575730Z | ros | raw | frequency on /utlidar/imu: 199.81848488832745 Hz | 1.00 | `events.jsonl#L22` |
| 2026-04-06 18:13:40.580520Z | ros | raw | frequency on /utlidar/cloud: 15.489269947647196 Hz | 1.00 | `events.jsonl#L23` |
| 2026-04-06 18:13:40.580538Z | ros | raw | frequency on /utlidar/imu: 207.97946493954973 Hz | 1.00 | `events.jsonl#L24` |
| 2026-04-06 18:13:40.581296Z | ros | raw | frequency on /utlidar/imu: ? Hz | 1.00 | `events.jsonl#L25` |
| 2026-04-06 18:13:40.585377Z | ros | raw | frequency on /utlidar/imu: 245.06998831261228 Hz | 1.00 | `events.jsonl#L26` |
| 2026-04-06 18:13:40.586209Z | ros | raw | frequency on /utlidar/robot_pose: 19.96915205329112 Hz | 1.00 | `events.jsonl#L27` |
| 2026-04-06 18:13:40.589689Z | ros | raw | frequency on /utlidar/imu: 231.8995986049848 Hz | 1.00 | `events.jsonl#L28` |
| 2026-04-06 18:13:40.594186Z | ros | raw | frequency on /utlidar/imu: 222.3927233100933 Hz | 1.00 | `events.jsonl#L29` |
| 2026-04-06 18:13:40.598755Z | ros | raw | frequency on /utlidar/imu: 218.84107892153366 Hz | 1.00 | `events.jsonl#L30` |
| 2026-04-06 18:13:40.603258Z | ros | raw | frequency on /utlidar/imu: 222.09202694065124 Hz | 1.00 | `events.jsonl#L31` |
| 2026-04-06 18:13:40.607875Z | ros | raw | frequency on /utlidar/imu: 216.59217339849582 Hz | 1.00 | `events.jsonl#L32` |
| 2026-04-06 18:13:40.608936Z | ros | raw | frequency on /utlidar/imu: 942.5266122388966 Hz | 1.00 | `events.jsonl#L33` |
| 2026-04-06 18:13:40.613217Z | ros | raw | frequency on /utlidar/imu: 233.58417158906377 Hz | 1.00 | `events.jsonl#L34` |
| 2026-04-06 18:13:40.617725Z | ros | raw | frequency on /utlidar/imu: 221.82633615429884 Hz | 1.00 | `events.jsonl#L35` |
| 2026-04-06 18:13:40.621821Z | ros | raw | frequency on /utlidar/imu: 244.12906224656354 Hz | 1.00 | `events.jsonl#L36` |
| 2026-04-06 18:13:40.626723Z | ros | raw | frequency on /utlidar/imu: 204.00769027389256 Hz | 1.00 | `events.jsonl#L37` |
| 2026-04-06 18:13:40.631267Z | ros | raw | frequency on /utlidar/imu: 220.03797855509862 Hz | 1.00 | `events.jsonl#L38` |
| 2026-04-06 18:13:40.635705Z | ros | raw | frequency on /utlidar/imu: 225.33109023744038 Hz | 1.00 | `events.jsonl#L39` |
| 2026-04-06 18:13:40.636859Z | ros | raw | frequency on /utlidar/imu: 866.6975788803135 Hz | 1.00 | `events.jsonl#L40` |
| 2026-04-06 18:13:40.639624Z | ros | raw | frequency on /utlidar/robot_pose: 18.721482268116564 Hz | 1.00 | `events.jsonl#L41` |
| 2026-04-06 18:13:40.641479Z | ros | raw | frequency on /utlidar/imu: 216.45930586695633 Hz | 1.00 | `events.jsonl#L42` |
| 2026-04-06 18:13:40.644347Z | ros | raw | frequency on /gnss: ? Hz | 1.00 | `events.jsonl#L43` |
| 2026-04-06 18:13:40.645748Z | ros | raw | frequency on /utlidar/cloud: 15.330772062126973 Hz | 1.00 | `events.jsonl#L44` |
| 2026-04-06 18:13:40.645769Z | ros | raw | frequency on /utlidar/imu: 233.07241653210622 Hz | 1.00 | `events.jsonl#L45` |
| 2026-04-06 18:13:40.649731Z | ros | raw | frequency on /utlidar/imu: 252.41383348966164 Hz | 1.00 | `events.jsonl#L46` |
| 2026-04-06 18:13:40.654167Z | ros | raw | frequency on /utlidar/imu: 225.4356826361547 Hz | 1.00 | `events.jsonl#L47` |
| 2026-04-06 18:13:40.659341Z | ros | raw | frequency on /utlidar/imu: 193.2735022994715 Hz | 1.00 | `events.jsonl#L48` |
| 2026-04-06 18:13:40.663822Z | ros | raw | frequency on /utlidar/imu: 223.14958229745437 Hz | 1.00 | `events.jsonl#L49` |
| 2026-04-06 18:13:40.668225Z | ros | raw | frequency on /utlidar/imu: 227.11637829321586 Hz | 1.00 | `events.jsonl#L50` |

_Truncated: 5394 additional rows in `timeline.json`._

## Triggers

### `trg_c56eff95`: dead_topic

- **t**: 2026-04-06 18:13:48.490684Z
- **subject**: `/utlidar/robot_pose`
- **severity**: warning
- **message**: Topic /utlidar/robot_pose has been silent for 3.0s (timeout 3.0s)
- **data**:
  - `detector`: `'dead_topic'`
  - `message`: `'Topic /utlidar/robot_pose has been silent for 3.0s (timeout 3.0s)'`
  - `metric`: `'dead_topic:/utlidar/robot_pose'`
  - `threshold`: `3.0`
  - `topic`: `'/utlidar/robot_pose'`
  - `value`: `3.004237`
- **source**: `events.jsonl#L2231`

## Likely causes

1. **Topic /utlidar/robot_pose stopped emitting messages.** _(confidence 0.98, score 0.98, subsystem `ros`)_
   - **precursor chain**:
     - `2026-04-06 18:13:41.497738Z` (7.0s before trigger, subsystem `ros`, relevance 0.15): topic /gnss appeared on the graph [`snapshots.json#1`]
     - `2026-04-06 18:13:41.497738Z` (7.0s before trigger, subsystem `ros`, relevance 0.15): topic /multiplestate appeared on the graph [`snapshots.json#1`]
     - `2026-04-06 18:13:41.497738Z` (7.0s before trigger, subsystem `ros`, relevance 0.15): topic /utlidar/cloud appeared on the graph [`snapshots.json#1`]
     - `2026-04-06 18:13:41.497738Z` (7.0s before trigger, subsystem `ros`, relevance 0.23): topic /utlidar/robot_pose appeared on the graph [`snapshots.json#1`]
   - **reasoning**:
     - base score for DeadTopicDetector: 0.65.
     - severity bonus (warning): +0.03.
     - precursor bonus: +0.30.
     - precursor: topic /gnss appeared on the graph (7.0s before trigger, subsystem=ros, relevance=0.15)
     - precursor: topic /multiplestate appeared on the graph (7.0s before trigger, subsystem=ros, relevance=0.15)
     - precursor: topic /utlidar/cloud appeared on the graph (7.0s before trigger, subsystem=ros, relevance=0.15)
     - precursor: topic /utlidar/robot_pose appeared on the graph (7.0s before trigger, subsystem=ros, relevance=0.23)
     - precursor contribution capped at 0.30 (raw sum was 0.69).
     - diff bonus: +0.00.
     - no prior bundle on this host; signature diff not informative.
     - final confidence (clamped): 0.98.
   - **evidence**: `events.jsonl#L2231`, `triggers.json#trg_c56eff95`, `snapshots.json#1`, `snapshots.json#1`, `snapshots.json#1`, `snapshots.json#1`

## Signatures

**Config signature**

- hash: `51d82a6394b7e27053ebd56e7e29c7c4a0305dd45c7d300de8db975b96c0be37`
- ros_distro: `humble`  rmw: `None`  domain_id: `None`

**Version signature**

- hash: `daec1ad3e331f79e6002dcb60a6390d06027456af322635109e3db2d38f4ffb9`
- os: `Ubuntu 22.04` (kernel `6.8.0-124-generic`)
- python: `3.10.12`
- blackboxrs: `0.4.1`
- nvidia driver: `570.211.01`

## Config / version diff vs prior session

_No prior bundle found on this host. Diff is the full current signature payload (omitted for brevity; see `signatures/diff.json`)._

## Fingerprint

- **id**: `fpr_a9c8fbf8a4271883`
- **algorithm**: v1
- **detectors**: blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector
- **subsystems**: ros
- **topology**: `61aba542`

_This id will collide on a recurrence with the same surface._

## Recommended preflight rule

Adopt with `robot-blackbox prevention adopt --from-incident <id>`.

```yaml
check: topic_present
params:
  topic: '/utlidar/robot_pose'
  min_publishers: 1
severity_on_fail: block
rationale: |
  Generated from incident: Topic /utlidar/robot_pose stopped emitting messages.
```
