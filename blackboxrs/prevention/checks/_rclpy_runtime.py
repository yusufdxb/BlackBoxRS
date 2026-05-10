"""rclpy runtime helpers shared by preflight checks.

Encapsulates the awkward parts of using rclpy from a short-lived helper
process: lazy import, init/shutdown safety, transient node creation,
and a small spin loop for graph discovery (DDS discovery is not
instant; a tight one-shot query usually misses things on a cold cache).

The module is import-safe in environments without rclpy: callers should
inspect :data:`RCLPY_AVAILABLE` before running anything that touches
the graph.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

try:  # pragma: no cover - imported lazily on hosts with rclpy
    import rclpy
    from rclpy.node import Node

    RCLPY_AVAILABLE = True
except ImportError:  # pragma: no cover - rclpy missing
    rclpy = None  # type: ignore[assignment]
    Node = None  # type: ignore[assignment,misc]
    RCLPY_AVAILABLE = False


_DEFAULT_NODE_NAME = "blackbox_preflight"
_DEFAULT_NAMESPACE = "/blackbox"


@contextmanager
def transient_node(
    name: str = _DEFAULT_NODE_NAME,
    namespace: str = _DEFAULT_NAMESPACE,
) -> Iterator[Any]:
    """Yield an rclpy node and tear it down on exit.

    Initialises rclpy if it has not been already; only shuts down rclpy
    if this context did the init (so callers running inside an existing
    rclpy process do not have their context yanked out from under them).
    """
    if not RCLPY_AVAILABLE:
        raise RuntimeError("rclpy is not available; install ros-<distro>-rclpy")

    init_done_here = False
    if not rclpy.ok():
        rclpy.init()
        init_done_here = True

    node = rclpy.create_node(name, namespace=namespace)
    try:
        yield node
    finally:
        try:
            node.destroy_node()
        except Exception:
            logger.debug("destroy_node failed", exc_info=True)
        if init_done_here:
            try:
                rclpy.shutdown()
            except Exception:
                logger.debug("rclpy.shutdown failed", exc_info=True)


def spin_for(node: Any, deadline: float, *, slice_sec: float = 0.1) -> None:
    """Spin *node* until wall-clock ``time.monotonic()`` exceeds *deadline*.

    Used to allow DDS discovery to populate before we ask the graph for
    publishers/subscribers/nodes. Does not raise on spin errors; a flaky
    spin is logged and treated as "no further events this slice."
    """
    if not RCLPY_AVAILABLE:
        return
    while time.monotonic() < deadline:
        try:
            rclpy.spin_once(node, timeout_sec=slice_sec)
        except Exception:
            logger.debug("spin_once raised; continuing", exc_info=True)
            time.sleep(slice_sec)


def with_graph_node(
    fn: Callable[[Any], tuple[str, str]],
    *,
    settle_sec: float = 1.0,
    name: str = _DEFAULT_NODE_NAME,
) -> tuple[str, str]:
    """Run *fn* with a transient node after a short discovery spin.

    Args:
        fn: Callable that takes the rclpy node and returns
            ``(status, message)``.
        settle_sec: Seconds to spin the node before invoking *fn* so
            the graph cache is populated.
        name: Optional override for the transient node name.

    Returns:
        ``(status, message)`` from *fn*, or
        ``("skipped", "rclpy not available...")`` when rclpy is missing.
    """
    if not RCLPY_AVAILABLE:
        return ("skipped", "rclpy not available; preflight cannot inspect "
                           "the ROS 2 graph in this environment.")

    deadline = time.monotonic() + max(0.0, settle_sec)
    with transient_node(name=name) as node:
        spin_for(node, deadline)
        return fn(node)
