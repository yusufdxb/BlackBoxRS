# Remote / observer-mode quickstart

This guide takes a fresh laptop with no BlackBoxRS install and ends with
a real incident bundle captured off a robot you can already reach with
`ros2 topic list`. Target time: 5 minutes.

The two assumptions are:

1. You already have a ROS 2 installation on the laptop. Humble is the
   verified end-to-end path in CI. Other ROS 2 distributions may work
   when `rclpy` is ABI-compatible, but they are not claimed as verified
   here. `source /opt/ros/<distro>/setup.bash` works.
2. The robot is reachable over DDS from this laptop, i.e.
   `ros2 topic list` returns the robot's topics. If it does not, see
   *DDS reachability* at the bottom.

You do **not** need shell access to the robot.

---

## 1. Install

```bash
# In your project venv, conda env, or a fresh virtualenv:
pip install -e git+https://github.com/yusufdxb/BlackBoxRS.git#egg=blackboxrs
# (or, if you've cloned: pip install -e .)
```

The `robot-blackbox` CLI is now on your PATH. Confirm:

```bash
robot-blackbox --help
```

`rclpy` is **not** pulled from PyPI. It must come from the system ROS 2
install you sourced above. If `python3 -c "import rclpy"` fails, fix
your ROS 2 sourcing before continuing.

---

## 2. Configure observer mode

```bash
robot-blackbox init      # creates ~/.blackboxrs/config.yaml with defaults
```

Edit that file and add a `runtime` block:

```yaml
# ~/.blackboxrs/config.yaml
runtime:
  role: observer
  observed_host: go2-edu-01     # free-form robot label (your choice)
```

That single block is the entire pivot. On the next `start`, the daemon
does three things:

- `system_monitor` is disabled (CPU / memory / disk readings would
  describe your laptop, not the robot).
- The anomaly engine drops `ProcessSignalsDetector` for the same
  reason.
- Every event, snapshot, and incident bundle is tagged with both
  `observer_host` (your laptop's hostname) and `observed_host` (the
  label above).

Onboard-mode behaviour is unchanged when `runtime.role` is absent or
set to `onboard`.

---

## 3. Run the daemon

```bash
# Make sure the laptop is on the right DDS domain *first*:
export ROS_DOMAIN_ID=0         # or whatever the robot uses
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp   # match the robot's RMW
ros2 topic list                # sanity check; you should see robot topics

robot-blackbox start --foreground
```

You should see a log line of the form:

```
BlackBoxRS observer mode: observer=my-laptop observed=go2-edu-01
(host-bound collectors and ProcessSignalsDetector disabled).
```

Leave the daemon running. Drive the robot through a failure scenario,
or just wait for something interesting (a dropped topic, a TF tree
break, a QoS mismatch) to happen.

---

## 4. Build an incident bundle

In a second terminal, after the failure:

```bash
robot-blackbox incident build --since 5m
robot-blackbox incident verify ~/.blackboxrs/incidents/inc_*
```

This slices the local JSONL log (everything the laptop captured) into a
bundle at `~/.blackboxrs/incidents/inc_<timestamp>_<id>/`, then verifies
the finalized manifest and checksums.

Render the report:

```bash
robot-blackbox incident show ~/.blackboxrs/incidents/inc_*
```

The header now distinguishes observer from observed:

```
# Incident `inc_2026-05-17T14-22-00_a3f2`

- **Severity**: error
- **Observer**: `my-laptop`
- **Observed**: `go2-edu-01`
```

The rest of the bundle is identical to an onboard bundle and is
portable: send it over Slack and any teammate can re-render the report
with `robot-blackbox incident show <bundle-dir>`.

`incident verify` exits 0 for finalized, checksum-valid bundles. It exits
2 for readable legacy bundles without `manifest.json`, and 1 for
incomplete, corrupted, or unsupported bundles. The checksum manifest
detects local partial writes and accidental modification; it is not an
authentication or tamper-proofing mechanism.

---

## 5. Adopt a prevention rule

```bash
robot-blackbox prevention adopt --from-incident \
  ~/.blackboxrs/incidents/inc_<id>
robot-blackbox preflight
```

`preflight` exits 0 / 1 / 2 for pass / block / warn. Wire it into your
launch script so the next bringup refuses to start when the precursor
to the captured failure shows up again.

> **Trust boundary.** Prevention rules live as plain YAML files under
> `~/.blackboxrs/prevention/rules/`. Some rule kinds (e.g.
> `custom_python`) execute code in the daemon's privilege context at
> preflight time. Treat that directory as a trust boundary: anyone
> who can write to it can run arbitrary code under the user that runs
> `robot-blackbox preflight`. On shared workstations, scope the
> directory to your user and version-control the rule files you
> intend to share.

---

## What each detector measures in observer mode

Live in the current engine:

| Detector | Source | Works remotely? |
|---|---|---|
| `frequency` | rclpy graph polling | yes |
| `dead_topic` | last-message timestamp per topic | yes |
| `qos_mismatch` | rclpy `get_publishers_info_by_topic` | yes |
| `threshold` (cpu / mem) | local psutil | only when system-monitor is enabled; describes the observer host. Disabled by default in observer mode. |
| `tf_topology` | `/tf` and `/tf_static` snapshots | yes; DDS-bound |
| `clock_skew` | system clock, NTP/chrony, and ROS `/clock` sampling | partly. `/clock` is robot-relative over DDS; host/NTP checks describe the observer. |
| `process_signals` | local psutil per-pid walk | onboard only. In observer mode the producer is disabled because it would sample the laptop, not the robot. |

You can confirm what's actually wired today by running
`robot-blackbox start --foreground` and watching the
`Initialised N anomaly detectors` startup log line.

If you want host metrics from the robot too, the supported path is to
run a second BlackBoxRS daemon onboard the robot in `onboard` mode and
merge bundles later. That isn't built in v0.4; for now the rule is
"bundles describe one host at a time, observer mode declares which
host."

---

## DDS reachability

If `ros2 topic list` from your laptop does **not** show the robot's
topics, BlackBoxRS will still start but will produce empty bundles.
Common fixes:

- `ROS_DOMAIN_ID` must match the robot's (`echo $ROS_DOMAIN_ID` on
  both sides).
- `RMW_IMPLEMENTATION` must match. Mixing FastDDS with CycloneDDS is
  not supported.
- For Cyclone over Wi-Fi, you usually need a Cyclone config XML that
  forces unicast discovery to the robot's IP. The
  `CYCLONEDDS_URI=file:///path/to/cyclonedds.xml` env var picks it up.
- Some robots run on a VPN or a dedicated VLAN; your laptop must be on
  the same network segment, not behind NAT.

Verify with `ros2 topic hz /tf` (or any topic the robot publishes
continuously). If `hz` reports a steady rate, BlackBoxRS will see the
same traffic.

---

## What this guide does *not* cover

- Multi-robot capture (run one observer per robot for now).
- Capturing a rosbag2 from the observer in lockstep with the incident
  bundle. The `rosbag2` recorder runs in observer mode, but disk
  bandwidth on the laptop becomes the limiter for high-rate topics.
- Sharing bundles with non-developers. The bundle is a directory; tar
  it and send it.
