"""Passive BlackBoxRS recorder and online monitor on one host.

ros2 launch blackboxrs_ros recorder_monitor.launch.py config:=/path/to/go2_hardware.yaml

Both nodes only subscribe; their publishers are /diagnostics and
/blackboxrs/... (enforced by the publish guard). Stopping the launch sends
SIGINT; the recorder then drains and finalizes its evidence before exiting.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    config = LaunchConfiguration("config")
    return LaunchDescription([
        DeclareLaunchArgument("config", description="BlackBoxRS runtime configuration (YAML)"),
        Node(package="blackboxrs_ros", executable="recorder", name="blackboxrs_recorder",
             arguments=["--config", config], output="screen",
             sigterm_timeout="15", sigkill_timeout="20"),
        Node(package="blackboxrs_ros", executable="monitor", name="blackboxrs_monitor",
             arguments=["--config", config], output="screen"),
    ])
