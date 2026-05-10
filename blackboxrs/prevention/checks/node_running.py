"""Preflight check: a named ROS 2 node is present on the graph.

Pass criterion: ``get_node_names_and_namespaces()`` returns at least
one entry whose ``namespace + name`` matches the configured target.

The check accepts either a fully-qualified node spec
(``/namespace/name``) or a bare ``name``. When the namespace is
omitted we accept any namespace.
"""

from __future__ import annotations

from typing import Any

from ._rclpy_runtime import RCLPY_AVAILABLE, with_graph_node


def _split_target(target: str) -> tuple[str | None, str]:
    """Return ``(namespace_or_None, name)`` for a node spec."""
    target = target.strip()
    if "/" not in target:
        return (None, target)
    head, _, tail = target.rpartition("/")
    namespace = head if head else "/"
    return (namespace, tail)


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a node_running check.

    Expected ``params``:

    - ``name`` (str): node name; optionally fully-qualified
      (``/ns/name``).
    - ``timeout_sec`` (float, default 3.0): graph discovery settle.
    """
    name = params.get("name")
    timeout_sec = float(params.get("timeout_sec", 3.0))

    if not isinstance(name, str) or not name.strip():
        return ("error", "node_running requires a non-empty 'name' parameter.")

    if not RCLPY_AVAILABLE:
        return ("skipped",
                f"rclpy not available; cannot verify node {name!r} is running.")

    expected_ns, expected_name = _split_target(name)

    def _query(node: Any) -> tuple[str, str]:
        try:
            entries = node.get_node_names_and_namespaces()
        except Exception as exc:
            return ("error", f"graph query failed: {exc!r}")

        # Each entry is (name, namespace). Prefer namespace match when
        # the rule provided one; otherwise accept any namespace.
        for entry_name, entry_ns in entries:
            if entry_name != expected_name:
                continue
            if expected_ns is None or entry_ns == expected_ns:
                fq = f"{entry_ns.rstrip('/')}/{entry_name}"
                return ("pass", f"node {fq!r} is on the graph.")

        listed = ", ".join(sorted(f"{ns.rstrip('/')}/{n}"
                                  for n, ns in entries)) or "(none)"
        return ("fail",
                f"node {name!r} not found on graph. "
                f"Visible nodes: {listed}")

    return with_graph_node(_query, settle_sec=timeout_sec,
                           name="blackbox_preflight_node_running")
