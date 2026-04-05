"""Thread-safe event bus for BlackBoxRS.

The :class:`EventBus` decouples event producers (monitors, detectors) from
consumers (loggers, dashboard, anomaly engine) using an in-process
publish/subscribe model backed by :class:`queue.Queue`.
"""

from __future__ import annotations

import logging
import threading
from queue import Queue

from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)


class EventBus:
    """In-process pub/sub event bus.

    Subscribers receive events through dedicated :class:`~queue.Queue`
    instances.  A subscriber can listen to a specific channel (matched
    against :attr:`BlackBoxEvent.source`) or to *all* events by
    subscribing with ``channel=None``.

    All public methods are thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Queue[BlackBoxEvent]]] = {}
        self._global_subscribers: list[Queue[BlackBoxEvent]] = []

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, channel: str | None = None) -> Queue[BlackBoxEvent]:
        """Create a new subscription and return its queue.

        Args:
            channel: Event source channel to listen on (e.g.
                ``"ros_monitor"``).  Pass ``None`` to receive every event
                regardless of source.

        Returns:
            A :class:`~queue.Queue` that will receive matching
            :class:`BlackBoxEvent` instances as they are published.
        """
        q: Queue[BlackBoxEvent] = Queue()
        with self._lock:
            if channel is None:
                self._global_subscribers.append(q)
            else:
                self._subscribers.setdefault(channel, []).append(q)
        return q

    def unsubscribe(
        self, queue: Queue[BlackBoxEvent], channel: str | None = None
    ) -> None:
        """Remove a previously registered subscription.

        Silently does nothing if the queue is not found in the specified
        channel.

        Args:
            queue: The queue returned by :meth:`subscribe`.
            channel: The channel the queue was subscribed to, or ``None``
                for a global subscription.
        """
        with self._lock:
            if channel is None:
                try:
                    self._global_subscribers.remove(queue)
                except ValueError:
                    pass
            else:
                subscribers = self._subscribers.get(channel, [])
                try:
                    subscribers.remove(queue)
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, event: BlackBoxEvent) -> None:
        """Publish an event to all matching subscribers.

        The event is delivered to:

        1. Every queue subscribed to ``event.source`` as a channel.
        2. Every global (channel-less) subscriber.

        Delivery is non-blocking; if a subscriber's queue is full the
        event is dropped for that subscriber and a warning is logged.

        Args:
            event: The event to publish.
        """
        with self._lock:
            targets: list[Queue[BlackBoxEvent]] = list(self._global_subscribers)
            targets.extend(self._subscribers.get(event.source, []))

        for q in targets:
            try:
                q.put_nowait(event)
            except Exception:
                logger.warning(
                    "Failed to deliver event %s to a subscriber queue",
                    event.event_type,
                )
