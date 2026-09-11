# BlackBoxRS container

A single Dockerfile, pinned to **ROS 2 Humble**: the only distro
this project currently claims to support end-to-end.

## Why this exists

Before this file, `setup.sh` only provisioned Python deps.  Anyone
wanting to run BlackBoxRS *against real ROS 2* had to source
`/opt/ros/humble/setup.bash` themselves and hope their Python
interpreter saw `rclpy`.  This container removes that ambiguity:
everything the daemon and bounded C++ recorder need is inside the image,
built on top of the official `ros:humble-ros-base`. The native package is
compiled in a separate build stage, so the runtime image does not include a
native source tree, build tree, or package test artifacts. The upstream
`ros:humble-ros-base` image itself may include general build utilities.

The image includes `ros2 bag`, SQLite and MCAP storage plugins, Fast DDS,
Cyclone DDS, and type support for `std_msgs`, `geometry_msgs`, `nav_msgs`,
`sensor_msgs`, and `tf2_msgs`. Add the matching ROS packages in a derived
image when capturing other interface families.

## Build

```bash
# from the repository root
docker build -f docker/Dockerfile.humble -t blackboxrs:humble .

# explicit test image used by CI
docker build --target ci -f docker/Dockerfile.humble -t blackboxrs:humble-ci .
```

The default `production` target contains the daemon, native recorder, and
runtime dependencies. It does not install the BlackBoxRS `dev` extra and
excludes the repository test suite, proof scripts, and committed sample bag.
The explicit `ci` target layers those assets and project test dependencies onto
the exact same runtime stage. General tools inherited from the upstream ROS
base image are outside this target split.

## Run

Most networked ROS 2 setups expect host networking so DDS discovery
just works:

```bash
docker run --rm --network host \
    -e ROS_DOMAIN_ID=42 \
    -v "$HOME/.blackboxrs:/root/.blackboxrs" \
    blackboxrs:humble
```

The default command is `robot-blackbox start --foreground`.  Logs
are written under `/root/.blackboxrs/logs`, which the bind-mount
above persists on the host.

Set `capture.backend: cpp` in `/root/.blackboxrs/config.yaml` to use the
installed `blackbox_capture_cpp` recorder. Keep that directory mounted because
it also holds the native capture session data referenced by the daemon.

The native executables are available in every container command because the
entrypoint sources both ROS Humble and `/opt/blackboxrs/native_install`:

```bash
docker run --rm blackboxrs:humble \
    ros2 pkg executables blackbox_capture_cpp
```

## Reproduce the live-ROS tests inside the container

```bash
docker build --target ci -f docker/Dockerfile.humble -t blackboxrs:humble-ci .
docker run --rm --network host \
    -e ROS_DOMAIN_ID=42 \
    --entrypoint bash \
    blackboxrs:humble-ci \
    -lc 'source /opt/ros/humble/setup.bash && source /opt/blackboxrs/native_install/setup.bash && cd /opt/blackboxrs && python3 -m pytest tests/integration/test_ros_live.py -q'
```

CI also replays the committed 909-message MCAP through the recorder installed
in this image and checks schema names, exact per-topic CDR payload order,
checksums, clean finalization, and loss-counter reconciliation. A separate
bounded smoke test starts `BlackBoxDaemon` with `capture.backend: cpp`, verifies
that its native child and watchdog are live, stops through the daemon lifecycle,
and validates the persisted clean session from the production image.

## What this image does NOT do

- No multi-distro support.  Iron / Jazzy are not in here because
  the repo does not live-verify them.
- No Jetson-specific variant.  The Jetson code paths
  (`tegrastats`, sysfs GPU load) are implemented in Python and will
  run wherever `tegrastats` is available, but this Dockerfile does
  not target `l4t-base`.
- No auto-start of a user ROS 2 stack. The container runs BlackBoxRS services;
  you bring your own application nodes.
