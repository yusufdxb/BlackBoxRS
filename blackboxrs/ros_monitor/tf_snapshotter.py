"""TF snapshot producer for BlackBoxRS.

Subscribes to ``/tf`` and ``/tf_static`` on a real ``rclpy`` node,
maintains a last-seen map per ``(parent_frame_id, child_frame_id)``
edge, and emits one ``ros.tf`` :class:`BlackBoxEvent` per snapshot
tick. The downstream :class:`TfTopologyDetector` consumes those events
to fire ``orphan_frame``, ``multi_parent``, and ``stale_edge``
anomalies.

The producer is split into two layers so the bookkeeping is testable
without ``rclpy``:

* :class:`TfSnapshotState` is the pure, in-memory state that records
  edges and builds the snapshot payload.
* :class:`TfSnapshotter` wires the state to an ``rclpy`` node by
  creating subscriptions with the QoS profiles required by the spec
  (``/tf``: RELIABLE + VOLATILE depth 100; ``/tf_static``: RELIABLE +
  TRANSIENT_LOCAL depth 1).

Observer mode is fully supported: ``/tf`` and ``/tf_static`` are
ordinary DDS topics, so a workstation reaching the robot over DDS can
subscribe with no on-robot install. The ``TRANSIENT_LOCAL`` durability
on ``/tf_static`` is the load-bearing detail that lets late observer
attaches still receive the static edges.

See ``docs/design/orphan_detector_producers.md`` for the full
contract.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from blackboxrs.core.config import RosMonitorConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded rclpy import
# ---------------------------------------------------------------------------
try:
    from rclpy.qos import (
        QoSDurabilityPolicy,
        QoSHistoryPolicy,
        QoSProfile,
        QoSReliabilityPolicy,
    )

    _RCLPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RCLPY_AVAILABLE = False
    QoSProfile = None  # type: ignore[assignment,misc]


def _stamp_to_sec(stamp: Any) -> float:
    """Convert a ROS 2 ``builtin_interfaces/Time`` to float seconds."""
    sec = float(getattr(stamp, "sec", 0))
    nsec = float(getattr(stamp, "nanosec", 0))
    return sec + nsec * 1e-9


# ---------------------------------------------------------------------------
# QoS profile shims
# ---------------------------------------------------------------------------


class _NamedQoS:
    """A QoS descriptor that carries human-readable names for assertions
    and converts to a real ``rclpy.qos.QoSProfile`` when ``rclpy`` is
    available.

    The bookkeeping shell never inspects the names; they exist so the
    unit tests can verify the producer requested the right policy
    without forcing ``rclpy`` to be importable in CI.
    """

    def __init__(
        self,
        *,
        reliability_name: str,
        durability_name: str,
        depth: int,
    ) -> None:
        self.reliability_name = reliability_name
        self.durability_name = durability_name
        self.depth = depth

    def to_rclpy(self) -> Any:
        if not _RCLPY_AVAILABLE:  # pragma: no cover
            return None
        reliability = {
            "RELIABLE": QoSReliabilityPolicy.RELIABLE,
            "BEST_EFFORT": QoSReliabilityPolicy.BEST_EFFORT,
        }[self.reliability_name]
        durability = {
            "VOLATILE": QoSDurabilityPolicy.VOLATILE,
            "TRANSIENT_LOCAL": QoSDurabilityPolicy.TRANSIENT_LOCAL,
        }[self.durability_name]
        return QoSProfile(
            reliability=reliability,
            durability=durability,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=self.depth,
        )


_TF_DYNAMIC_QOS = _NamedQoS(
    reliability_name="RELIABLE",
    durability_name="VOLATILE",
    depth=100,
)
_TF_STATIC_QOS = _NamedQoS(
    reliability_name="RELIABLE",
    durability_name="TRANSIENT_LOCAL",
    depth=1,
)


# ---------------------------------------------------------------------------
# Pure state
# ---------------------------------------------------------------------------


class TfSnapshotState:
    """In-memory last-seen map keyed by ``(parent, child)`` edge.

    Dedupes multi-publisher ``/tf`` traffic via last-writer-wins on the
    recorded timestamp. Static edges from ``/tf_static`` always report
    ``last_update_sec_ago = 0.0`` and are never garbage-collected, so
    late observer attaches that already received the latched message
    keep the static subtree visible across long sessions.
    """

    def __init__(self) -> None:
        # (parent, child) -> (last_stamp_sec, is_static)
        self._edges: dict[tuple[str, str], tuple[float, bool]] = {}

    def record(
        self,
        parent: str,
        child: str,
        *,
        stamp_sec: float,
        is_static: bool,
    ) -> None:
        """Record one transform observation."""
        key = (parent, child)
        existing = self._edges.get(key)
        if existing is not None and not is_static:
            existing_stamp, _ = existing
            # Last-writer-wins by stamp; ignore out-of-order replays.
            if stamp_sec < existing_stamp:
                return
        self._edges[key] = (float(stamp_sec), bool(is_static))

    def build_payload(
        self,
        *,
        now_sec: float,
        gc_age_sec: float,
        expected_frames: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compute the ``ros.tf`` event payload and GC stale edges.

        Args:
            now_sec: Current node-clock time in float seconds.
            gc_age_sec: Drop non-static edges older than this many
                seconds from the in-memory map so a multi-hour session
                does not grow unbounded.
            expected_frames: Operator-declared frames; included in the
                payload when non-empty so the detector can fire
                ``orphan_frame``.
        """
        # Garbage-collect dynamic edges past gc_age_sec.
        to_drop = [
            key
            for key, (stamp, is_static) in self._edges.items()
            if not is_static and (now_sec - stamp) > gc_age_sec
        ]
        for key in to_drop:
            self._edges.pop(key, None)

        edges_out: list[dict[str, Any]] = []
        for (parent, child), (stamp, is_static) in self._edges.items():
            if is_static:
                age = 0.0
            else:
                raw = now_sec - stamp
                if raw < 0.0:
                    logger.warning(
                        "TF clock rewind on edge %s -> %s "
                        "(stamp=%.3f, now=%.3f); clamping age to 0",
                        parent,
                        child,
                        stamp,
                        now_sec,
                    )
                    age = 0.0
                else:
                    age = raw
            edges_out.append(
                {
                    "parent": parent,
                    "child": child,
                    "last_update_sec_ago": age,
                    "is_static": is_static,
                }
            )

        payload: dict[str, Any] = {"edges": edges_out}
        if expected_frames:
            payload["expected_frames"] = list(expected_frames)
        return payload


# ---------------------------------------------------------------------------
# rclpy integration
# ---------------------------------------------------------------------------


class TfSnapshotter:
    """rclpy-side adapter that wires ``/tf`` and ``/tf_static`` into a
    :class:`TfSnapshotState` and publishes ``ros.tf`` events.

    The producer takes a ready-made node rather than constructing its
    own; the host :class:`~blackboxrs.ros_monitor.monitor.RosMonitor`
    already owns the node lifecycle and executor.

    Args:
        node: A live ``rclpy.node.Node`` (or a stand-in with the same
            shape, used in tests).
        event_bus: Shared :class:`EventBus` to publish snapshots on.
        ros_config: :class:`RosMonitorConfig` carrying the ``tf`` sub
            config.
        clock: A callable returning float seconds, defaults to
            ``time.monotonic``. In production this should be the
            node-clock time so ``/clock`` is honored during bag
            playback; pass ``lambda: node.get_clock().now().nanoseconds
            * 1e-9`` from the wiring layer.
        session_metadata: Optional dict merged into every emitted event's
            metadata.
    """

    def __init__(
        self,
        *,
        node: Any,
        event_bus: EventBus,
        ros_config: RosMonitorConfig,
        clock: Callable[[], float] | None = None,
        session_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._node = node
        self._bus = event_bus
        self._tf_cfg = ros_config.tf
        self._clock = clock or time.monotonic
        self._session_metadata = dict(session_metadata or {})

        self._state = TfSnapshotState()
        self._timer: Any = None
        self._dyn_sub: Any = None
        self._static_sub: Any = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Create subscriptions and the snapshot timer.

        Imports of ``tf2_msgs`` are deferred so the module stays
        import-safe on hosts without ROS 2; tests pass a fake node and
        skip the import entirely.
        """
        msg_type = _resolve_tf_message_type()

        dyn_qos = _qos_for(self._node, _TF_DYNAMIC_QOS)
        static_qos = _qos_for(self._node, _TF_STATIC_QOS)

        self._dyn_sub = self._node.create_subscription(
            msg_type,
            "/tf",
            self._on_dynamic,
            dyn_qos,
        )
        self._static_sub = self._node.create_subscription(
            msg_type,
            "/tf_static",
            self._on_static,
            static_qos,
        )

        hz = float(self._tf_cfg.snapshot_hz)
        period = 1.0 / hz if hz > 0 else 1.0
        self._timer = self._node.create_timer(period, self.emit_snapshot)

        logger.info(
            "TF snapshotter started (snapshot_hz=%.2f, expected_frames=%d)",
            hz,
            len(self._tf_cfg.expected_frames),
        )

    def stop(self) -> None:
        """Tear down subscriptions and the timer."""
        for handle in (self._dyn_sub, self._static_sub):
            if handle is None:
                continue
            try:
                self._node.destroy_subscription(handle)
            except Exception:  # noqa: BLE001  # pragma: no cover
                logger.debug("destroy_subscription failed", exc_info=True)
        if self._timer is not None:
            try:
                self._node.destroy_timer(self._timer)
            except Exception:  # noqa: BLE001  # pragma: no cover
                logger.debug("destroy_timer failed", exc_info=True)
        self._dyn_sub = None
        self._static_sub = None
        self._timer = None

    # -- callbacks ----------------------------------------------------------

    def _on_dynamic(self, msg: Any) -> None:
        self._ingest(msg, is_static=False)

    def _on_static(self, msg: Any) -> None:
        self._ingest(msg, is_static=True)

    def _ingest(self, msg: Any, *, is_static: bool) -> None:
        transforms = getattr(msg, "transforms", None) or []
        for tf in transforms:
            header = getattr(tf, "header", None)
            parent = getattr(header, "frame_id", None) if header else None
            child = getattr(tf, "child_frame_id", None)
            if not parent or not child:
                continue
            stamp = getattr(header, "stamp", None)
            stamp_sec = _stamp_to_sec(stamp) if stamp is not None else self._clock()
            self._state.record(
                parent, child, stamp_sec=stamp_sec, is_static=is_static
            )

    # -- snapshot -----------------------------------------------------------

    def emit_snapshot(self) -> None:
        """Build a payload from current state and publish it."""
        payload = self._state.build_payload(
            now_sec=self._clock(),
            gc_age_sec=float(self._tf_cfg.gc_age_sec),
            expected_frames=list(self._tf_cfg.expected_frames),
        )
        event = BlackBoxEvent.ros_event(
            event_type="ros.tf",
            data=payload,
            **self._session_metadata,
        )
        self._bus.publish(event)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _qos_for(node: Any, named: _NamedQoS) -> Any:
    """Return the QoS object to hand to ``create_subscription``.

    A real ``rclpy`` node receives a real ``QoSProfile``; a fake node
    (tests) receives the ``_NamedQoS`` itself so the test can assert
    on the requested policy without importing rclpy.
    """
    if _is_real_rclpy_node(node):  # pragma: no cover  (real rclpy not in CI)
        return named.to_rclpy()
    return named


def _is_real_rclpy_node(node: Any) -> bool:
    """Heuristic: real rclpy nodes expose ``get_clock`` and ``context``."""
    return hasattr(node, "get_clock") and hasattr(node, "context")


def _resolve_tf_message_type() -> Any:
    """Import ``tf2_msgs/msg/TFMessage`` lazily.

    Returns ``None`` if ``tf2_msgs`` isn't importable (tests / no ROS).
    The fake nodes in tests don't care about the actual message class.
    """
    try:  # pragma: no cover  (tf2_msgs not present in CI)
        from tf2_msgs.msg import TFMessage

        return TFMessage
    except ImportError:
        return None
