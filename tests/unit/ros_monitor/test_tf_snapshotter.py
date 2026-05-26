"""Unit tests for the TF snapshot producer.

Covers two layers:

1. ``TfSnapshotState``: the pure, rclpy-free bookkeeping. Records
   ``(parent, child)`` updates with a wall-clock-ish timestamp, computes
   per-edge ``last_update_sec_ago``, garbage-collects old dynamic edges,
   and clamps clock rewinds to ``0.0``.

2. ``TfSnapshotter``: the rclpy integration shell, exercised with a fake
   node so the tests stay import-safe on a host without ROS 2. Asserts
   QoS profiles for ``/tf`` and ``/tf_static``, observer-mode opt-in,
   and event publication to the shared :class:`EventBus`.
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any
from unittest.mock import MagicMock

import pytest

from blackboxrs.core.config import (
    BlackBoxConfig,
    RosMonitorConfig,
    RuntimeConfig,
    TfProducerConfig,
)
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.ros_monitor.tf_snapshotter import (
    TfSnapshotState,
    TfSnapshotter,
)


# ---------------------------------------------------------------------------
# Pure-state tests (no rclpy)
# ---------------------------------------------------------------------------


class TestTfSnapshotState:
    """Bookkeeping for the (parent, child) -> last-stamp map."""

    def test_empty_state_emits_empty_edges(self):
        state = TfSnapshotState()
        payload = state.build_payload(now_sec=100.0, gc_age_sec=60.0)
        assert payload == {"edges": []}

    def test_single_dynamic_edge_reports_age(self):
        state = TfSnapshotState()
        state.record("odom", "base_link", stamp_sec=99.5, is_static=False)
        payload = state.build_payload(now_sec=100.0, gc_age_sec=60.0)
        assert payload["edges"] == [
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": pytest.approx(0.5),
                "is_static": False,
            }
        ]

    def test_static_edge_reports_zero_age_regardless_of_stamp(self):
        state = TfSnapshotState()
        state.record("base_link", "imu_link", stamp_sec=10.0, is_static=True)
        payload = state.build_payload(now_sec=999.0, gc_age_sec=60.0)
        assert payload["edges"] == [
            {
                "parent": "base_link",
                "child": "imu_link",
                "last_update_sec_ago": 0.0,
                "is_static": True,
            }
        ]

    def test_last_writer_wins_on_same_edge(self):
        state = TfSnapshotState()
        state.record("odom", "base_link", stamp_sec=10.0, is_static=False)
        state.record("odom", "base_link", stamp_sec=20.0, is_static=False)
        payload = state.build_payload(now_sec=20.5, gc_age_sec=60.0)
        ages = [e["last_update_sec_ago"] for e in payload["edges"]]
        assert ages == [pytest.approx(0.5)]

    def test_multi_parent_edges_both_kept(self):
        state = TfSnapshotState()
        state.record("base_link", "imu_link", stamp_sec=10.0, is_static=False)
        state.record("body", "imu_link", stamp_sec=10.0, is_static=False)
        payload = state.build_payload(now_sec=10.0, gc_age_sec=60.0)
        pairs = {(e["parent"], e["child"]) for e in payload["edges"]}
        assert pairs == {("base_link", "imu_link"), ("body", "imu_link")}

    def test_gc_drops_dynamic_edges_older_than_gc_age(self):
        state = TfSnapshotState()
        state.record("odom", "base_link", stamp_sec=10.0, is_static=False)
        state.record("map", "odom", stamp_sec=50.0, is_static=False)
        # now=100, gc_age=60 -> odom->base_link (90 s old) drops,
        # map->odom (50 s old) survives.
        payload = state.build_payload(now_sec=100.0, gc_age_sec=60.0)
        pairs = {(e["parent"], e["child"]) for e in payload["edges"]}
        assert pairs == {("map", "odom")}

    def test_gc_never_drops_static_edges(self):
        state = TfSnapshotState()
        state.record(
            "base_link", "imu_link", stamp_sec=0.0, is_static=True
        )
        payload = state.build_payload(now_sec=10_000.0, gc_age_sec=60.0)
        assert len(payload["edges"]) == 1
        assert payload["edges"][0]["is_static"] is True

    def test_clock_rewind_clamps_age_to_zero(self):
        state = TfSnapshotState()
        # stamp is in the future relative to ``now`` (negative age).
        state.record("odom", "base_link", stamp_sec=110.0, is_static=False)
        payload = state.build_payload(now_sec=100.0, gc_age_sec=60.0)
        assert payload["edges"][0]["last_update_sec_ago"] == 0.0

    def test_expected_frames_added_when_provided(self):
        state = TfSnapshotState()
        payload = state.build_payload(
            now_sec=100.0,
            gc_age_sec=60.0,
            expected_frames=["base_link", "odom"],
        )
        assert payload["expected_frames"] == ["base_link", "odom"]

    def test_expected_frames_omitted_when_empty(self):
        state = TfSnapshotState()
        payload = state.build_payload(
            now_sec=100.0, gc_age_sec=60.0, expected_frames=[]
        )
        assert "expected_frames" not in payload


# ---------------------------------------------------------------------------
# rclpy-integration tests (fake node)
# ---------------------------------------------------------------------------


def _drain(q: "Queue[BlackBoxEvent]") -> list[BlackBoxEvent]:
    out: list[BlackBoxEvent] = []
    while True:
        try:
            out.append(q.get_nowait())
        except Empty:
            return out


@dataclass
class _FakeSubscription:
    msg_type: Any
    topic: str
    callback: Any
    qos: Any


class _FakeNode:
    """Stand-in for an ``rclpy.node.Node`` that records create_subscription
    calls and exposes a controllable monotonic clock for the snapshotter
    timer logic.
    """

    def __init__(self) -> None:
        self.subscriptions: list[_FakeSubscription] = []
        self.timers: list[tuple[float, Any]] = []
        self.now_sec = 0.0

    def create_subscription(
        self, msg_type, topic, callback, qos_profile, *args, **kwargs
    ):
        sub = _FakeSubscription(
            msg_type=msg_type,
            topic=topic,
            callback=callback,
            qos=qos_profile,
        )
        self.subscriptions.append(sub)
        return sub

    def create_timer(self, period_sec, callback):
        self.timers.append((period_sec, callback))
        return MagicMock()

    def destroy_subscription(self, sub):  # pragma: no cover
        if sub in self.subscriptions:
            self.subscriptions.remove(sub)

    def destroy_timer(self, timer):  # pragma: no cover
        pass


def _make_tf_msg(parent: str, child: str, stamp_sec: float):
    """Build a minimal stand-in for ``tf2_msgs/msg/TFMessage``."""

    class _Time:
        def __init__(self, s: float) -> None:
            self.sec = int(s)
            self.nanosec = int((s - int(s)) * 1e9)

    class _Header:
        def __init__(self, parent_id: str, s: float) -> None:
            self.frame_id = parent_id
            self.stamp = _Time(s)

    class _Transform:
        def __init__(self, parent_id: str, child_id: str, s: float) -> None:
            self.header = _Header(parent_id, s)
            self.child_frame_id = child_id

    class _Msg:
        def __init__(self) -> None:
            self.transforms = [_Transform(parent, child, stamp_sec)]

    return _Msg()


class TestTfSnapshotterRclpyShell:
    """Wire-level tests against a fake rclpy node."""

    def _build(self, *, observer: bool = False) -> tuple[TfSnapshotter, _FakeNode, EventBus]:
        bus = EventBus()
        cfg = BlackBoxConfig()
        if observer:
            cfg.runtime = RuntimeConfig(role="observer", observed_host="go2-edu-01")
            cfg.apply_runtime_role()
        node = _FakeNode()
        snap = TfSnapshotter(
            node=node,
            event_bus=bus,
            ros_config=cfg.ros_monitor,
            clock=lambda: node.now_sec,
        )
        return snap, node, bus

    def test_subscribes_to_tf_and_tf_static(self):
        snap, node, _ = self._build()
        snap.start()
        topics = sorted(s.topic for s in node.subscriptions)
        assert topics == ["/tf", "/tf_static"]

    def test_tf_static_qos_is_reliable_transient_local_depth_1(self):
        snap, node, _ = self._build()
        snap.start()
        static_sub = next(s for s in node.subscriptions if s.topic == "/tf_static")
        qos = static_sub.qos
        # We check structural attributes so the test does not depend on
        # importing rclpy enums in CI.
        assert qos.reliability_name == "RELIABLE"
        assert qos.durability_name == "TRANSIENT_LOCAL"
        assert qos.depth == 1

    def test_tf_dynamic_qos_is_reliable_volatile_depth_100(self):
        snap, node, _ = self._build()
        snap.start()
        dyn_sub = next(s for s in node.subscriptions if s.topic == "/tf")
        qos = dyn_sub.qos
        assert qos.reliability_name == "RELIABLE"
        assert qos.durability_name == "VOLATILE"
        assert qos.depth == 100

    def test_dynamic_message_updates_state_and_snapshot_emits_event(self):
        snap, node, bus = self._build()
        q = bus.subscribe()
        snap.start()

        dyn_sub = next(s for s in node.subscriptions if s.topic == "/tf")
        node.now_sec = 100.0
        dyn_sub.callback(_make_tf_msg("odom", "base_link", stamp_sec=99.7))

        node.now_sec = 100.5
        snap.emit_snapshot()

        events = _drain(q)
        tf_events = [e for e in events if e.event_type == "ros.tf"]
        assert len(tf_events) == 1
        ev = tf_events[0]
        assert ev.source == "ros_monitor"
        assert ev.data["edges"] == [
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": pytest.approx(0.8, abs=1e-6),
                "is_static": False,
            }
        ]

    def test_static_message_routes_through_static_subscription(self):
        snap, node, bus = self._build()
        q = bus.subscribe()
        snap.start()

        static_sub = next(s for s in node.subscriptions if s.topic == "/tf_static")
        node.now_sec = 50.0
        static_sub.callback(_make_tf_msg("base_link", "imu_link", stamp_sec=10.0))

        snap.emit_snapshot()
        ev = [e for e in _drain(q) if e.event_type == "ros.tf"][0]
        assert ev.data["edges"] == [
            {
                "parent": "base_link",
                "child": "imu_link",
                "last_update_sec_ago": 0.0,
                "is_static": True,
            }
        ]

    def test_expected_frames_from_config_appear_in_payload(self):
        bus = EventBus()
        cfg = BlackBoxConfig()
        cfg.ros_monitor.tf = TfProducerConfig(
            expected_frames=["base_link", "odom", "map"]
        )
        node = _FakeNode()
        snap = TfSnapshotter(
            node=node,
            event_bus=bus,
            ros_config=cfg.ros_monitor,
            clock=lambda: node.now_sec,
        )
        q = bus.subscribe()
        snap.start()
        snap.emit_snapshot()
        ev = [e for e in _drain(q) if e.event_type == "ros.tf"][0]
        assert ev.data["expected_frames"] == ["base_link", "odom", "map"]

    def test_works_in_observer_mode_subscriptions_still_created(self):
        """Observer mode must not disable the TF producer; ``/tf`` and
        ``/tf_static`` are DDS topics reachable from a workstation.
        """
        snap, node, bus = self._build(observer=True)
        snap.start()
        assert {s.topic for s in node.subscriptions} == {"/tf", "/tf_static"}

    def test_timer_period_matches_snapshot_hz(self):
        bus = EventBus()
        cfg = BlackBoxConfig()
        cfg.ros_monitor.tf = TfProducerConfig(snapshot_hz=2.0)
        node = _FakeNode()
        snap = TfSnapshotter(
            node=node,
            event_bus=bus,
            ros_config=cfg.ros_monitor,
            clock=lambda: node.now_sec,
        )
        snap.start()
        assert len(node.timers) == 1
        period, _cb = node.timers[0]
        assert period == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Config additions
# ---------------------------------------------------------------------------


class TestTfProducerConfig:
    def test_defaults_match_spec(self):
        cfg = TfProducerConfig()
        assert cfg.snapshot_hz == 1.0
        assert cfg.expected_frames == []
        assert cfg.gc_age_sec == 60.0

    def test_ros_monitor_config_carries_tf_section(self):
        rmc = RosMonitorConfig()
        assert isinstance(rmc.tf, TfProducerConfig)
