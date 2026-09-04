# Performance envelope

BlackBoxRS calls itself a low-overhead observability daemon.  This
document backs that claim with reproducible numbers produced by
`scripts/benchmark.py`.

**If you change `EventBus`, `RotatingJsonlWriter`, or the daemon's
event path, re-run this benchmark and update the numbers below.**

## How to reproduce

```bash
./setup.sh
source .venv/bin/activate
python scripts/benchmark.py                     # human-readable
python scripts/benchmark.py --json              # one JSON row per bench
python scripts/benchmark.py --json-output out.json
python scripts/benchmark.py --help              # all knobs
```

The script measures three things in isolation, it does **not** give
you end-to-end numbers under a live ROS 2 graph:

| Benchmark   | What it measures                                            | What it does NOT measure |
|-------------|-------------------------------------------------------------|--------------------------|
| `event_bus` | Single-producer / single-consumer publish throughput + drop rate on the bounded in-process queue | DDS / rclpy delivery, multi-consumer fan-out |
| `writer`    | `RotatingJsonlWriter.write()` per-call latency and sustained events/sec | fsync under disk contention, large-payload tail |
| `pipeline`  | Producer → EventBus → consumer thread → writer pathway      | A real SystemMonitor cadence (1 Hz), rclpy hop |

All three runs use a realistic event payload, the `system.cpu`
record the SystemMonitor actually emits (24 per-CPU values, metadata
envelope, ~560 bytes of JSON on disk).

## CI regression gate

CI enforces a conservative performance floor using:

- `scripts/benchmark.py --json-output artifacts/benchmark-results.json`
- `scripts/check_benchmark_regressions.py`
- `scripts/benchmark_baseline.json`
- `scripts/benchmark_thresholds.json`

The checked-in thresholds are deliberately loose tripwires for GitHub's
shared runners, not promises about any particular robot host. The
workstation numbers below are still the reference narrative.

## What this benchmark deliberately does NOT claim

- **No end-to-end ROS 2 latency.**  `rclpy` is out of scope; the
  recorded overhead is for the in-process event bus and writer only.
- **No fsync-under-contention figures.**  The writer benchmark runs
  against a fresh tmpfs-like directory with no competing I/O.
- **No multi-hour stability run.**  The benchmark caps at ~200k
  events per iteration; long-run memory growth is not measured here.
- **No Jetson / Arm numbers.**  The figures below are from an x86_64
  workstation; a Jetson Orin NX will be materially slower, especially
  for the writer step.

## Reference results (author's workstation)

- Host: the dev workstation (AMD Ryzen 9 7900X3D, 24 logical CPUs, 64 GB RAM)
- OS: Ubuntu 22.04.x, kernel 6.8
- Python: 3.10.12
- Disk: NVMe SSD, ext4, no fsync pressure
- Date: 2026-04-16

### `event_bus`: bus-only throughput

Single producer loop, single consumer thread, no consumer delay.

| metric               | value     |
|----------------------|-----------|
| events               | 200,000   |
| queue depth          | 1,024     |
| publish elapsed      | 0.214 s   |
| publish rate         | 935,000 events/sec |
| delivered to consumer| 40,504    |
| dropped              | 159,496   |
| drop rate            | 79.75 %   |

**Interpretation.**  A 1k-deep queue cannot absorb a ~935k events/sec
burst from a single thread driven by a pure-Python consumer, a
majority of events drop, exactly as the backpressure contract
advertises.  The real SystemMonitor publishes at 1 Hz, so the daemon
sees **zero drops in normal operation**; this benchmark exists to
verify the drop *path* is fast and accounted for, not that producers
always keep up.

### `writer`: RotatingJsonlWriter throughput

50,000 writes of a realistic `system.cpu` event, one call at a time.

| metric               | value        |
|----------------------|--------------|
| events               | 50,000       |
| total elapsed        | 0.159 s      |
| write rate           | 314,000 events/sec |
| bytes written        | 28.0 MB      |
| throughput           | 176 MB/sec   |
| per-write mean       | 3.1 µs       |
| per-write p50        | 2.96 µs      |
| per-write p99        | 4.55 µs      |
| per-write max        | 118 µs       |

**Interpretation.**  p99 of ~5 µs per `write()` call means a 1-Hz
SystemMonitor publish costs the main thread roughly one part in 10⁵
of wall time.  The 118 µs max likely corresponds to a rotation / file
handle operation; it is still well below one ROS control loop at
typical rates.

### `pipeline`: end-to-end

100,000 events through producer → EventBus → consumer thread →
RotatingJsonlWriter.  Deliberately overrun to exercise drop
accounting.

| metric               | value     |
|----------------------|-----------|
| events               | 100,000   |
| queue depth          | 1,024     |
| publish elapsed      | 0.085 s   |
| publish rate         | 1,170,000 events/sec |
| delivered + written  | 1,062     |
| dropped              | 98,938    |
| drop rate            | 98.94 %   |

**Interpretation.**  This is the **worst-case** pattern: a blasting
producer, a single Python consumer, and a disk-backed writer with
fflush() per event.  Even here the bus correctly drops for the
subscriber it cannot feed, the writer does not crash, the drop
counter matches the loss, and producers never block.

## How to spot a regression

If you touch the hot path, compare before/after with `--json-output`:

```bash
python scripts/benchmark.py --json-output before.json
# ... change code ...
python scripts/benchmark.py --json-output after.json
diff before.json after.json
```

Rules of thumb:

- `writer.per_write_us_p50` going above ~5 µs on the reference box
  is a regression.
- `event_bus.publish_rate_eps` dropping below ~500k is a regression.
- `pipeline.delivered` going *down* at identical `events` +
  `queue_depth` means the consumer got slower (or the writer did).

# ROS 2 reliability benchmark

BlackBoxRS also ships a local reliability benchmark through:

```bash
robot-blackbox benchmark list --include-unsupported
robot-blackbox benchmark run --include-unsupported --output-dir artifacts/blackboxrs_benchmark
robot-blackbox benchmark report artifacts/blackboxrs_benchmark/raw_results.json
```

This benchmark is separate from the performance envelope above. It measures
whether supported local ROS 2 failure classes exercise the real BlackBoxRS
pipeline:

- production-shaped `BlackBoxEvent` inputs
- `EventBus` plus `AnomalyEngine`
- built-in detectors
- incident bundle creation
- finalized manifest and checksum validation
- replay agreement fields where the local scenario supports replay semantics
- prevention rule derivation for eligible detectors
- preflight recurrence blocking and healthy-control pass checks

The benchmark deliberately does not add new detectors. Unsupported failure
classes remain visible in the JSON and Markdown report instead of being
silently skipped.

## Reliability scenario taxonomy

| Scenario | Status | Detector or path | Healthy control |
|----------|--------|------------------|-----------------|
| `healthy_topic_publisher` | supported | none expected | yes |
| `dead_topic_dropout` | supported | `DeadTopicDetector` | `healthy_topic_publisher` |
| `healthy_qos_compatible_graph` | supported | none expected | yes |
| `qos_mismatch_reliability` | supported | `QoSMismatchDetector` | `healthy_qos_compatible_graph` |
| `healthy_tf_stream` | supported | none expected | yes |
| `tf_stale_transform` | supported | `TfTopologyDetector` | `healthy_tf_stream` |
| `corrupted_bundle_rejection` | supported artifact check | bundle integrity validator | not applicable |
| `unsupported_prevention_condition` | supported preflight check | fail-closed preflight runner | not applicable |
| `duplicate_or_forbidden_publisher` | unsupported | no current detector | not applicable |

Unsupported examples are not faked. Duplicate publishers, forbidden
publishers, missing required publishers, TF discontinuity, clock freeze,
clock rewind, and process restart do not currently have benchmark support
unless a current detector or validation path can observe them.

## Result fields

Raw results are written to `raw_results.json`; aggregate data is written to
`summary.json`; a concise table is written to `report.md`. Each repetition
records:

- scenario id and repetition
- pass, fail, error, skipped, or unsupported status
- whether a fault was injected
- expected and observed detector
- detection latency and clock mode
- anomaly count and duplicate alert count
- incident bundle path and integrity state
- trigger-to-evidence traceability result
- replay support and agreement
- prevention derivation result
- recurrence-block and healthy-control preflight result
- runtime duration
- environment metadata, including version, host, platform, git commit, and
  dirty-worktree state when available
- unavailable CPU and peak memory overhead fields as `null`

## Metrics and clocks

`detection_latency_sec` is defined as:

```text
anomaly emission time - fault activation time
```

The local synthetic scenarios use `virtual_ros_time`, the same process-global
clock mechanism used by offline replay. Runtime duration uses Python monotonic
wall time around each repetition and is reported separately from detection
latency. Wall-clock runtime is not converted into ROS time.

Latency is unavailable for healthy controls and artifact-only scenarios. CPU
overhead and peak memory overhead are not measured by this reliability
benchmark; those fields remain `null` until a matched baseline/on profiler is
added.

## Repetitions and summaries

Default supported scenarios run five repetitions. Reports include minimum,
median, and maximum latency for scenarios where latency is measurable. With
five repetitions, p95 is intentionally not reported because it would imply
more statistical precision than the sample supports.

Benchmark success requires the relevant stage to pass:

- healthy controls must emit no fault anomaly
- fault scenarios must emit the expected detector and anomaly kind
- wrong-detector-only matches fail
- finalized incident bundles must validate successfully
- corrupted bundles pass only when corruption is rejected
- replay agreement failures fail replay-supported scenarios
- prevention-supported scenarios must derive an enforceable rule
- recurrence blocking must not break the paired healthy control
- unsupported scenarios remain reported as unsupported and are not counted as
  passes

## Evidence boundary

The reliability benchmark supports claims about reproducible local synthetic
and artifact scenarios. It does not support claims about:

- real-world precision or recall
- low false-positive rates outside the executed healthy controls
- production safety
- universal ROS 2 graph coverage
- live robot or Go2 validation
- Jetson or NVIDIA hardware performance
- superiority over rosbag plus scripts without a real baseline

For external claims, cite the generated `report.md`, `summary.json`, exact
command, environment metadata, scenario count, repetition count, and any
unsupported scenarios.

The CI benchmark regression gate currently runs the performance-envelope
script (`scripts/benchmark.py`). The reliability benchmark is a local
evidence-producing command and is not yet a CI gate.
