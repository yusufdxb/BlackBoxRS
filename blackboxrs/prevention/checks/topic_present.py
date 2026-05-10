"""Preflight check: topic is present on the ROS 2 graph.

Pass criterion: the configured topic has at least ``min_publishers``
publishers visible to a transient rclpy node after a short discovery
spin.

Failure mode mapped to the rule's ``severity_on_fail`` ("warn" or
"block"); "fail" comes back from this module and is upgraded by the
runner.

Limitations:

* Discovery latency: a topic that comes up *after* our spin window
  will be reported as missing. Increase ``timeout_sec`` if your stack
  takes longer to come up.
* Graph visibility depends on RMW + DDS settings: if the launch uses
  a different ``ROS_DOMAIN_ID`` than the shell running preflight, the
  topic will not be visible. We surface that as a "fail" because that
  is exactly what the operator needs to know.
* This check does not subscribe to the topic; messages may still be
  silently dropped due to a QoS mismatch even when this check passes.
  Pair with ``qos_match`` for the same topic if that matters.
"""

from __future__ import annotations

from typing import Any

from ._rclpy_runtime import RCLPY_AVAILABLE, with_graph_node


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a topic-present check.

    Expected ``params``:

    - ``topic`` (str): fully-qualified topic name to check.
    - ``min_publishers`` (int, default 1): minimum publisher count to
      consider the check passed.
    - ``timeout_sec`` (float, default 3.0): graph discovery settle
      time.
    """
    topic = params.get("topic")
    min_pubs = int(params.get("min_publishers", 1))
    timeout_sec = float(params.get("timeout_sec", 3.0))

    if not isinstance(topic, str) or not topic:
        return ("error", "topic_present requires a non-empty 'topic' parameter.")

    if not RCLPY_AVAILABLE:
        return ("skipped",
                f"rclpy not available; cannot verify {topic!r} is on the graph.")

    def _query(node: Any) -> tuple[str, str]:
        try:
            infos = node.get_publishers_info_by_topic(topic)
        except Exception as exc:
            return ("error", f"graph query failed for {topic!r}: {exc!r}")
        count = len(infos)
        if count >= min_pubs:
            return ("pass",
                    f"{topic} has {count} publisher(s) (>= {min_pubs}).")
        return ("fail",
                f"{topic} has {count} publisher(s); expected >= {min_pubs}. "
                f"Confirm the publisher node is running and that ROS_DOMAIN_ID "
                f"matches the launch.")

    return with_graph_node(_query, settle_sec=timeout_sec,
                           name="blackbox_preflight_topic_present")
