"""TF (transform tree) topology anomaly detector.

Fires when a snapshot of the ROS 2 ``/tf`` + ``/tf_static`` graph shows
structural problems: a frame referenced by a child has no parent edge
(orphan), the same child has more than one parent (multi-parent), or a
previously fresh edge has not been updated in longer than a configured
timeout (stale). These are the three failure modes that produce the
familiar ``"Could not find a connection between '...' and '...'"``
message in real ROS 2 stacks.

The detector is a pure event-stream consumer in line with every other
detector in this package. The producer (a small TF snapshotter inside
:mod:`blackboxrs.ros_monitor`, or an external bridge in environments
where ``rclpy`` is not importable) emits one ``BlackBoxEvent`` of the
form::

    BlackBoxEvent.ros_event(
        event_type="ros.tf",
        data={
            "expected_frames": ["base_link", "odom", ...],     # optional
            "edges": [
                {"parent": "odom", "child": "base_link",
                 "last_update_sec_ago": 0.05,
                 "is_static": False},
                ...
            ],
        },
    )

``rclpy`` / ``tf2_msgs`` are *not* imported here, so this module is
import-safe in CI and on hosts without ROS 2. The producer side is
responsible for the actual ``tf2`` parsing, exactly like
``QoSMismatchDetector`` consumes ``ros.qos`` events without importing
``rclpy`` itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)


@dataclass
class TfTopologyConfig:
    """Configuration for :class:`TfTopologyDetector`.

    Attributes:
        stale_timeout_sec: Edges whose ``last_update_sec_ago`` exceeds
            this value are flagged stale. Static edges (``is_static =
            True``) are exempt because ``/tf_static`` is published once.
    """

    stale_timeout_sec: float = 5.0


class TfTopologyDetector(BaseDetector):
    """Fires when the TF graph is structurally broken or stale.

    Consumes ``ros_monitor`` events with ``event_type == "ros.tf"``.
    The data payload is interpreted as a *snapshot* of the current TF
    graph; this detector is stateless across snapshots so the producer
    is free to emit at whatever cadence is convenient.

    Three failure classes are detected, in this order of severity. The
    first kind found in a snapshot wins so a single broken graph never
    produces three near-identical anomalies.

    1. **orphan_frame**: a child appears in some edge but no edge lists
       it (or any of its ancestors) as a child of the world root, AND
       it is also listed in ``expected_frames``. Without
       ``expected_frames`` we cannot tell whether an orphan is just a
       leaf the operator does not care about, so we silently skip.
    2. **multi_parent**: the same child appears as the child end of
       more than one edge. tf2 will pick one parent arbitrarily and
       lookups across the conflicting subtree silently return the
       wrong transform.
    3. **stale_edge**: a non-static edge has not been updated for
       longer than ``stale_timeout_sec``.

    Args:
        config: A :class:`TfTopologyConfig` holding ``stale_timeout_sec``.
    """

    #: Identity for fingerprinting. ``failure_kind`` is the dimension of
    #: failure (orphan/multi-parent/stale); ``frame`` and ``parent``
    #: scope it to the actual TF subtree. Two snapshots that show the
    #: same kind of failure on the same edge collide; a different kind
    #: or a different edge yields a distinct fingerprint.
    signature_fields = ["failure_kind", "frame", "parent"]

    target_subsystem = "ros"

    def __init__(self, config: TfTopologyConfig | None = None) -> None:
        cfg = config or TfTopologyConfig()
        self._stale_timeout_sec = cfg.stale_timeout_sec

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "tf_topology"

    # -- Detection ------------------------------------------------------

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Inspect a TF snapshot and emit an anomaly if it is broken.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event for the first failure class found, or
            ``None`` if the snapshot is healthy (or the event isn't a
            TF snapshot).
        """
        if event.source != "ros_monitor" or event.event_type != "ros.tf":
            return None

        edges_raw = event.data.get("edges")
        if not isinstance(edges_raw, list):
            return None

        edges = [e for e in edges_raw if isinstance(e, dict)]

        # Pre-compute structures used by all three checks.
        children_to_parents: dict[str, list[str]] = {}
        all_children: set[str] = set()
        all_parents: set[str] = set()
        for edge in edges:
            child = edge.get("child")
            parent = edge.get("parent")
            if not isinstance(child, str) or not isinstance(parent, str):
                continue
            children_to_parents.setdefault(child, []).append(parent)
            all_children.add(child)
            all_parents.add(parent)

        expected_frames_raw = event.data.get("expected_frames")
        expected_frames: set[str] = (
            {f for f in expected_frames_raw if isinstance(f, str)}
            if isinstance(expected_frames_raw, list)
            else set()
        )

        # 1. Orphan frame: expected but no parent edge connects it to the
        # graph (we treat "is a child somewhere" as "connected"). We do
        # not chase ancestry to a root because a true root has no parent
        # edge and we'd false-positive on it.
        for frame in sorted(expected_frames):
            if frame in all_children:
                continue
            if frame in all_parents:
                # Frame exists as a parent only -> it's a root. Roots
                # are not orphans by themselves; only missing children
                # of roots are.
                continue
            return self._emit(
                event,
                failure_kind="orphan_frame",
                frame=frame,
                parent="<none>",
                detail=(
                    f"Expected TF frame {frame!r} is not present in "
                    f"any edge; the transform tree is missing it."
                ),
            )

        # 2. Multi-parent: a child appears under more than one parent.
        for child, parents in sorted(children_to_parents.items()):
            unique_parents = sorted(set(parents))
            if len(unique_parents) <= 1:
                continue
            return self._emit(
                event,
                failure_kind="multi_parent",
                frame=child,
                parent=",".join(unique_parents),
                detail=(
                    f"TF frame {child!r} has multiple parents "
                    f"({', '.join(unique_parents)}); tf2 lookups will "
                    f"return inconsistent transforms."
                ),
            )

        # 3. Stale edge: a non-static edge has not been updated in too long.
        for edge in edges:
            child = edge.get("child")
            parent = edge.get("parent")
            if not isinstance(child, str) or not isinstance(parent, str):
                continue
            if edge.get("is_static") is True:
                continue
            last_age = edge.get("last_update_sec_ago")
            if not isinstance(last_age, (int, float)):
                continue
            if last_age <= self._stale_timeout_sec:
                continue
            return self._emit(
                event,
                failure_kind="stale_edge",
                frame=child,
                parent=parent,
                detail=(
                    f"TF edge {parent!r} -> {child!r} has not been "
                    f"updated for {float(last_age):.2f}s "
                    f"(timeout {self._stale_timeout_sec}s)."
                ),
                value=float(last_age),
                threshold=float(self._stale_timeout_sec),
            )

        return None

    # -- Helpers --------------------------------------------------------

    def _emit(
        self,
        event: BlackBoxEvent,
        *,
        failure_kind: str,
        frame: str,
        parent: str,
        detail: str,
        value: float = 0.0,
        threshold: float = 0.0,
    ) -> BlackBoxEvent:
        """Build the anomaly event with the standard envelope."""
        anomaly = AnomalyData(
            detector=self.name,
            metric=f"tf:{failure_kind}:{frame}",
            value=float(value),
            threshold=float(threshold),
            message=detail,
        )

        logger.warning("TF topology anomaly: %s", detail)

        data: dict[str, Any] = anomaly.model_dump()
        data["failure_kind"] = failure_kind
        data["frame"] = frame
        data["parent"] = parent

        metadata = dict(event.metadata)
        metadata.update(self.detector_metadata())

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.tf_topology",
            data=data,
            severity="warning",
            **metadata,
        )
