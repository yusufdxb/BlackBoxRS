# Native capture benchmarks

Numbers become project claims only when the corresponding JSON artifact, commit,
build configuration, ROS/RMW configuration, and workload are retained. The
measured results below are backed by retained artifacts in
`docs/benchmarks/native_capture/`. Every other section of this document
describes tooling, not results.

## Measured results (first retained run)

Single run per scenario, 30 s each, recorded 2026-08-09. These are a first
retained measurement, **not** a published percentile: the fair-comparison rule
below requires five or more fresh launches before a percentile becomes a project
claim. This 2026-08-09 artifact set contains no rosbag2 or Python comparison.
Treat its latency figures as indicative of order of magnitude only.

Host: Linux 6.8.0-136-generic, x86_64, 24 logical CPUs. ROS 2 Humble.
Workspace built with `colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release`
using the default system GCC. Source commit `55e50f4` with a dirty tree
(`git_dirty: true` in every artifact), so these results precede the commit that
carries them.

| Scenario | Workload | Sent | Serialized committed | Dropped | Peak queue util | Max RSS (MB) | Max CPU (%) | Ingest p50/p99 (us) | Artifact valid |
|---|---|---|---|---|---|---|---|---|---|
| A | 1 topic, 100 Hz, 64 B | 2,999 | 2,999 | 0 | 0.000 | 45.6 | 2 | 32.6 / 84.9 | yes |
| B | 10 topics, 1 kHz agg, 1 KiB | 29,999 | 29,999 | 0 | 0.009 | 47.4 | 5 | 15.7 / 48.9 | yes |
| C | 10 topics, 10 kHz agg, 256 B | 299,999 | 299,999 | 0 | 0.181 | 49.0 | 28 | 11.3 / 189.5 | yes |
| E | Scenario B with 10x burst | 56,999 | 56,999 | 0 | 0.033 | 49.0 | 15 | 12.6 / 66.9 | yes |
| G | Scenario B, 5 ms writer delay | 29,999 | 0 | 24,595 | 0.892 | 47.8 | 3 | null | **no** |

CPU percent is recorder process CPU over elapsed time, where 100 percent is one
fully used logical core. All five runs used `best_effort` QoS at depth 10.

### What the clean runs show

Scenarios A, B, C, and E each reconcile exactly: publisher sent count equals
recorder serialized committed count equals serialized retained count, with zero
drops and zero storage errors. Each ended in `STOPPED_CLEAN` with a 214 ms
shutdown drain, seven closed segments, and startup latency between 175 and
186 ms. The supervisor marked all four `valid: true` with an empty error list.

Resident memory stayed within 45.6 to 49.0 MB across a hundredfold change in
message rate, against a configured 128 MiB ceiling and a 33.6 MB startup
estimate of capture-owned allocation. This is the boundedness claim behaving as
designed, on this workload and duration.

### What Scenario G shows, and why it is retained as invalid

Scenario G injects a 5 ms writer delay to model slow storage. The recorder did
not block the ROS callback and did not grow without bound: RSS peaked at
47.8 MB and queue utilization peaked at 0.892 without exceeding capacity. It
shed 24,595 messages (25.6 MB) under `kLowPriorityShed`, and every drop is
attributed per topic with count, bytes, first and last sequence, and first and
last monotonic timestamp.

It also failed in ways the artifact states plainly. The supervisor recorded
`valid: false` with seven errors, including that capture never provided an
authoritative terminal status, that no closed MCAP segment was found, that the
shutdown deadline expired and the process was terminated, that received does not
reconcile with committed plus dropped, and that publisher sent does not
reconcile with serialized committed plus recorder-accounted drops, leaving DDS
or pre-callback loss unexplained. Final state was `SHEDDING`; the 10 s drain
deadline expired; storage retained one 4.3 MB `.partial` segment and zero closed
segments.

The operational consequence is direct: under sustained storage stall at this
rate, the recorder protects the robot and its own memory ceiling, but the
session yields no clean segment and the evidence is incomplete by an amount that
is partly unquantified. That is a real limitation of the current design, not a
harness artifact, and it is why the promotion gate is not satisfied. It is
retained here precisely because a black box that hid this result would be
worthless.

### Two-hour endurance run

Artifact: `docs/benchmarks/native_capture/long_run_2h.json`. Ten topics, 1 kHz
aggregate, 1 KiB payloads, 15 s sampling, 600 s warmup excluded from the trend
fit. Observed span 7,185 s across 480 samples. The supervisor reported
`valid: true` with an empty error list.

| Measure | Value |
|---|---|
| Events received / committed / durable | 7,228,863 / 7,228,863 / 7,228,863 |
| Dropped | 0 (0 bytes) |
| Storage errors | 0 |
| RSS min / p50 / max | 45.3 / 49.8 / 51.2 MB |
| RSS slope after warmup | 1.61 MB/hour (threshold 5.0) |
| Peak queue utilization | 0.024 |
| Segments closed / evicted | 256 / 1,184 |
| Bytes written / evicted / retained | 8.78 GB / 7.09 GB / 1.69 GB |
| Shutdown | `STOPPED_CLEAN`, exit 0, 264 ms drain |

Retention held: 8.78 GB was written and 7.09 GB evicted to keep 1.69 GB against
the configured cap, so the on-disk footprint stayed bounded while the process
footprint grew 1.61 MB/hour. That slope is a screen against a user-selected
threshold, not a proof of zero leak; two hours is the observed span and nothing
is claimed beyond it. `serialized_committed` is null in this artifact because
rolling eviction makes the session-wide serialized population incomplete, which
is the intended reporting behavior rather than a missing measurement.

The sustained write rate was 1.22 MB/s. On flash-based robot storage that is
roughly 100 GB/day of write volume to maintain continuous history, which is a
device-endurance consideration this design has not yet addressed.

### Known provenance gaps in these artifacts

- `build.compiler`, `build.build_type`, and `build.rmw_implementation` are
  `null` in all five artifacts. The supervisor reads them from the `CXX`,
  `CMAKE_BUILD_TYPE`, and `RMW_IMPLEMENTATION` environment variables, which were
  not set in the benchmark shell. The build configuration stated above is
  therefore recorded by hand and is not independently attested by the artifact.
  The supervisor should query the actual build rather than trusting the
  environment.
- Every run has `git_dirty: true`.
- One run per scenario. No repeat launches, no alternated run order, no warmup
  discipline, and no comparison against rosbag2 or the Python backend.
- `write` and `trigger_to_flush` percentiles are null in every artifact; that
  instrumentation does not yet export samples.

### CI bag fidelity gate

CI replays the committed `examples/bags/go2_sim_odom_imu.mcap` fixture through
the installed standalone C++ recorder under both Fast DDS and Cyclone DDS. The
gate compares all 909 selected CDR payloads in per-topic order, checks the three
ROS schema type names, verifies finalized MCAP sidecar sizes and checksums, and
requires clean shutdown with zero recorder drops, storage errors, RMW losses,
graph faults, subscription faults, QoS faults, and callback faults. Each run
uploads a `blackboxrs.native_bag_gate.v1` JSON artifact containing per-topic
counts and payload-sequence digests. Capture files are created in a temporary
directory and removed after verification.

Reproduce with:

```bash
python scripts/native_capture_benchmark.py \
  --backend native \
  --scenario C --duration-sec 30 \
  --output artifacts/native_capture/bench_scenario_C.json
```

The same supervisor can run `--backend rosbag2` with the identical publisher
command, exact workload topics, generated QoS overrides, and matched MCAP chunk
and rotation settings. Unsupported rosbag2 counters remain `null` and the
artifact describes durability, cache, resource-boundary, and loss-accounting
differences. This single-run capability has smoke coverage only. Use the repeat
matrix below for a publishable backend comparison.

### Matched repeat matrix

`native_capture_benchmark_matrix.py` launches the existing single-run
supervisor in paired, counterbalanced order. Five repetitions means ten fresh
recorder launches: native then rosbag2 for the first pair, rosbag2 then native
for the second, and so on. Every child receives the same workload arguments.

A public run requires a clean Git tree, a sourced install containing both C++
benchmark packages, an explicit RMW implementation, an isolated ROS domain, and
a CMake cache that records the compiler and build type. The cache is hashed into
the summary, together with the source-tree and installed-executable hashes. The
installed executables must not be older than their source trees. Run this from
the repository after committing the code under test:

```bash
source /opt/ros/humble/setup.bash
CXX=/usr/bin/c++ colcon build \
  --build-base build/impressive_rate \
  --install-base install/impressive_rate \
  --packages-select blackbox_capture_cpp blackbox_capture_bench \
  --cmake-args -DCMAKE_BUILD_TYPE=RelWithDebInfo
source install/impressive_rate/setup.bash
python3 scripts/native_capture_benchmark_matrix.py \
  --output-dir artifacts/native_capture_matrix_5x \
  --publish-dir docs/benchmarks/native_capture/matrix_5x \
  --matrix-id native-rosbag2-5x \
  --repetitions 5 \
  --install-prefix install/impressive_rate \
  --cmake-cache build/impressive_rate/blackbox_capture_cpp/CMakeCache.txt \
  --ros-distro humble \
  --rmw-implementation rmw_fastrtps_cpp \
  --ros-domain-id 87 \
  --scenario custom \
  --topics 10 \
  --rate 5000 \
  --payload-bytes 256 \
  --duration-sec 30 \
  --discovery-warmup-sec 2
```

The ignored output directory contains one JSON document per launch,
`summary.json`, and a `SHA256SUMS` manifest that covers every JSON artifact. The
optional publish directory must be a new, non-ignored path inside the repository.
It is populated atomically only after every child and final clean-tree check
passes. Its checksum manifest covers the child artifacts, summary, and copies of
both JSON schemas, making the directory ready for review and commit without
weakening the clean-start measurement guard. The matrix fails on a
nonzero child exit, schema error, child validity failure, workload drift, Git
provenance drift, or build provenance drift. One invalid child stops the matrix
and leaves an invalid summary instead of aggregating a partial result.

The summary reports median and p95 across runs only for matched recorder-process
CPU, recorder-process RSS, publisher calls, and retained serialized workload
counts and bytes. If one child reports a null or unsupported value, that metric
stays null for the backend aggregate. It does not compare storage totals, queue
behavior, reasoned drops, ingest latency, or startup and shutdown timings. In
particular, it never claims durability equivalence: native fsync semantics and
rosbag2 close/finalization are different contracts.

#### Retained 5x comparison

The retained `native-rosbag2-5x` run used Fast DDS, 10 best-effort topics at an
aggregate 5,000 messages per second, 256-byte payloads, and 30 seconds per
launch. All ten child runs were valid. Both backends retained the same 149,999
workload messages and 40,799,728 serialized payload bytes in every run.

| Recorder-process metric | Native median / p95 | rosbag2 median / p95 |
|---|---:|---:|
| CPU, percent of one logical core | 9.93 / 10.06 | 8.06 / 9.32 |
| Peak RSS, MiB | 47.75 / 47.83 | 60.69 / 60.73 |
| Retained workload messages | 149,999 / 149,999 | 149,999 / 149,999 |
| Retained serialized bytes | 40,799,728 / 40,799,728 | 40,799,728 / 40,799,728 |

For this workload, native capture used 21.3 percent less median peak RSS while
using 23.1 percent more median recorder-process CPU. The result supports a lower
memory claim, not a universal speed claim. Review the
[matrix summary](benchmarks/native_capture/matrix_5x/summary.json), all ten child
artifacts, copied schemas, and the checksum manifest for the full evidence and
comparison boundaries.

`--exploratory` bypasses the clean-tree and sourced-build publication gates for
local smoke work. Its summary always records `publication.eligible: false`, and
it cannot use `--publish-dir`.

## Load generator

Build and source the ROS workspace, then run:

```bash
ros2 run blackbox_capture_bench publisher \
  --topics 10 \
  --rate 1000 \
  --payload-bytes 1024 \
  --duration 60 \
  --qos best_effort \
  --result-json artifacts/publisher.json
```

`--rate` is aggregate messages per second. `--rate-per-topic` is available when
the per-topic rate is the intended independent variable. Supplying both is an
error. Payload bytes describe `std_msgs/msg/ByteMultiArray.data`; DDS and ROS
serialization overhead is additional.

The first bytes of each payload contain a fixed marker, publisher sequence,
steady timestamp, and topic index when the configured payload is large enough.
The native recorder treats this as opaque serialized data. After capture, the
supervisor can compare the embedded publisher steady timestamp with MCAP's
recorder callback steady timestamp. This instruments ingest latency without
deserializing on the capture hot path. The publisher writes the number of calls
it actually completed, not a rate multiplied by duration. Scheduler catch-up
truncation and publishes skipped during churn are reported.

Deterministic traffic controls include:

- `--burst-every-sec`, `--burst-duration-ms`, and `--burst-multiplier`;
- `--churn-every-sec` and `--churn-down-ms`;
- reliable or best-effort QoS with an explicit depth;
- stable topic names under a configurable prefix and a run ID.

## Named scenarios

| Scenario | Workload | Purpose |
|---|---|---|
| A | 1 topic, 100 Hz, 64 B | Low-rate control-like baseline |
| B | 10 topics, 100 Hz each, 1 KiB | Multi-topic telemetry |
| C | 10 topics, 1 kHz each, 256 B | High callback rate |
| D | 1 topic, 30 Hz, 1 MiB | Camera-like payload pressure, only where memory permits |
| E | Scenario B base with a deterministic 10x burst | Backpressure transition |
| F | 10 topics with deterministic publisher churn | Discovery and subscription lifetime |
| G | Scenario B with a 5 ms injected writer delay | Slow-storage behavior |

Custom sweeps should cover aggregate rates 100, 500, 1,000, 5,000, 10,000, and
20,000 messages per second with 64 B, 256 B, 1 KiB, and 16 KiB payloads. A runner
may omit infeasible combinations, but the artifact must say which scenario was
not run. It must not record a failed launch as zero throughput.

## Benchmark supervisor

With the workspace sourced:

```bash
python scripts/native_capture_benchmark.py \
  --scenario B \
  --duration-sec 60 \
  --output artifacts/native_capture_benchmark.json
```

The supervisor creates a recorder parameter file, starts the recorder, waits for
its `READY` marker, samples the recorder process and status topic, runs the
publisher, requests process-group SIGINT shutdown, and reads the latest status
plus finalized session and sidecar artifacts. Use
`--recorder-params` with its matching `--capture-output-dir` to supply an
operational configuration. A custom command may use `{params}`, `{output}`, or
`{result}` placeholders. Externally supplied recorder parameters are marked as
not independently workload-matched because the supervisor does not interpret
arbitrary ROS parameter files. A comparison needs a separate configuration
review before publication.

`--slow-writer-ms` and `--fail-after-bytes` are deterministic failure-injection
parameters recorded in both scenario and provenance. A fail-after experiment
must also use `--expect-storage-fault`; the supervisor then requires a non-clean
terminal state and a nonzero storage-error count. Recorder callback-received
accounting must still reconcile. Publisher-to-callback DDS delivery equality is
not a validity condition after an intentional fail-stop because the writer may
fault before discovery or workload start; the artifact records that limitation
as a warning. Ordinary runs require `STOPPED_CLEAN`. Injection options do not
override an external parameter file.

Results conform to `scripts/native_capture_benchmark.schema.json` and use schema
version `blackboxrs.capture_benchmark.v1`. They include:

- git, ROS, RMW, build, and workload provenance;
- publisher-sent plus recorder-received, admitted, committed, durable, and dropped
  event counters;
- final `blackboxrs.capture_quality.v1` metadata and rolling-retention totals;
- serialized messages and bytes retained on benchmark topics, counted by
  reading finalized MCAP when the optional `mcap` package is installed;
- drop details when exported by the recorder;
- CPU, RSS, queue, storage, startup, and shutdown observations;
- explicit validity errors and warnings;
- null latency fields when the measurement was not instrumented.

The supervisor prefers an authoritative `STOPPED_CLEAN` or
`STOPPED_INCOMPLETE` status from the recorder's final log record or status topic.
It reconciles that state with `capture_quality.json` and finalized sidecars. If
the process dies before a final status, the supervisor preserves the last
periodic durable value only as a lower bound and leaves the final durable count
null. It never upgrades a sidecar or an inferred process exit into a clean
shutdown.

The artifact deliberately omits hostname and GPU identity. This keeps public
artifacts free of private machine identifiers. A controlled result should add an
externally meaningful machine class through publication metadata if needed.

Publisher `sent` and recorder `received` are different boundaries. The current
recorder aggregate also includes graph and other control chronology, so it cannot
be subtracted from publisher output to estimate DDS delivery loss. The
`serialized_committed` is payload-only but downstream of recorder admission.
For a supervisor-generated, exactly matched workload with no rolling eviction or
partial segment, the artifact requires
`sent == serialized_committed + serialized_dropped`. A mismatch is an invalid
experiment with unexplained DDS or pre-callback loss, not a successful throughput
result. External parameter files, evicted sessions, and partial sessions cannot
establish that comparison and carry an explicit warning. A partial MCAP may
contain payloads committed before a storage fault, but the supervisor does not
parse an incomplete file to upgrade those payloads into a session total. It
reports `serialized_committed` as null rather than incorrectly treating the
unrecoverable total as zero.

`dropped` includes both pre-admission rejection and post-admission loss such as
writer-fault discard, so it is not added to `admitted`. Final recorder
reconciliation checks `received == committed + dropped` and also checks that
committed never exceeds admitted.

When rolling retention has evicted a segment or a partial segment remains,
payload-only counts from finalized files are reported as `serialized_retained`;
`serialized_committed` becomes null because the session-wide payload count is no
longer reconstructable. Total finalized storage bytes can still be reported from
retained plus explicitly evicted byte accounting.

## Long-run stability

The long-run wrapper defaults to four hours and accepts two through eight hours:

```bash
python scripts/native_capture_long_run.py \
  --duration-hours 4 \
  --topics 10 \
  --rate 1000 \
  --payload-bytes 1024 \
  --output artifacts/native_capture_long_run.json
```

It samples RSS, CPU, process write bytes, closed segments, and partial segments at
one hertz, together with status counters, queue depth, rolling-segment state, and
cumulative retention eviction. It reports a least-squares RSS slope after a
configurable warmup. The recorder does not currently export allocator telemetry,
so allocation trend is explicitly unavailable rather than inferred from RSS.
`--growth-threshold-mb-per-hour` is a user-selected screen, not a universal leak
definition. The full sample series is retained so a reviewer can distinguish a
step change, allocator settling, rotation sawtooth, and persistent growth.

CI may run a short smoke with `--allow-short --duration-sec`. A short run is
expected to classify the trend as inconclusive and must not be presented as a
multi-hour stability result.

## Fair comparisons

Comparisons use matched semantics:

- native full-payload recording versus rosbag2 with the same topics, QoS intent,
  storage plugin, chunking, compression, and durability policy;
- Python telemetry versus native telemetry only when both retain the same data;
- standalone versus composition with identical executor, topics, settings, and
  process accounting boundaries.

The harness refuses payloads smaller than its 32-byte marker contract. It
validates CDR layout, magic, topic ID, global publisher-sequence uniqueness, and
timestamp before using a sample for latency. Any negative corrected latency
invalidates the latency population and the run instead of being clamped away.

Use at least five fresh launches, alternate backend order, record warmup, and
retain every run plus aggregation logic. Publisher-to-callback latency is valid
only when publisher and recorder share a host and the embedded steady timestamp
is decoded outside the capture hot path. Shared CI checks buildability, schema,
counter reconciliation, explicit loss, boundedness, and clean shutdown. It does
not gate CPU or latency percentiles on shared runners.

## Metrics and limitations

CPU percent is recorder process CPU time divided by elapsed steady time, where
100 percent represents one fully used logical core. RSS comes from `/proc`.
Storage bytes are closed MCAP bytes. Process write bytes come from `/proc/<pid>/io`
and can include writes outside segment payload.

Shared-host ingest p50/p95/p99 use a deterministic bounded reservoir over the
publisher marker and recorder callback timestamp. The artifact records the
population, sample size, and cap. Ingest percentiles remain null when MCAP is
unavailable, a timestamp marker is missing, rolling eviction makes the session
population incomplete, or `--cross-host` declares different steady-clock
domains. Write and trigger-to-flush percentiles remain null until native
instrumentation exports samples or histograms with those precise boundaries. A
null is not zero.
