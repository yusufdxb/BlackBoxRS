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


class TestEventBusBackpressure:
    """Bounded-queue / drop-on-full regression tests.

    Earlier revisions constructed every subscriber queue as ``Queue()``
    (maxsize=0, i.e. unbounded), so the "full queue" drop path documented
    on ``publish()`` was unreachable.  A slow subscriber would quietly
    balloon memory instead of dropping events.  These tests lock in the
    bounded-queue contract: each subscription has a finite capacity and
    excess events are dropped + counted, not queued.
    """

    def test_subscribe_respects_default_queue_maxsize(
        self, sample_ros_event: BlackBoxEvent
    ):
        bus = EventBus(default_queue_maxsize=4)
        q = bus.subscribe()
        assert q.maxsize == 4

    def test_subscribe_maxsize_override(self, sample_ros_event: BlackBoxEvent):
        bus = EventBus(default_queue_maxsize=4)
        q = bus.subscribe(maxsize=16)
        assert q.maxsize == 16

    def test_publish_drops_excess_events_on_full_queue(
        self, sample_ros_event: BlackBoxEvent
    ):
        bus = EventBus(default_queue_maxsize=1)
        q = bus.subscribe()  # capacity 1

        # Before the fix this grew the queue to 1000 entries and the
        # "dropped" counter did not exist.
        for _ in range(1000):
            bus.publish(sample_ros_event)

        assert q.qsize() == 1
        assert bus.dropped_count(q) == 999

    def test_publish_does_not_block_main_thread(
        self, sample_ros_event: BlackBoxEvent
    ):
        """publish() must be non-blocking even if no one is draining."""
        import time

        bus = EventBus(default_queue_maxsize=2)
        bus.subscribe()

        start = time.monotonic()
        for _ in range(500):
            bus.publish(sample_ros_event)
        elapsed = time.monotonic() - start

        # 500 non-blocking put_nowait calls should comfortably complete
        # under 1 second even on a cold CI machine.  A blocking put
        # would hang forever here.
        assert elapsed < 1.0

    def test_dropped_count_is_zero_for_unsaturated_queue(
        self, sample_ros_event: BlackBoxEvent
    ):
        bus = EventBus(default_queue_maxsize=8)
        q = bus.subscribe()
        for _ in range(3):
            bus.publish(sample_ros_event)
        assert bus.dropped_count(q) == 0

    def test_unsubscribe_clears_drop_counter(
        self, sample_ros_event: BlackBoxEvent
    ):
        bus = EventBus(default_queue_maxsize=1)
        q = bus.subscribe()
        bus.publish(sample_ros_event)
        bus.publish(sample_ros_event)  # dropped
        assert bus.dropped_count(q) == 1
        bus.unsubscribe(q)
        # After unsubscribe, counter for this queue should be forgotten.
        assert bus.dropped_count(q) == 0

    def test_invalid_maxsize_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            EventBus(default_queue_maxsize=0)
        bus = EventBus()
        with pytest.raises(ValueError):
            bus.subscribe(maxsize=0)
