"""Core ROS 2 monitor for BlackBoxRS.

Runs as a background rclpy node that periodically polls the ROS 2
computation graph, auto-subscribes to discovered topics, tracks
message frequencies, and emits ``BlackBoxEvent`` instances to the
shared event bus.

When ``rclpy`` is not installed the monitor logs a warning and
becomes a no-op so that BlackBoxRS can still function for
system-only monitoring.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from threading import Thread
from typing import Any

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.config import RosMonitorConfig
from blackboxrs.core.session import Session
from blackboxrs.ros_monitor.frequency_tracker import FrequencyTracker
from blackboxrs.ros_monitor.introspection import GraphSnapshot, RosIntrospector
from blackboxrs.ros_monitor.tf_snapshotter import TfSnapshotter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded rclpy import
# ---------------------------------------------------------------------------
try:
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import (
        QoSDurabilityPolicy,
        QoSHistoryPolicy,
        QoSProfile,
        QoSReliabilityPolicy,
    )

    _RCLPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RCLPY_AVAILABLE = False
    rclpy = None  # type: ignore[assignment]
    Node = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Best-effort QoS for generic subscriptions
# ---------------------------------------------------------------------------
_BEST_EFFORT_QOS = None
if _RCLPY_AVAILABLE:
    _BEST_EFFORT_QOS = QoSProfile(
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        durability=QoSDurabilityPolicy.VOLATILE,
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
    )

# Default node name used on the ROS 2 graph.
_NODE_NAME = "blackbox_ros_monitor"
_NODE_NAMESPACE = "/blackbox"


class RosMonitor:
    """ROS 2 node that observes the graph, tracks frequencies,
    and publishes ``BlackBoxEvent`` objects to the event bus.

    The monitor is designed to run in its own thread (see :meth:`run`).
    Call :meth:`start` / :meth:`stop` for lifecycle management.

    Args:
        event_bus: Shared event bus for publishing events.
        config: ``RosMonitorConfig`` section from the global config.
        session: Current ``Session`` instance for metadata tagging.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: RosMonitorConfig,
        session: Session,
        native_frequency_bridge: bool = False,
    ) -> None:
        self._event_bus = event_bus
        self._config = config
        self._session = session
        self._native_frequency_bridge = native_frequency_bridge
        self._frequency_period_sec = max(config.poll_interval_sec / 2.0, 1.0)
        self._python_frequency_fallback_requested = False
        self._python_frequency_fallback_reason: str | None = None
        self._frequency_fallback_count = 0
        self._frequency_fallback_failures = 0

        self._node: Node | None = None  # type: ignore[assignment]
        self._executor: Any = None
        self._introspector: RosIntrospector | None = None
        self._freq_tracker = FrequencyTracker()
        self._subscriptions: dict[str, Any] = {}
        self._poll_timer: Any = None
        self._freq_timer: Any = None
        self._tf_snapshotter: TfSnapshotter | None = None
        self._running = False
        self._thread: Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Initialise the rclpy node and begin monitoring.

        If ``rclpy`` is not available, a warning is logged and the
        monitor stays inactive (no-op).
        """
        if not _RCLPY_AVAILABLE:
            logger.warning(
                "rclpy is not installed -- ROS 2 monitoring is disabled"
            )
            return

        if not self._config.enabled:
            logger.info("ROS monitor disabled by configuration")
            return

        try:
            if not rclpy.ok():
                rclpy.init()
        except Exception:  # noqa: BLE001
            # rclpy.init() may already have been called by the host process.
            try:
                rclpy.init()
            except RuntimeError:
                pass

        self._node = rclpy.create_node(
            _NODE_NAME, namespace=_NODE_NAMESPACE
        )
        self._introspector = RosIntrospector(self._node)
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)

        poll_sec = self._config.poll_interval_sec
        self._poll_timer = self._node.create_timer(
            poll_sec, self._poll_graph
        )

        # Emit frequency events at half the poll interval (at least 1 s).
        if not self._native_frequency_bridge:
            self._freq_timer = self._node.create_timer(
                self._frequency_period_sec, self._emit_frequency_events
            )

        # Start the TF snapshot producer. Failures are non-fatal so a
        # missing tf2_msgs install or a strange node clock cannot take
        # the whole ROS monitor offline.
        try:
            node_clock = self._node.get_clock()
            self._tf_snapshotter = TfSnapshotter(
                node=self._node,
                event_bus=self._event_bus,
                ros_config=self._config,
                clock=lambda: node_clock.now().nanoseconds * 1e-9,
                session_metadata=self._session.metadata(),
            )
            self._tf_snapshotter.start()
        except Exception:  # noqa: BLE001
            logger.warning("TF snapshotter failed to start", exc_info=True)
            self._tf_snapshotter = None

        self._running = True
        self._thread = Thread(
            target=self._spin_loop,
            name="blackbox-ros-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "ROS monitor started (poll=%.1fs, frequency_source=%s, node=%s%s)",
            poll_sec,
            "native_cpp" if self._native_frequency_bridge else "python",
            _NODE_NAMESPACE,
            f"/{_NODE_NAME}",
        )

    def enable_python_frequency_fallback(self, reason: str) -> bool:
        """Request one-way native-to-Python frequency failover.

        The native watchdog runs outside the rclpy executor. This method only
        records the request; ``_poll_graph`` creates subscriptions and the
        timer on the executor thread.
        """
        with self._lock:
            if (
                not self._native_frequency_bridge
                or self._python_frequency_fallback_requested
            ):
                return False
            self._python_frequency_fallback_requested = True
            self._python_frequency_fallback_reason = reason
        logger.warning("Python ROS frequency fallback requested: %s", reason)
        return True

    def stop(self) -> None:
        """Cleanly shut down the node and release resources."""
        if not self._running and self._thread is None:
            return

        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._poll_timer is not None:
            self._poll_timer.cancel()
            self._poll_timer = None

        if self._freq_timer is not None:
            self._freq_timer.cancel()
            self._freq_timer = None

        if self._tf_snapshotter is not None:
            try:
                self._tf_snapshotter.stop()
            except Exception:  # noqa: BLE001
                logger.debug("TF snapshotter stop failed", exc_info=True)
            self._tf_snapshotter = None

        with self._lock:
            self._subscriptions.clear()

        if self._node is not None:
            self._node.destroy_node()
            self._node = None

        if self._executor is not None:
            self._executor.shutdown()
            self._executor = None

        self._introspector = None
        logger.info("ROS monitor stopped")

    def _spin_loop(self) -> None:
        """Background loop: spin the rclpy executor until stopped."""
        if self._executor is None:
            return

        try:
            while self._running:
                self._executor.spin_once(timeout_sec=0.5)
        except Exception:  # noqa: BLE001
            logger.exception("ROS monitor run loop exited with an error")

    # -- graph polling ------------------------------------------------------

    def _poll_graph(self) -> None:
        """Periodic callback: discover topics, update subscriptions."""
        self._apply_python_frequency_fallback()
        if self._introspector is None:
            return

        try:
            snapshot = self._introspector.snapshot()
        except Exception:  # noqa: BLE001
            logger.warning("Graph snapshot failed", exc_info=True)
            return

        # Emit QoS / topology events
        self._emit_topology_event(snapshot)
        self._emit_qos_events(snapshot)

        # Subscribe to any newly-discovered topics
        live_topics: set[str] = set()
        for topic_info in snapshot.topics:
            topic = topic_info.name
            live_topics.add(topic)
            if self._native_frequency_bridge:
                continue
            if topic in self._subscriptions:
                continue
            if not self._topic_allowed(topic):
                continue
            self._subscribe_topic(topic, topic_info.msg_type)

        # Drop subscriptions for topics that have left the graph.  Without
        # this step the monitor would keep leaked rclpy subscription
        # objects plus stale frequency windows for publishers that no
        # longer exist, which is bad for long-running recorders that outlive
        # many nodes.
        self._prune_stale_subscriptions(
            set() if self._native_frequency_bridge else live_topics
        )

    def _apply_python_frequency_fallback(self) -> None:
        """Apply a pending failover on the rclpy executor thread exactly once."""
        with self._lock:
            if not self._python_frequency_fallback_requested:
                return
            reason = self._python_frequency_fallback_reason or "native_rate_bridge_lost"

        try:
            if self._node is not None and self._freq_timer is None:
                self._freq_timer = self._node.create_timer(
                    self._frequency_period_sec, self._emit_frequency_events
                )
        except Exception:  # noqa: BLE001
            with self._lock:
                self._frequency_fallback_failures += 1
                failures = self._frequency_fallback_failures
            logger.exception("Could not create Python frequency fallback timer")
            event = BlackBoxEvent.ros_event(
                event_type="ros.frequency_fallback_failed",
                data={
                    "state": "ACTIVATION_FAILED",
                    "reason": reason,
                    "frequency_source": "unavailable",
                    "activation_failures": failures,
                },
                severity="error",
                **self._session.metadata(),
            )
            self._event_bus.publish(event)
            return

        with self._lock:
            self._python_frequency_fallback_requested = False
            self._python_frequency_fallback_reason = None
            self._native_frequency_bridge = False
            self._frequency_fallback_count += 1
        event = BlackBoxEvent.ros_event(
            event_type="ros.frequency_fallback",
            data={
                "state": "ACTIVE",
                "reason": reason,
                "frequency_source": "python",
                "fallback_count": self._frequency_fallback_count,
            },
            severity="warning",
            **self._session.metadata(),
        )
        self._event_bus.publish(event)
        logger.warning("Python ROS frequency fallback active: %s", reason)

    def _prune_stale_subscriptions(self, live_topics: set[str]) -> None:
        """Drop subscriptions whose topics are no longer on the graph.

        Args:
            live_topics: Set of topic names observed in the latest
                graph snapshot.  Any subscription we hold for a topic
                outside this set is considered stale.

        The rclpy ``destroy_subscription`` call is wrapped in a guard
        so a single destruction failure cannot leave the monitor in an
        inconsistent state: even if destruction raises, the subscription
        is still removed from our bookkeeping and the frequency tracker
        forgets the topic.
        """
        with self._lock:
            stale = [t for t in self._subscriptions if t not in live_topics]
            if not stale:
                return

            for topic in stale:
                sub = self._subscriptions.pop(topic, None)
                if sub is not None and self._node is not None:
                    try:
                        self._node.destroy_subscription(sub)
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "Failed to destroy subscription for %s during prune",
                            topic,
                            exc_info=True,
                        )
                self._freq_tracker.forget(topic)
                logger.debug("Pruned stale subscription for %s", topic)

    # -- subscriptions ------------------------------------------------------

    def _subscribe_topic(self, topic: str, msg_type_str: str) -> None:
        """Create a generic subscription to *topic*.

        Uses a raw / best-effort QoS profile so the monitor can
        receive messages regardless of publisher settings.
        """
        if self._node is None:
            return

        try:
            # Attempt to resolve the message type at runtime.
            msg_class = self._resolve_msg_type(msg_type_str)
            if msg_class is None:
                logger.debug(
                    "Cannot resolve message type %s for %s -- skipping",
                    msg_type_str,
                    topic,
                )
                return

            sub = self._node.create_subscription(
                msg_class,
                topic,
                lambda msg, t=topic: self._on_message(t, msg),
                qos_profile=_BEST_EFFORT_QOS,
            )
            with self._lock:
                self._subscriptions[topic] = sub
            logger.debug("Subscribed to %s [%s]", topic, msg_type_str)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to subscribe to %s", topic, exc_info=True
            )

    @staticmethod
    def _resolve_msg_type(msg_type_str: str) -> type | None:
        """Dynamically import a ROS 2 message class from its type string.

        Args:
            msg_type_str: Type string in ``pkg/msg/Type`` format
                (e.g. ``std_msgs/msg/String``).

        Returns:
            The message class, or ``None`` if it cannot be resolved.
        """
        parts = msg_type_str.split("/")
        if len(parts) != 3:
            return None

        package, interface, name = parts
        try:
            module = __import__(
                f"{package}.{interface}", fromlist=[name]
            )
            return getattr(module, name, None)
        except (ImportError, ModuleNotFoundError):
            return None

    # -- message handling ---------------------------------------------------

    def _on_message(self, topic: str, msg: Any) -> None:
        """Generic callback invoked for every received message.

        Records the arrival in the frequency tracker.  Heavy
        per-message event emission is intentionally avoided to keep
        overhead low -- frequency / latency events are emitted on a
        timer instead.
        """
        self._freq_tracker.record(topic)

    # -- event emission -----------------------------------------------------

    def _emit_topology_event(self, snapshot: GraphSnapshot) -> None:
        """Publish a ``ros.topology`` event with the current graph."""
        event = BlackBoxEvent.ros_event(
            event_type="ros.topology",
            data={
                "topic_count": len(snapshot.topics),
                "node_count": len(snapshot.node_names),
                "topics": [t.name for t in snapshot.topics],
                "nodes": snapshot.node_names,
            },
            severity="info",
            **self._session.metadata(),
        )
        self._event_bus.publish(event)

    def _emit_frequency_events(self) -> None:
        """Emit ``ros.frequency`` events for all tracked topics."""
        frequencies = self._freq_tracker.get_all_frequencies()
        if not frequencies:
            return

        for topic, hz in frequencies.items():
            latency_ms = self._freq_tracker.get_latency_estimate(topic)
            data: dict[str, Any] = {"topic": topic, "frequency_hz": round(hz, 2)}
            if self._config.track_latency and latency_ms is not None:
                data["interval_ms"] = round(latency_ms, 2)

            event = BlackBoxEvent.ros_event(
                event_type="ros.frequency",
                data=data,
                severity="info",
                **self._session.metadata(),
            )
            self._event_bus.publish(event)

    def _emit_qos_events(self, snapshot: GraphSnapshot) -> None:
        """Emit ``ros.qos`` events carrying QoS profiles per topic.

        Each event contains separate ``publisher_qos_profiles`` and
        ``subscriber_qos_profiles`` lists so downstream consumers (e.g.
        the QoS-mismatch detector) can compare each pub/sub pair.
        """
        for topic_info in snapshot.topics:
            if not (topic_info.publisher_qos_profiles or topic_info.subscriber_qos_profiles):
                continue

            event = BlackBoxEvent.ros_event(
                event_type="ros.qos",
                data={
                    "topic": topic_info.name,
                    "msg_type": topic_info.msg_type,
                    "publisher_count": topic_info.publisher_count,
                    "subscriber_count": topic_info.subscriber_count,
                    "publisher_qos_profiles": topic_info.publisher_qos_profiles,
                    "subscriber_qos_profiles": topic_info.subscriber_qos_profiles,
                },
                severity="debug",
                **self._session.metadata(),
            )
            self._event_bus.publish(event)

    # -- helpers ------------------------------------------------------------

    def _topic_allowed(self, topic: str) -> bool:
        """Return ``True`` if *topic* passes the configured filters.

        An empty ``topic_filters`` list means "allow all".  Each filter
        entry is matched using Unix shell-style wildcards via
        :func:`fnmatch.fnmatch`.

        Internal ROS 2 topics (starting with ``/rosout``, ``/parameter_events``,
        or within the ``/blackbox`` namespace) are always excluded.
        """
        # Always skip ROS 2 infrastructure topics
        _INTERNAL_PREFIXES = ("/rosout", "/parameter_events", f"{_NODE_NAMESPACE}/")
        if any(topic.startswith(p) for p in _INTERNAL_PREFIXES):
            return False

        filters: list[str] = self._config.topic_filters
        if not filters:
            return True

        return any(fnmatch.fnmatch(topic, f) for f in filters)
