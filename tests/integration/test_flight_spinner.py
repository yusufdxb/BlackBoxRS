"""The recorder's wait loop (``_Spinner``) on an isolated localhost-only domain.

Skipped without rclpy. Publishes only std_msgs/String on /bbrs_spin_test/*
in domain 93 with ROS_LOCALHOST_ONLY=1.
"""

from __future__ import annotations

import time

import pytest

rclpy = pytest.importorskip("rclpy")


@pytest.fixture
def nodes(monkeypatch):
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    monkeypatch.setenv("ROS_DOMAIN_ID", "93")
    ctx = rclpy.Context()
    rclpy.init(context=ctx)
    rx = rclpy.create_node("bbrs_spin_rx", context=ctx, enable_rosout=False,
                           start_parameter_services=False)
    tx = rclpy.create_node("bbrs_spin_tx", context=ctx, enable_rosout=False,
                           start_parameter_services=False)
    yield ctx, rx, tx
    rx.destroy_node()
    tx.destroy_node()
    rclpy.shutdown(context=ctx)


def _qos(reliable: bool, depth: int = 200):
    from rclpy.qos import QoSProfile, QoSReliabilityPolicy
    return QoSProfile(depth=depth, reliability=QoSReliabilityPolicy.RELIABLE if reliable
                      else QoSReliabilityPolicy.BEST_EFFORT)


def _wait_matched(pub, n: int = 1, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while pub.get_subscription_count() < n:
        assert time.monotonic() < end, "subscription never matched"
        time.sleep(0.05)


def test_one_wakeup_drains_queue_in_order_with_message_info(nodes):
    from std_msgs.msg import String

    from blackboxrs.flight.recorder import _Spinner

    ctx, rx, tx = nodes
    got: list[tuple[str, dict | None]] = []

    def cb(msg, info):
        got.append((msg.data, info))

    cb._bbrs_info = True
    rx.create_subscription(String, "/bbrs_spin_test/burst", cb, _qos(False))
    ticks: list[int] = []
    rx.create_timer(0.01, lambda: ticks.append(1))
    spinner = _Spinner(rx, ctx)
    pub = tx.create_publisher(String, "/bbrs_spin_test/burst", _qos(True, 100))
    try:
        _wait_matched(pub)
        for i in range(50):
            pub.publish(String(data=str(i)))
        time.sleep(0.3)  # all 50 are queued in the reader before the next wait
        spinner.spin_once(0.5)
        assert [d for d, _ in got] == [str(i) for i in range(50)]
        for _, info in got:
            assert info is not None
            # Humble + Cyclone fills the source timestamp; reception may be 0.
            assert info["source_timestamp"] > 0 and "received_timestamp" in info
        end = time.monotonic() + 2.0
        while not ticks and time.monotonic() < end:
            spinner.spin_once(0.05)
        assert ticks, "timer never fired under the spinner"
    finally:
        spinner.close()


def test_qos_events_are_delivered_by_poll_events(nodes):
    from rclpy.qos_event import SubscriptionEventCallbacks
    from std_msgs.msg import String

    from blackboxrs.flight.recorder import _Spinner

    ctx, rx, tx = nodes
    events: list[object] = []
    # A RELIABLE reader and a BEST_EFFORT writer are incompatible: the
    # reader's middleware raises a requested-incompatible-QoS event.
    rx.create_subscription(String, "/bbrs_spin_test/qos", lambda m: None, _qos(True),
                           event_callbacks=SubscriptionEventCallbacks(
                               incompatible_qos=events.append))
    spinner = _Spinner(rx, ctx)
    tx.create_publisher(String, "/bbrs_spin_test/qos", _qos(False))
    try:
        end = time.monotonic() + 5.0
        while not events and time.monotonic() < end:
            spinner.spin_once(0.05)
            spinner.poll_events()
        assert events, "incompatible QoS event never reached its callback"
        assert events[0].total_count >= 1
    finally:
        spinner.close()
