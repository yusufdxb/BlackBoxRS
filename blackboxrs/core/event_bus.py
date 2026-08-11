"""Thread-safe event bus for BlackBoxRS.

The :class:`EventBus` decouples event producers (monitors, detectors) from
consumers (loggers, dashboard, anomaly engine) using an in-process
publish/subscribe model backed by bounded :class:`queue.Queue` instances.

Backpressure contract
---------------------
Every subscriber is given a *bounded* queue.  When a queue is full,
:meth:`EventBus.publish` drops the event for that subscriber, increments
a per-queue drop counter, and logs a rate-limited warning.  Producers
never block on a slow consumer.
"""

from __future__ import annotations

import logging
import threading
from queue import Full, Queue

from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)

#: Default bounded queue capacity applied to new subscriptions when the
#: caller does not specify ``maxsize`` explicitly.
DEFAULT_QUEUE_MAXSIZE = 1024


class EventBus:
    """In-process pub/sub event bus with bounded subscriber queues.

    Subscribers receive events through dedicated :class:`~queue.Queue`
    instances.  A subscriber can listen to a specific channel (matched
    against :attr:`BlackBoxEvent.source`) or to *all* events by
    subscribing with ``channel=None``.

    All public methods are thread-safe.

    Args:
        default_queue_maxsize: Default capacity applied to every new
            subscription when the caller does not pass ``maxsize=``
            explicitly.  Must be >= 1 (0 would mean unbounded and
            defeat the backpressure guarantee).
    """

    def __init__(
        self, default_queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE
    ) -> None:
        if default_queue_maxsize < 1:
            raise ValueError(
                "default_queue_maxsize must be >= 1 (0 means unbounded "
                f"which violates the backpressure contract); got {default_queue_maxsize}"
            )
        self._default_queue_maxsize = default_queue_maxsize
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Queue[BlackBoxEvent]]] = {}
        self._global_subscribers: list[Queue[BlackBoxEvent]] = []
        # Drop counts keyed by id(queue) so we can look up by the queue
        # reference without requiring it to be hashable for dict keys.
        self._dropped_counts: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    @property
    def default_queue_maxsize(self) -> int:
        """Return the default queue capacity for new subscriptions."""
        return self._default_queue_maxsize

    def subscribe(
        self,
        channel: str | None = None,
        maxsize: int | None = None,
    ) -> Queue[BlackBoxEvent]:
        """Create a new bounded subscription and return its queue.

        Args:
            channel: Event source channel to listen on (e.g.
                ``"ros_monitor"``).  Pass ``None`` to receive every event
                regardless of source.
            maxsize: Override the bus-wide default queue capacity for
                this subscription.  Must be >= 1.

        Returns:
            A bounded :class:`~queue.Queue` that will receive matching
            :class:`BlackBoxEvent` instances as they are published.
        """
        size = self._default_queue_maxsize if maxsize is None else maxsize
        if size < 1:
            raise ValueError(
                "subscriber queue maxsize must be >= 1 (0 means unbounded "
                f"which violates the backpressure contract); got {size}"
            )
        q: Queue[BlackBoxEvent] = Queue(maxsize=size)
        with self._lock:
            if channel is None:
                self._global_subscribers.append(q)
            else:
                self._subscribers.setdefault(channel, []).append(q)
            self._dropped_counts[id(q)] = 0
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
            self._dropped_counts.pop(id(queue), None)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, event: BlackBoxEvent) -> None:
        """Publish an event to all matching subscribers.

        The event is delivered to:

        1. Every global (channel-less) subscriber.
        2. Every queue subscribed to ``event.source`` as a channel.

        Delivery is strictly non-blocking.  If a subscriber's queue is
        full the event is **dropped** for that subscriber only, the
        subscriber's drop counter is incremented, and a rate-limited
        warning is logged.  Other subscribers continue to receive the
        event normally.

        Args:
            event: The event to publish.
        """
        with self._lock:
            targets: list[Queue[BlackBoxEvent]] = list(self._global_subscribers)
            targets.extend(self._subscribers.get(event.source, []))

        for q in targets:
            try:
                q.put_nowait(event)
            except Full:
                total = self._record_drop(q)
                # Log the first drop and then every 100th to avoid log
                # flooding when a consumer falls far behind.
                if total == 1 or total % 100 == 0:
                    logger.warning(
                        "EventBus: subscriber queue full (capacity=%d): "
                        "dropped %s event (total drops on this queue: %d)",
                        q.maxsize,
                        event.event_type,
                        total,
                    )
            except Exception:
                logger.exception(
                    "EventBus: unexpected delivery failure for %s",
                    event.event_type,
                )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def dropped_count(self, queue: Queue[BlackBoxEvent]) -> int:
        """Return the cumulative number of events dropped for *queue*.

        Returns 0 if *queue* is not currently subscribed (either it was
        never registered or it has been unsubscribed).

        Args:
            queue: A queue previously returned by :meth:`subscribe`.

        Returns:
            The total drop count for this subscription since it was
            created.
        """
        with self._lock:
            return self._dropped_counts.get(id(queue), 0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_drop(self, queue: Queue[BlackBoxEvent]) -> int:
        """Increment the drop counter for *queue* and return the new total."""
        key = id(queue)
        with self._lock:
            total = self._dropped_counts.get(key, 0) + 1
            # Only keep the counter if the queue is still registered.
            # (unsubscribe() removes the entry, so a race that deletes
            # the key between lookup and assignment would resurrect it
            # -- guard against that by only writing if the key exists.)
            if key in self._dropped_counts:
                self._dropped_counts[key] = total
        return total
