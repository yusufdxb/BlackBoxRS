#!/usr/bin/env python3
"""Controlled ROS 2 publisher for telemetry-health validation."""

from __future__ import annotations

import argparse
import math
import time

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="/utlidar/robot_pose")
    parser.add_argument("--rate-hz", type=float, default=18.75)
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--startup-delay-sec", type=float, default=0.0)
    parser.add_argument("--silent-after-sec", type=float, default=None)
    parser.add_argument("--pause-at-sec", type=float, default=None)
    parser.add_argument("--pause-duration-sec", type=float, default=0.0)
    parser.add_argument("--freeze-after-sec", type=float, default=None)
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--best-effort", action="store_true")
    parser.add_argument(
        "--jitter-sec",
        default="",
        help="Comma-separated interval offsets repeated over the base period.",
    )
    args = parser.parse_args()
    if args.rate_hz <= 0 or args.duration_sec <= 0:
        raise SystemExit("rate and duration must be positive")

    jitter = [float(value) for value in args.jitter_sec.split(",") if value]
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=(
            ReliabilityPolicy.BEST_EFFORT
            if args.best_effort
            else ReliabilityPolicy.RELIABLE
        ),
        durability=DurabilityPolicy.VOLATILE,
    )
    rclpy.init()
    node = rclpy.create_node("telemetry_health_test_publisher")
    publisher = node.create_publisher(PoseStamped, args.topic, qos)
    started = time.monotonic()
    next_publish = started + args.startup_delay_sec
    sent = 0
    frozen_stamp = None
    try:
        while rclpy.ok() and time.monotonic() - started < args.duration_sec:
            now = time.monotonic()
            rclpy.spin_once(node, timeout_sec=0.001)
            if now < next_publish:
                continue
            elapsed = now - started
            paused = (
                args.pause_at_sec is not None
                and args.pause_at_sec
                <= elapsed
                < args.pause_at_sec + args.pause_duration_sec
            )
            silent = (
                args.silent_after_sec is not None
                and elapsed >= args.silent_after_sec
            )
            capped = args.max_messages is not None and sent >= args.max_messages
            if not paused and not silent and not capped:
                msg = PoseStamped()
                if (
                    args.freeze_after_sec is not None
                    and elapsed >= args.freeze_after_sec
                ):
                    if frozen_stamp is None:
                        frozen_stamp = node.get_clock().now().to_msg()
                    msg.header.stamp = frozen_stamp
                else:
                    msg.header.stamp = node.get_clock().now().to_msg()
                msg.header.frame_id = "odom"
                msg.pose.position.x = sent * 0.001
                msg.pose.position.y = math.sin(sent * 0.01) * 0.01
                msg.pose.orientation.w = 1.0
                publisher.publish(msg)
                sent += 1
            base_period = 1.0 / args.rate_hz
            offset = jitter[(sent - 1) % len(jitter)] if jitter else 0.0
            next_publish += max(0.001, base_period + offset)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_publisher(publisher)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
