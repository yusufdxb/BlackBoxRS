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
