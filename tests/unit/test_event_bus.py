"""Tests for blackboxrs.core.event_bus."""

from __future__ import annotations

from queue import Empty

from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent


class TestEventBusSubscribePublish:
    """Test basic subscribe and publish mechanics."""

    def test_global_subscriber_receives_event(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        queue = event_bus.subscribe()
        event_bus.publish(sample_ros_event)
        received = queue.get(timeout=1)
        assert received.event_type == sample_ros_event.event_type

    def test_channel_subscriber_receives_matching_event(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        queue = event_bus.subscribe(channel="ros_monitor")
        event_bus.publish(sample_ros_event)
        received = queue.get(timeout=1)
        assert received.source == "ros_monitor"

    def test_channel_subscriber_ignores_non_matching_event(
        self, event_bus: EventBus, sample_system_event: BlackBoxEvent
    ):
        queue = event_bus.subscribe(channel="ros_monitor")
        event_bus.publish(sample_system_event)  # source=system_monitor
        try:
            queue.get(timeout=0.1)
            assert False, "Should not have received an event"
        except Empty:
            pass

    def test_global_subscriber_receives_all_sources(
        self,
        event_bus: EventBus,
        sample_ros_event: BlackBoxEvent,
        sample_system_event: BlackBoxEvent,
    ):
        queue = event_bus.subscribe()
        event_bus.publish(sample_ros_event)
        event_bus.publish(sample_system_event)
        events = [queue.get(timeout=1), queue.get(timeout=1)]
        sources = {e.source for e in events}
        assert sources == {"ros_monitor", "system_monitor"}


class TestEventBusUnsubscribe:
    """Test unsubscribe behavior."""

    def test_unsubscribe_global(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        queue = event_bus.subscribe()
        event_bus.unsubscribe(queue)
        event_bus.publish(sample_ros_event)
        try:
            queue.get(timeout=0.1)
            assert False, "Should not have received an event after unsubscribe"
        except Empty:
            pass

    def test_unsubscribe_channel(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        queue = event_bus.subscribe(channel="ros_monitor")
        event_bus.unsubscribe(queue, channel="ros_monitor")
        event_bus.publish(sample_ros_event)
        try:
            queue.get(timeout=0.1)
            assert False, "Should not have received an event after unsubscribe"
        except Empty:
            pass

    def test_unsubscribe_nonexistent_is_silent(self, event_bus: EventBus):
        """Unsubscribing a queue that was never subscribed should not raise."""
        from queue import Queue

        q: Queue[BlackBoxEvent] = Queue()
        event_bus.unsubscribe(q)  # should not raise
        event_bus.unsubscribe(q, channel="ros_monitor")  # should not raise


class TestEventBusMultipleSubscribers:
    """Test that multiple subscribers each get their own copy."""

    def test_multiple_global_subscribers(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        q1 = event_bus.subscribe()
        q2 = event_bus.subscribe()
        event_bus.publish(sample_ros_event)
        assert q1.get(timeout=1).event_type == sample_ros_event.event_type
        assert q2.get(timeout=1).event_type == sample_ros_event.event_type

    def test_mixed_global_and_channel_subscribers(
        self, event_bus: EventBus, sample_ros_event: BlackBoxEvent
    ):
        q_global = event_bus.subscribe()
        q_channel = event_bus.subscribe(channel="ros_monitor")
        event_bus.publish(sample_ros_event)
        assert q_global.get(timeout=1).event_type == sample_ros_event.event_type
        assert q_channel.get(timeout=1).event_type == sample_ros_event.event_type
