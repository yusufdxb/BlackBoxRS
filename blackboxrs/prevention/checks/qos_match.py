"""Preflight check: publisher and subscriber QoS profiles are compatible.

Inspects ``get_publishers_info_by_topic`` and
``get_subscriptions_info_by_topic`` for the configured topic and
applies the same compatibility rules used by the
:class:`QoSMismatchDetector` at runtime.

A topic with zero publishers OR zero subscribers is reported as
``skipped`` with an explicit message: there is no compatibility
relation to verify, and we refuse to silently pretend a missing
endpoint is "fine."
"""

from __future__ import annotations

import logging
from typing import Any

from ._rclpy_runtime import RCLPY_AVAILABLE, with_graph_node

logger = logging.getLogger(__name__)


_RELIABILITY_RANK = {"RELIABLE": 2, "BEST_EFFORT": 1}
_DURABILITY_RANK = {"TRANSIENT_LOCAL": 2, "VOLATILE": 1}


def _enum_short(value: Any) -> str | None:
    """Return the trailing identifier of an rclpy QoS enum."""
    if value is None:
        return None
    raw = getattr(value, "name", None) or str(value)
    return raw.rsplit(".", maxsplit=1)[-1].upper()


def _check_dim(pub: Any, sub: Any, attr: str, ranks: dict[str, int]) -> str | None:
    """Mirror of QoSMismatchDetector dimension check, against rclpy info."""
    pub_short = _enum_short(getattr(pub.qos_profile, attr, None))
    sub_short = _enum_short(getattr(sub.qos_profile, attr, None))
    if pub_short is None or sub_short is None:
        return None
    pub_rank = ranks.get(pub_short)
    sub_rank = ranks.get(sub_short)
    if pub_rank is None or sub_rank is None:
        return None
    if pub_rank < sub_rank:
        return f"{attr}: pub={pub_short}, sub={sub_short}"
    return None


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a qos_match check.

    Expected ``params``:

    - ``topic`` (str): fully-qualified topic name.
    - ``timeout_sec`` (float, default 3.0): graph discovery settle.
    """
    topic = params.get("topic")
    timeout_sec = float(params.get("timeout_sec", 3.0))

    if not isinstance(topic, str) or not topic:
        return ("error", "qos_match requires a non-empty 'topic' parameter.")

    if not RCLPY_AVAILABLE:
        return ("skipped",
                f"rclpy not available; cannot inspect QoS on {topic!r}.")

    def _query(node: Any) -> tuple[str, str]:
        try:
            pubs = node.get_publishers_info_by_topic(topic)
            subs = node.get_subscriptions_info_by_topic(topic)
        except Exception as exc:
            return ("error", f"graph query failed for {topic!r}: {exc!r}")

        if not pubs:
            return ("skipped",
                    f"{topic} has no publishers visible; nothing to compare.")
        if not subs:
            return ("skipped",
                    f"{topic} has no subscribers visible; nothing to compare.")

        mismatches: list[str] = []
        for p in pubs:
            for s in subs:
                rel = _check_dim(p, s, "reliability", _RELIABILITY_RANK)
                if rel:
                    mismatches.append(rel)
                dur = _check_dim(p, s, "durability", _DURABILITY_RANK)
                if dur:
                    mismatches.append(dur)

        if not mismatches:
            return ("pass",
                    f"{topic}: {len(pubs)} pub(s) and {len(subs)} sub(s) "
                    f"have compatible QoS.")
        joined = "; ".join(sorted(set(mismatches)))
        return ("fail", f"{topic} QoS incompatibility(s): {joined}")

    return with_graph_node(_query, settle_sec=timeout_sec,
                           name="blackbox_preflight_qos_match")
