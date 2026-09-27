# C++ runtime benchmarks

Every number here was measured on one x86_64 workstation (24 logical CPUs,
Linux 6.8, GCC 11.4, Release build, ROS 2 Humble with its default RMW). None
of it was measured on the GO2 or on the Orin NX: those numbers come from gate
H1 in [CPP_HARDWARE_VALIDATION.md](CPP_HARDWARE_VALIDATION.md), with the same
commands. Nothing here asserts a threshold. The machine-readable budget is the
profile's `preflight` block (`blackboxrs/flight/profiles/go2.yaml`:
`max_recorder_cpu_percent: 50.0`, `max_recorder_rss_mb: 500`), which
preflight check P9 enforces on the live recorder and gate H1 applies on the
robot.

Each raw file in [benchmarks/](benchmarks/) records the commit, build type and
machine it was measured on.

| File | What | Commit |
|---|---|---|
| `cpp_ros_recorder_workstation.json` | recorder process on real ROS 2 traffic, 1x to 10x | `c135c1c` |
| `cpp_core_workstation.json` | ROS-free pipeline, queue, serialization, replay | `20cef29` |
| `cpp_core_workstation_debug.json` | the same on a Debug build | `7e100e5` |
| `cpp_soak_workstation.json` | 30 min at 5x, RSS every second | `20cef29` |
| `cpp_soak_workstation_50db5a1.json` | an earlier 30 min soak, kept for the analysis below | `50db5a1` |

## 1. Recorder on real ROS 2 traffic

```bash
source install/setup.bash    # plus the unitree_go message workspace
python3 scripts/cpp/ros_benchmark.py --scales 1,2,5,10 --seconds 30 --out x.json
```

The test tool `load_publisher` publishes default-valued messages of the real
types (`unitree_go/LowState` at 500 Hz, `unitree_go/SportModeState` at 295 Hz,
`nav_msgs/Odometry` at 151 Hz, the rates measured on the GO2, times the
scale) on a loopback-only private domain. The recorder runs the `go2_helix`
profile in continuous mode with fsync, and decodes every message at run time
through rosidl introspection. CPU and RSS are for the recorder process (all
of its threads) from `/proc`; the publisher is a separate process and is not
counted.

| Scale | Offered | Received | Dropped | Recorder CPU (one core) | Peak RSS | Exit | Evidence |
|---|---|---|---|---|---|---|---|
| 1x | 946 msg/s | 28,382 / 28,382 | 0 | 2.8 % | 32.8 MB | 0 | verified |
| 2x | 1,892 msg/s | 56,762 / 56,762 | 0 | 4.5 % | 32.8 MB | 0 | verified |
| 5x | 4,730 msg/s | 141,902 / 141,902 | 0 | 10.0 % | 32.8 MB | 0 | verified |
| 10x | 9,460 msg/s | 283,802 / 283,802 | 0 | 17.7 % | 32.9 MB | 0 | verified |

The previous committed run of the same script (`50db5a1`) gave 2.8, 4.5, 10.0
and 17.2 %, 32.6 to 32.8 MB. Before that commit, peak RSS grew with the rate
(51 MB at 1x, 206 MB at 10x) because ring entries kept their decoded JSON.

For comparison, the Python flight recorder used 40.6 % of one core at 1x on
this workstation ([FLIGHT_RECORDER.md](FLIGHT_RECORDER.md)).

## 2. Recording pipeline without ROS

```bash
blackboxrs benchmark --scale 1,2,5,10,20 --seconds 30 --json x.json
```

The same `Recorder`, `FlightCore` and `EvidenceWriter` (fsync, SHA-256, CRC-32C
chunks), fed by an in-process load generator with the GO2 + HELIX topic mix
and a JSON stand-in for the CDR decoder. "Push" is the time a producer spends
handing one message to the recorder, the whole cost on a subscription
callback.

| Scale | Offered | Dropped | Peak queue depth | Push p50 / p99 | Pipeline thread CPU | Writer thread CPU | Writer max lag | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1x | 1,036 msg/s | 0 | 2 | 0.58 / 1.28 µs | 0.5 % | 0.3 % | 13.8 ms | verified |
| 2x | 2,072 msg/s | 0 | 3 | 0.51 / 1.01 µs | 0.8 % | 0.6 % | 11.8 ms | verified |
| 5x | 5,180 msg/s | 0 | 4 | 0.47 / 0.81 µs | 1.8 % | 1.4 % | 13.8 ms | verified |
| 10x | 10,360 msg/s | 0 | 4 | 0.47 / 0.74 µs | 3.2 % | 2.4 % | 13.7 ms | verified |
| 20x | 20,720 msg/s | 0 | 6 | 0.38 / 0.88 µs | 5.4 % | 4.2 % | 13.7 ms | verified |

`stop()` (drain, close, finalize, join) took 15 to 16 ms at every scale. The
difference to section 1 is the ROS side: DDS reception, rclcpp dispatch and
the introspection decode of real messages.

Other sections of the same run:

* **Queue.** `BoundedQueue` (mutex and condition variable) under contention:
  6.7 M pushes/s with 1 producer, 5.2 M with 2, 7.0 M with 4. The 20x load
  is 20,720 pushes/s, so the queue is not the limit and no lock-free queue
  was written.
* **Serialization.** 2.17 M GO2-shaped records/s (426 bytes mean, 926 MB/s)
  through the Python-compatible JSON writer.
* **Replay.** The four golden cases replay 1,851x to 2,099x faster than real
  time (2.9 to 3.2 ms per replay).
* The whole benchmark process peaked at 473 MB RSS; that is the queue and
  serialization micro-benchmarks' own buffers, not the recorder (its rows
  above ran at 26 to 43 MB process RSS).

## 3. Debug build

`cpp_core_workstation_debug.json` (`-O0`, assertions on) is kept for scale,
not as a result: the pipeline thread needs about 4x the CPU of Release (1.9 %
vs 0.5 % at 1x, 8.0 % vs 1.8 % at 5x), replay is about 9x slower (236x vs
2,099x real time on `nominal_motion`), serialization 14x slower. Only Release
builds are deployed; H0 checks the build type.

## 4. Memory over 30 minutes

```bash
blackboxrs benchmark --only none --soak 1800 --soak-scale 5 --json x.json
```

The recorder takes the GO2 + HELIX load at 5x (5,180 msg/s) for 30 minutes
in one continuous bundle; RSS is sampled every second.

| Commit | Messages | Dropped | Peak queue depth | RSS after 3 min warmup | Slope | Evidence |
|---|---|---|---|---|---|---|
| `20cef29` | 9,324,002 | 0 | 57 | 27.43 to 27.95 MB | +1.0 MB/h | verified (3.4 GB) |
| `50db5a1` | 9,324,002 | 0 | 36 | 27.58 to 28.96 MB | +2.9 MB/h | verified (3.4 GB) |

RSS is not perfectly flat, and the growth was traced rather than rounded
away:

* At `50db5a1` the soak harness itself kept one JSON object per sample in
  the process it measured. Storing samples as preallocated structs
  (`20cef29`) cut the slope from 2.9 to 1.0 MB/h.
* What is left grows with the records written, not with time: about 0.07
  bytes per record, +0.6 MB over 9.3 M records. That is consistent with the
  one piece of state that must grow for a continuous bundle, its
  `integrity.json` chunk table (48 bytes per 1,024 records, about 9,100
  entries here, plus vector growth slack). ASSUMPTION: the remainder is that
  table; it was not isolated with an allocation profiler. At the 1x GO2 rate
  it would be about 0.16 MB per hour of continuous recording, and it is
  released when the bundle closes.
* Queue depth stayed at 0 to 3 in the 1 s samples (57 at the peak between
  samples), so the pipeline never fell behind.

The `20cef29` soak ran while other work used the same workstation (the ROS
benchmark and the HELIX rehearsal); it still dropped nothing.

## 5. Not measured here

* Anything on the Orin NX: CPU, RSS, fsync latency on the payload disk,
  thermal behaviour. Gate H1.
* Whether Cyclone DDS on the robot supplies source timestamps (the recorder
  handles their absence; the clock monitor then has less to judge).
* Latency under real-time scheduling. The runtime makes no real-time claim.
* A soak longer than 30 minutes, or on real robot traffic.
* An aarch64 soak. CI builds and tests on aarch64 and checks that every
  golden replay is byte-identical to x86_64 (30/30 at `1140d01`), but it
  runs no benchmark there.
