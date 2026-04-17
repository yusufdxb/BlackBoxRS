"""Unit tests for ``RosMonitor`` topic lifecycle bookkeeping.

These tests deliberately do NOT require rclpy.  They exercise the
churn logic on the ``RosMonitor`` instance directly by:

1. constructing the monitor (which does not touch rclpy),
2. wiring mock ``_node``, ``_introspector``, and pre-populated
   ``_subscriptions`` state,
3. calling ``_poll_graph()`` or ``_prune_stale_subscriptions()``,
4. asserting that subscriptions for topics missing from the new
   snapshot are destroyed, forgotten, and no longer produce
   frequency events.

The live-ROS counterpart (``tests/integration/test_ros_live.py``)
verifies that rclpy actually delivers snapshots; this file verifies
that the monitor reacts correctly to any snapshot it receives.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from blackboxrs.core.config import RosMonitorConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session
from blackboxrs.ros_monitor.introspection import GraphSnapshot, TopicInfo
from blackboxrs.ros_monitor.monitor import RosMonitor


def _make_monitor(
    topic_filters: list[str] | None = None,
) -> tuple[RosMonitor, EventBus, MagicMock]:
    bus = EventBus(default_queue_maxsize=64)
    config = RosMonitorConfig(
        enabled=True,
        poll_interval_sec=0.1,
        track_latency=False,
        topic_filters=topic_filters or [],
    )
    monitor = RosMonitor(bus, config, Session())
    fake_node = MagicMock()
    monitor._node = fake_node  # type: ignore[assignment]
    return monitor, bus, fake_node


def _snapshot(topics: list[str]) -> GraphSnapshot:
    return GraphSnapshot(
        timestamp=datetime.now(timezone.utc),
        topics=[
            TopicInfo(name=t, msg_type="std_msgs/msg/String")
            for t in topics
        ],
        node_names=[],
    )


class TestStaleSubscriptionPruning:
    def test_disappeared_topic_is_destroyed_and_forgotten(self):
        monitor, _, fake_node = _make_monitor()
        sub_a = SimpleNamespace(name="a")
        sub_b = SimpleNamespace(name="b")
        monitor._subscriptions = {"/a": sub_a, "/b": sub_b}
        monitor._freq_tracker.record("/a")
        monitor._freq_tracker.record("/a")
        monitor._freq_tracker.record("/b")
        monitor._freq_tracker.record("/b")

        monitor._prune_stale_subscriptions(live_topics={"/a"})

        assert "/a" in monitor._subscriptions
        assert "/b" not in monitor._subscriptions
        fake_node.destroy_subscription.assert_called_once_with(sub_b)

        # Frequency tracker must forget the pruned topic so no further
        # ros.frequency events get emitted for a dead publisher.
        assert monitor._freq_tracker.get_frequency("/b") is None

    def test_nothing_to_prune_is_a_no_op(self):
        monitor, _, fake_node = _make_monitor()
        monitor._subscriptions = {"/x": SimpleNamespace()}
        monitor._prune_stale_subscriptions(live_topics={"/x"})
        fake_node.destroy_subscription.assert_not_called()
        assert "/x" in monitor._subscriptions

    def test_destroy_failure_still_removes_bookkeeping(self):
        """A broken destroy_subscription() must not leave dangling state."""
        monitor, _, fake_node = _make_monitor()
        fake_node.destroy_subscription.side_effect = RuntimeError("boom")
        monitor._subscriptions = {"/gone": SimpleNamespace()}
        monitor._freq_tracker.record("/gone")
        monitor._freq_tracker.record("/gone")

        monitor._prune_stale_subscriptions(live_topics=set())

        assert "/gone" not in monitor._subscriptions
        assert monitor._freq_tracker.get_frequency("/gone") is None

    def test_node_none_is_tolerated(self):
        """During shutdown the node may already be gone — pruning must
        still clean up bookkeeping."""
        monitor, _, _ = _make_monitor()
        monitor._node = None
        monitor._subscriptions = {"/gone": SimpleNamespace()}
        monitor._prune_stale_subscriptions(live_topics=set())
        assert "/gone" not in monitor._subscriptions


class TestPollGraphPruning:
    def test_poll_graph_prunes_on_churn(self, monkeypatch: pytest.MonkeyPatch):
        """Two back-to-back polls with different graphs: the topic that
        disappeared in the second poll must be unsubscribed."""
        monitor, bus, fake_node = _make_monitor()

        # Pretend subscriptions succeed; return a distinct object per call.
        def fake_create_subscription(*_args, **_kwargs):
            return SimpleNamespace()

        fake_node.create_subscription.side_effect = fake_create_subscription

        # Fake introspector: first call returns /a + /b, second call only /a.
        snapshots = iter([_snapshot(["/a", "/b"]), _snapshot(["/a"])])
        monitor._introspector = SimpleNamespace(
            snapshot=lambda: next(snapshots),
        )

        # Force subscribe_topic to always register a subscription
        # regardless of whether we can resolve the msg type under test.
        monkeypatch.setattr(
            RosMonitor,
            "_resolve_msg_type",
            staticmethod(lambda _s: SimpleNamespace),
        )

        monitor._poll_graph()
        assert set(monitor._subscriptions) == {"/a", "/b"}

        monitor._poll_graph()
        assert set(monitor._subscriptions) == {"/a"}
        # destroy_subscription was called exactly once, for /b.
        assert fake_node.destroy_subscription.call_count == 1

    def test_poll_graph_respects_filter_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """If the config's topic_filters are tightened at runtime, a
        previously-allowed topic still disappears from subscriptions on
        the next poll because it is no longer in `live_topics` — but
        more importantly, a newly-filtered topic that IS still in the
        graph must not be re-subscribed."""
        monitor, _, fake_node = _make_monitor(topic_filters=["/keep/*"])
        fake_node.create_subscription.side_effect = (
            lambda *a, **k: SimpleNamespace()
        )
        monkeypatch.setattr(
            RosMonitor,
            "_resolve_msg_type",
            staticmethod(lambda _s: SimpleNamespace),
        )
        monitor._introspector = SimpleNamespace(
            snapshot=lambda: _snapshot(["/keep/a", "/drop/b"]),
        )

        monitor._poll_graph()
        # /drop/b was filtered out; only /keep/a should be subscribed.
        assert set(monitor._subscriptions) == {"/keep/a"}
        fake_node.destroy_subscription.assert_not_called()


class TestFrequencyTrackerForget:
    def test_forget_removes_topic(self):
        from blackboxrs.ros_monitor.frequency_tracker import FrequencyTracker

        tracker = FrequencyTracker(window_sec=5.0)
        tracker.record("/a")
        tracker.record("/a")
        tracker.record("/b")
        tracker.forget("/a")

        assert tracker.get_frequency("/a") is None
        # /b is untouched.
        assert "/b" in tracker._windows

    def test_forget_unknown_topic_is_noop(self):
        from blackboxrs.ros_monitor.frequency_tracker import FrequencyTracker

        tracker = FrequencyTracker(window_sec=5.0)
        tracker.forget("/never-seen")  # must not raise
