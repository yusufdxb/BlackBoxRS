"""Numeric payload handling and canonical JSON for replay results.

Flight records store non-finite floats as the strings ``"NaN"``,
``"Infinity"`` and ``"-Infinity"`` (JSON has no such numbers). Replay turns
them back into floats where a number is expected, and treats anything else
that is not a number as malformed rather than guessing a value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

_NONFINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
NS = 1_000_000_000


def as_number(v: Any) -> tuple[float | None, str]:
    """Return ``(value, problem)``.

    ``problem`` is ``""`` for a finite number, ``"nonfinite"`` for NaN or
    Inf (value returned), and ``"malformed"`` for a missing or non-numeric
    value (value ``None``). Booleans are malformed: a bool in a velocity
    field is a type error, not 0 or 1.
    """
    if isinstance(v, bool) or v is None:
        return None, "malformed"
    if isinstance(v, (int, float)):
        f = float(v)
        return f, ("" if math.isfinite(f) else "nonfinite")
    if isinstance(v, str) and v in _NONFINITE:
        return _NONFINITE[v], "nonfinite"
    return None, "malformed"


def get_path(data: Any, path: str) -> tuple[bool, Any]:
    """``(found, value)`` for a dotted path into nested dicts."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def set_path(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Copy of ``data`` with ``path`` set to ``value`` (intermediate dicts created)."""
    out = copy.deepcopy(data)
    parts = path.split(".")
    cur = out
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return out


def del_path(data: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(data)
    parts = path.split(".")
    cur = out
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return out
    cur.pop(parts[-1], None)
    return out


def jsonable(obj: Any) -> Any:
    """Recursively make ``obj`` strict-JSON safe (non-finite floats as strings)."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace variance, strict floats."""
    return json.dumps(jsonable(obj), sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=True)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("ascii")).hexdigest()


def fmt_s(t_ns: int | None) -> str:
    """Replay time as signed seconds with millisecond resolution."""
    if t_ns is None:
        return "-"
    return f"{t_ns / NS:+.3f}s"
