# BlackBoxRS container

A single Dockerfile, pinned to **ROS 2 Humble** — the only distro
this project currently claims to support end-to-end.

## Why this exists

Before this file, `setup.sh` only provisioned Python deps.  Anyone
wanting to run BlackBoxRS *against real ROS 2* had to source
`/opt/ros/humble/setup.bash` themselves and hope their Python
interpreter saw `rclpy`.  This container removes that ambiguity:
everything the daemon needs is inside the image, built on top of the
official `ros:humble-ros-base`.
It also includes `ros2 bag` plus the default SQLite storage plugin so
the anomaly-triggered recorder path is available in the verified image.

## Build

```bash
# from the repository root
docker build -f docker/Dockerfile.humble -t blackboxrs:humble .
```

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

## Reproduce the live-ROS tests inside the container

```bash
docker run --rm --network host \
    -e ROS_DOMAIN_ID=42 \
    --entrypoint bash \
    blackboxrs:humble \
    -lc 'source /opt/ros/humble/setup.bash && cd /opt/blackboxrs && python3 -m pytest tests/integration/test_ros_live.py -q'
```

## What this image does NOT do

- No multi-distro support.  Iron / Jazzy are not in here because
  the repo does not live-verify them.
- No Jetson-specific variant.  The Jetson code paths
  (`tegrastats`, sysfs GPU load) are implemented in Python and will
  run wherever `tegrastats` is available, but this Dockerfile does
  not target `l4t-base`.
- No auto-start of a user ROS 2 stack.  The container runs only the
  BlackBoxRS recorder; you bring your own nodes.
