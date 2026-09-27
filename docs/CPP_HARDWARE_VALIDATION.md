# C++ runtime: hardware validation procedure (GO2 + Jetson Orin NX)

**Status: SOFTWARE READY FOR HARDWARE VALIDATION. Not validated on hardware.**
No gate below has been run on the GO2 or on the Orin NX. Every hardware
number in this repository for the C++ runtime is still to be measured; the
workstation numbers in [CPP_BENCHMARKS.md](CPP_BENCHMARKS.md) are not
Jetson numbers.

This document is the plan for the first sessions. The first gates are
passive: nothing in them needs the robot to move, and BlackBoxRS never
commands motion in any gate.

## What BlackBoxRS does and does not do on the robot

* It **only subscribes** (BEST_EFFORT, VOLATILE). It publishes nothing but
  `/diagnostics` and `/blackboxrs/...` status, both created through the
  publish guard, and nothing at all with `diagnostics.publish: false`
  (`test_passive_nodes` checks this on a live graph).
* The motion and control topics (`/cmd_vel`, `/nav/cmd_vel`,
  `/teleop/cmd_vel`, `/helix/cmd_vel`, `/helix/hold`, `/api/sport/request`,
  `/lowcmd`, `/wirelesscontroller`, anything under `/api/`) are refused by the
  guard before a publisher exists, including through a remap. A runtime
  configuration can add to that list, never remove from it.
* The `go2_helix` profile does not subscribe `/cmd_vel`, so HELIX preflight C6
  (`/cmd_vel` has the sport sink as its only subscriber) is unaffected.
* Preflight creates no publisher. The replay node creates none unless run
  with `--publish`, and then only on `/blackboxrs/replay/...`.

## The environment this was prepared for

Measured on the payload 2026-09-01 (GO2 field notes): Jetson Orin NX 16 GB,
Ubuntu 22.04.5, kernel 5.15.148-tegra, aarch64, ROS 2 Humble,
`RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`, robot NIC `enP8p1s0`.
ASSUMPTION: the JetPack point release (JetPack 6 / L4T R36 by the kernel)
was not recorded; H0 records it.

The C++ build needs GCC 11 or newer (C++20, `std::jthread`), CMake 3.22,
and these packages. Only `nlohmann-json3-dev` is not already pulled in by a
ROS 2 Humble install:

| Package | Why | Present on a Humble install |
|---|---|---|
| `build-essential`, `cmake`, `ninja-build` (optional) | build | cmake yes |
| `libyaml-cpp-dev` | profile and runtime config parsing | yes (yaml_cpp_vendor) |
| `libssl-dev` | SHA-256 (OpenSSL EVP) | yes (Fast DDS depends on it) |
| `nlohmann-json3-dev` | JSON | **no** |
| `libgtest-dev` or `gtest_vendor` | tests | yes (gtest_vendor) |
| `unitree_go`, `unitree_api`, `helix_msgs` type support | decoding those topics | from the unitree and HELIX workspaces |

The lab network has no internet. Stage the arm64 `.deb` before the
session: `apt-get download nlohmann-json3-dev` on any Ubuntu 22.04 arm64
machine (or from packages.ubuntu.com, jammy, arm64), copy it with the source,
and `sudo dpkg -i nlohmann-json3-dev_*.deb` on the payload.

Docker was considered and not used: the payload already has the exact ROS
and vendor workspaces the recorder must load type support from, a
container would have to mount them anyway, and it would add an image
transfer to an offline network. The build is two colcon packages.

## Before the session (workstation, mandatory)

```bash
cd ~/Projects/BlackBoxRS && git checkout <CANDIDATE_SHA> && git status --porcelain   # empty
colcon build --base-paths cpp ros2 --cmake-args -DCMAKE_BUILD_TYPE=Release
ctest --test-dir build/blackboxrs_core                    # all pass
cpp/build/release/tools/blackboxrs verify --repeat 10 --json verify_x86.json
HELIX_SRC=~/workspace/helix scripts/cpp/helix_rehearsal.sh /tmp/rehearsal
#   must print STAGE A..F: PASS [REHEARSAL...], "recorder exit 0",
#   every bundle VERIFIED, and "Python vs C++ replay: N/N byte-identical"
```

Copy to the payload: the repository at the candidate SHA (`rsync`, no build
trees), `verify_x86.json`, and the `nlohmann-json3-dev` arm64 `.deb`.

## Environment on the payload (every terminal)

```bash
source /opt/ros/humble/setup.bash
source <UNITREE_WS>/install/setup.bash       # unitree_go, unitree_api types
source <HELIX>/install/setup.bash            # helix_msgs types
source ~/yusuf/BlackBoxRS/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
grep -o 'NetworkInterface name="[^"]*"' "${CYCLONEDDS_URI#file://}"   # enP8p1s0
ip -brief link show enP8p1s0                                          # UP
sudo date -u -s "<current UTC time>"   # no RTC: the payload has booted at 1970
ros2 daemon stop
ros2 topic hz /lowstate                # ~500 Hz, or stop: presence is not data
EV=~/blackboxrs_evidence
```

## Gate H0: build on the Orin NX

Robot may be off. Nothing runs against ROS.

```bash
cd ~/yusuf/BlackBoxRS
uname -m; cat /etc/nv_tegra_release; gcc --version | head -1; cmake --version | head -1
colcon build --base-paths cpp ros2 --cmake-args -DCMAKE_BUILD_TYPE=Release
file install/blackboxrs_ros/lib/blackboxrs_ros/recorder          # ELF 64-bit ... ARM aarch64
ctest --test-dir build/blackboxrs_core --output-on-failure
build/blackboxrs_core/tools/blackboxrs version
build/blackboxrs_core/tools/blackboxrs verify --repeat 10 --json verify_arm.json
python3 - <<'PY'
import json
a = {c["case"]: c["result_sha256"] for c in json.load(open("verify_x86.json"))["cases"]}
b = {c["case"]: c["result_sha256"] for c in json.load(open("verify_arm.json"))["cases"]}
print("identical on x86_64 and aarch64:", a == b, sorted(k for k in a if a[k] != b.get(k)))
PY
build/blackboxrs_core/tools/blackboxrs benchmark --seconds 30 --json H0_benchmark_orin.json
```

PASS when: the build has no warnings, every core test passes, `verify` is
30/30 deterministic, the result digests are **identical to the workstation's**
(the core is compiled with `-ffp-contract=off` for exactly this), and the
benchmark JSON is saved. Record JetPack/L4T, GCC and CMake versions.

## Gate H1: idle, passive recording

Robot powered, lying down or standing on the remote's stand lock, **no
commanded motion**. HELIX may be off.

```bash
ros2 run blackboxrs_ros preflight --config configs/go2_hardware.yaml --json > H1_preflight.json
#   must be GO; read every WARN. P8 lists each topic's measured rate.
ros2 launch blackboxrs_ros recorder_monitor.launch.py config:=$PWD/configs/go2_hardware.yaml
# in another terminal, for 10 minutes:
pidstat -u -r -t -p $(pgrep -f 'lib/blackboxrs_ros/recorder') 5 120 > H1_pidstat.txt
ros2 topic echo --once /blackboxrs/status > H1_status.json   # queue, drops, writer lag
# Ctrl-C the launch (SIGINT): the recorder drains, finalizes, prints "stopped (...)"
cpp/build/release/tools/blackboxrs validate $EV/<session>/inc_*
cpp/build/release/tools/blackboxrs inspect  $EV/<session>/inc_*
```

Measure and record: recorder CPU (process and per thread: executor,
pipeline, writer), RSS over the 10 minutes, ingest queue depth and
high-water, drops, writer bytes/s and lag, `/lowstate` and
`/sportmodestate` rates with and without the recorder running.

PASS when: preflight GO; 0 dropped (ingest and shutdown); the bundle
VERIFIED; recorded rates within 10 % of the field-note rates (500, 295, 151
Hz); recorder CPU and RSS inside the profile budget
(`preflight.max_recorder_cpu_percent` 50 % of one core,
`max_recorder_rss_mb` 500); no abort condition met.

## Gate H2: existing telemetry, offline replay of a real bundle

Only after H0 and H1 pass. Robot standing still under the remote; HELIX
stack running (T1) with the sink in `dry_run` (T2); no stage runner.

Record 5 minutes as in H1, then on the workstation:

```bash
B=<bundle>
blackboxrs validate $B                                    # VERIFIED
blackboxrs inspect $B                                     # every go2_helix topic that exists has records
blackboxrs replay $B --sut observed --json H2_cpp.json    # INCOMPLETE is expected (no commands)
robot-blackbox lab replay $B --sut observed --json H2_py.json
cmp H2_cpp.json H2_py.json                                # byte-identical
robot-blackbox flight replay $B --write                   # Python analysis of C++ evidence
```

Check: every profile topic's `topic_status` is `subscribed` or explicitly
absent; DDS source and reception timestamps are present (Cyclone on the
payload supplying them was never verified); the robot clock offset
reported by the `clock_offset` finding is stable over the session; no
decode errors.

## Gate H3: controlled motion, observed

Only after H0 to H2 pass. The motion is HELIX's already-approved stage D
(docs/HW_MOTION_TEST.md in HELIX: operator stands the robot with the remote,
`mcf` only, 2 m clear, spotter, sink `armed`). BlackBoxRS starts before the
stage and only records and monitors.

PASS when: HELIX preflight stays GO with BlackBoxRS running; 0 drops; the
bundle VERIFIED; the online monitor reports no invariant FAIL (its findings
are in `online_findings.jsonl`); `/lowstate` rate unchanged within 10 %.

## Gate H4: HELIX Stage E evidence capture

Only after H3 passes. Use the triggered configuration so the bundle is cut
around the HELIX hold:

```bash
ros2 run blackboxrs_ros preflight --config configs/go2_hardware_stage_e.yaml   # GO
ros2 launch blackboxrs_ros recorder_monitor.launch.py config:=$PWD/configs/go2_hardware_stage_e.yaml
# T4: ros2 run helix_arbiter helix_hw_stage --stage E ...   (HELIX procedure, unchanged)
# wait >= 15 s (post window) after the stage ends, then Ctrl-C the launch
```

Offline afterwards, on the real bundle:

```bash
blackboxrs validate $B
blackboxrs replay $B --sut observed --json E_cpp.json      # stop_dominance must be PASS and exercised
robot-blackbox lab replay $B --sut observed --json E_py.json && cmp E_cpp.json E_py.json
robot-blackbox flight replay $B --write                    # the stop chain and its verdicts
```

This is the evidence Replay Lab has been waiting for: a real HELIX Stage E
bundle. Commit it as `examples/replay_lab/evidence/real_stage_e/` only after
it validates and both engines agree on it.

## Abort conditions (pre-registered)

Stop the gate (Ctrl-C the recorder; the robot side follows HELIX's own abort
rules, which always take precedence: handheld remote, lab e-stop, Ctrl-C in
the HELIX terminals) when any of these happens. The numeric limits are the
profile's existing budgets; H1 measurements refine them, nothing here is a
new safety number.

| Condition | Source of the limit | Action |
|---|---|---|
| BlackBoxRS appears as a publisher of any motion or control topic | publish guard, preflight P7 | abort the session; this is a defect |
| Recorder CPU above `preflight.max_recorder_cpu_percent` (50 % of one core) for more than 10 s | profile budget | abort; recorder too expensive for the payload |
| Recorder RSS above `preflight.max_recorder_rss_mb` (500 MB), or rising for the whole of H1 | profile budget | abort |
| Any message dropped at the ingest queue on a HELIX, command or odometry topic | recorder metrics | abort H3/H4: the evidence would be incomplete |
| Ingest queue high-water keeps rising (queue never drains) | recorder metrics | abort |
| Any evidence write error, `write_failed` or `complete_with_loss` bundle, disk below the floor | writer | abort |
| Wall clock before 2025, or a `clock_jump` record | preflight P3, recorder | abort, fix the clock |
| `/lowstate` rate drops by more than 10 % with the recorder running, or HELIX preflight goes NO-GO | field-note rate, HELIX | abort: the recorder degrades ROS |
| The recorder crashes, restarts, or exits non-zero | process | abort |
| A bundle fails `blackboxrs validate` | integrity record | stop; do not advance to the next gate |

## Preflight reference

`ros2 run blackboxrs_ros preflight --config <runtime.yaml> [--json] [--listen S]`
returns **GO** (exit 0, possibly with warnings) or **NO-GO** (exit 1). It
never publishes and never commands motion. Checks:

| Id | Check | Fails when |
|---|---|---|
| P1 | configuration parses and validates | invalid |
| P2 | build recorded | (warning if not Release or dirty) |
| P3 | wall clock plausible | year before 2025 |
| P4 | RMW, ROS_DOMAIN_ID, CycloneDDS interface | wrong RMW or domain; the configured NIC does not exist |
| P5 | evidence directory writable, disk free | not writable; below the hard floor |
| P7 | no BlackBoxRS publisher on a control topic; HELIX C6 | any; `/cmd_vel` subscribed while HELIX runs |
| P8 | each profile topic: publisher, type, data rate | required topic absent, mistyped, silent or below `min_rate_fraction` of `expected_hz` |
| P9 | live self-test of the real recorder node | bundle not verified; any drop; CPU or RSS over the profile budget |

## Performance budget: what to record on the Orin NX

The same executables produce comparable JSON on both machines. Do not
compare against expected Jetson numbers: there are none yet.

| Metric | Command | Workstation (x86_64) | Orin NX |
|---|---|---|---|
| core pipeline CPU at 1x, 2x, 5x; drops; push latency | `blackboxrs benchmark` | CPP_BENCHMARKS.md | H0 |
| recorder process CPU and RSS on real GO2 traffic | pidstat during H1 | (synthetic traffic: CPP_BENCHMARKS.md) | H1 |
| replay throughput (x real time) | `blackboxrs benchmark --only replay` | CPP_BENCHMARKS.md | H0 |
| memory stability | `blackboxrs benchmark --soak 1800 --soak-scale 5 --only none` | CPP_BENCHMARKS.md | optional, off-robot |

## Unresolved hardware assumptions

* ASSUMPTION: Cyclone DDS on the payload supplies DDS source and reception
  timestamps for every profile topic (seen on the workstation only).
* ASSUMPTION: `nlohmann-json3-dev` 3.10 for jammy arm64 installs cleanly
  offline; everything else is already on a Humble install.
* ASSUMPTION: the recorder's CPU on the Orin NX scales from the workstation
  roughly like the Python recorder did. Unknown until H0/H1.
* ASSUMPTION: the evidence disk on the payload sustains the writer's
  throughput with an fsync per second (a few hundred KB/s at 1x on the
  workstation).
* Not known: the thermal zone names on the payload's L4T.
