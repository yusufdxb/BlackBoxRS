"""Record model and ROS message extraction for the flight recorder.

A record is one JSON-serialisable dict. Every record carries the recorder's
own clocks, taken when the recorder saw it:

``t_mono_ns``
    recorder ``CLOCK_MONOTONIC``. Never jumps. The ring buffer, trigger
    windows and every receipt-domain interval use this clock.
``t_wall_ns``
    recorder wall clock (``CLOCK_REALTIME``) at the same instant. Used to
    detect wall-clock jumps and to line bundles up with other logs.
``t_ros_ns``
    the recorder node's ROS clock. Equal to wall time unless
    ``use_sim_time`` is set.

Message records can also carry up to three publisher-side times, each in a
declared clock domain:

``dds_src_ns``
    DDS source timestamp: when the publisher wrote the sample, on the
    publisher host's wall clock (``rmw_message_info_t.source_timestamp``).
``dds_rx_ns``
    DDS reception timestamp on the recorder host's wall clock, taken by the
    middleware before the executor queue (so it excludes callback batching).
``pub_stamp_s``
    a stamp the publisher wrote into the message itself (``header.stamp``,
    HELIX ``stamp``/``timestamp``, sink trace ``t_wall``). Its domain is
    ``pub_stamp_domain``.

Receipt times are never used as emission times. See docs/FLIGHT_RECORDER.md,
section "Timing methodology".
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

MAX_LIST = 256  # longer sequences keep their length and first MAX_LIST items

# Where each role's embedded publisher stamp lives and which clock wrote it.
# helix_payload_wall: time.time() / node clock on the HELIX host (payload Jetson).
# robot_clock: the GO2 main computer, known to be skewed against the payload.
STAMP_SOURCES: dict[str, tuple[str, str]] = {
    "helix_fault": ("timestamp", "helix_payload_wall"),
    "recovery_action": ("timestamp", "helix_payload_wall"),
    "helix_hold": ("stamp", "helix_payload_wall"),
    "arbiter_status": ("stamp", "helix_payload_wall"),
    "sink_trace": ("t_wall", "helix_payload_wall"),
    "odometry": ("header.stamp", "robot_clock"),
    "go2_state": ("stamp", "robot_clock"),
    "phoenix_observation": ("header.stamp", "publisher_header"),
}


def _is_ros_msg(obj: Any) -> bool:
    return hasattr(obj, "get_fields_and_field_types")


def _sanitize_float(v: float) -> Any:
    # JSON has no NaN/Inf. Keep them visible as strings rather than lying.
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    return v


def to_plain(obj: Any) -> Any:
    """Convert a ROS message (or nested value) into JSON-compatible data."""
    if _is_ros_msg(obj):
        return {name: to_plain(getattr(obj, name)) for name in obj.get_fields_and_field_types()}
    if isinstance(obj, float):
        return _sanitize_float(obj)
    if isinstance(obj, (bool, int, str)) or obj is None:
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return {"b64": base64.b64encode(bytes(obj[:MAX_LIST])).decode(), "len": len(obj)}
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    try:
        seq = list(obj)  # list, tuple, array.array, numpy array
    except TypeError:
        return repr(obj)
    items = [to_plain(x.item() if hasattr(x, "item") and not _is_ros_msg(x) else x)
             for x in seq[:MAX_LIST]]
    if len(seq) > MAX_LIST:
        return {"len": len(seq), "head": items, "truncated": True}
    return items


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(path)
            cur = cur[part]
        else:
            if not hasattr(cur, part):
                raise KeyError(path)
            cur = getattr(cur, part)
    return cur


def _set_path(out: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = out
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def extract_fields(msg: Any, fields: tuple[str, ...]) -> tuple[dict[str, Any], list[str]]:
    """Extract ``fields`` (dotted paths) from a message.

    Returns the nested dict and the list of paths that did not exist, so a
    profile typo is reported instead of silently producing empty records.
    An empty ``fields`` tuple keeps the whole message.
    """
    if not fields:
        plain = to_plain(msg)
        return (plain if isinstance(plain, dict) else {"value": plain}), []
    out: dict[str, Any] = {}
    missing: list[str] = []
    for f in fields:
        try:
            _set_path(out, f, to_plain(_get_path(msg, f)))
        except KeyError:
            missing.append(f)
    return out, missing


def stamp_to_sec(stamp: Any) -> float | None:
    """builtin_interfaces/Time or unitree TimeSpec (sec, nanosec) -> seconds."""
    if stamp is None:
        return None
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return float(stamp)
    get = stamp.get if isinstance(stamp, dict) else (lambda k, d=None: getattr(stamp, k, d))
    sec, nsec = get("sec"), get("nanosec")
    if sec is None or nsec is None:
        return None
    return float(sec) + float(nsec) * 1e-9


def publisher_stamp(role: str, data: dict[str, Any]) -> tuple[float | None, str | None]:
    """The embedded publisher stamp for ``role`` and its clock domain.

    A zero stamp is treated as absent: HELIX writes 0 for "not asserted" and
    an unset header is 0.0, neither of which is an emission time.
    """
    src = STAMP_SOURCES.get(role)
    if src is None:
        return None, None
    path, domain = src
    try:
        raw = _get_path(data, path)
    except KeyError:
        return None, None
    val = stamp_to_sec(raw)
    if val is None or val == 0.0 or (isinstance(val, float) and not math.isfinite(val)):
        return None, None
    return val, domain


def decode_sink_trace(data: dict[str, Any]) -> dict[str, Any]:
    """HELIX sink trace is JSON inside std_msgs/String; parse it in place."""
    raw = data.get("data")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"unparsed": raw}
        if isinstance(parsed, dict):
            return parsed
    return data


def make_msg_record(
    *,
    topic: str,
    role: str,
    msg_type: str,
    data: dict[str, Any],
    t_mono_ns: int,
    t_wall_ns: int,
    t_ros_ns: int | None = None,
    dds_src_ns: int | None = None,
    dds_rx_ns: int | None = None,
    stored: bool = True,
) -> dict[str, Any]:
    if role == "sink_trace":
        data = decode_sink_trace(data)
    stamp, domain = publisher_stamp(role, data)
    return {
        "kind": "msg",
        "topic": topic,
        "role": role,
        "type": msg_type,
        "t_mono_ns": int(t_mono_ns),
        "t_wall_ns": int(t_wall_ns),
        "t_ros_ns": None if t_ros_ns is None else int(t_ros_ns),
        "dds_src_ns": dds_src_ns or None,
        "dds_rx_ns": dds_rx_ns or None,
        "pub_stamp_s": stamp,
        "pub_stamp_domain": domain,
        "data": data if stored else None,
    }


def make_event_record(
    kind: str, *, t_mono_ns: int, t_wall_ns: int, **payload: Any
) -> dict[str, Any]:
    """Non-message records: graph, sys, trigger, marker, health, recorder."""
    return {"kind": kind, "t_mono_ns": int(t_mono_ns), "t_wall_ns": int(t_wall_ns), **payload}


def record_size(rec: dict[str, Any]) -> int:
    """Serialized size estimate used for the ring buffer byte cap."""
    return len(json.dumps(rec, separators=(",", ":"), default=str))


class StoreDecimator:
    """Decide which arrivals keep their payload on ``store_max_hz`` topics.

    Every arrival still becomes a record (so rates, gaps and losses are
    exact); only the payload of the extra ones is dropped. Shared by the live
    recorder and the synthetic generator so both decimate identically.
    """

    def __init__(self, limits: dict[str, float]) -> None:
        self._period = {t: int(1e9 / hz) for t, hz in limits.items() if hz}
        self._last: dict[str, int] = {}

    def keep(self, topic: str, t_mono_ns: int) -> bool:
        period = self._period.get(topic)
        if period is None:
            return True
        last = self._last.get(topic)
        if last is not None and t_mono_ns - last < period:
            return False
        self._last[topic] = t_mono_ns
        return True
