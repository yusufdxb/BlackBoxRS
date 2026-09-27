"""Deterministic fault injection on the replay event stream.

A fault is a pure function from an ordered event list to a new ordered
event list. There is no randomness: every injector is fully determined by
its parameters, so no seed is needed or accepted. Faults are applied in the
order given, each to the output of the previous one.

Every event a fault creates or changes carries the fault id in
``ReplayEvent.injected``; the replay result lists, per fault, how many
events it touched and the first of them. A fault that matches no event is
an error (almost always a wrong topic name or time window), never a silent
no-op.

Times (``from_s``, ``to_s``, ``at_s``) are seconds from the start of the
evidence, on the replay (receipt) clock. Windows are half-open
``[from_s, to_s)``; an omitted ``to_s`` means the end of the evidence.
"""

from __future__ import annotations

import copy
import json
import math
import statistics
from dataclasses import dataclass, replace
from typing import Any, Callable

from blackboxrs.lab.events import ReplayEvent, check_total_order
from blackboxrs.lab.evidence import TopicInfo
from blackboxrs.lab.values import NS, as_number, del_path, get_path, set_path


class FaultError(ValueError):
    """A fault definition is invalid or does not apply to the evidence."""


_REQUIRED = object()


@dataclass(frozen=True)
class Param:
    kind: str                     # float, int, bool, str, list, json
    default: Any = _REQUIRED
    help: str = ""


@dataclass(frozen=True)
class KindSpec:
    name: str
    category: str
    summary: str
    params: dict[str, Param]
    fn: Callable[..., list[ReplayEvent]]
    selects: bool = True          # takes topic / topics / host


@dataclass(frozen=True)
class Fault:
    id: str
    kind: str
    params: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "params": dict(sorted(self.params.items()))}


class _Ctx:
    def __init__(self, fault: Fault, index: int, topics: dict[str, TopicInfo],
                 events: list[ReplayEvent]) -> None:
        self.fault = fault
        self.index = index
        self.topics = topics
        self.touched: list[ReplayEvent] = []
        # sub-index allocator for copies of an evidence event (duplicates)
        self._subs: dict[int, int] = {}
        for e in events:
            if e.order[0] == 0:
                self._subs[e.order[1]] = max(self._subs.get(e.order[1], 0), e.order[2])

    def next_sub(self, seq: int) -> int:
        self._subs[seq] = self._subs.get(seq, 0) + 1
        return self._subs[seq]

    def mark(self, e: ReplayEvent) -> ReplayEvent:
        e2 = e.touched(self.fault.id)
        self.touched.append(e2)
        return e2


# ---------------------------------------------------------------------------
# selection helpers
# ---------------------------------------------------------------------------

_SELECT = {
    "topic": Param("str", None, "one topic"),
    "topics": Param("list", None, "several topics"),
    "host": Param("str", None, "every topic published from this host (payload, robot)"),
}
_WINDOW = {
    "from_s": Param("float", 0.0, "window start, seconds from evidence start"),
    "to_s": Param("float", None, "window end (exclusive); omitted = end of evidence"),
}


def _topics_of(p: dict[str, Any], topics: dict[str, TopicInfo]) -> set[str]:
    chosen = [k for k in ("topic", "topics", "host") if p.get(k) is not None]
    if len(chosen) != 1:
        raise FaultError("give exactly one of topic, topics, host")
    if p.get("topic") is not None:
        names = {p["topic"]}
    elif p.get("topics") is not None:
        names = set(p["topics"])
    else:
        names = {n for n, t in topics.items() if t.host == p["host"]}
        if not names:
            raise FaultError(f"no topic in the evidence is published from host {p['host']!r}")
    unknown = sorted(names - set(topics))
    if unknown:
        raise FaultError(f"topics not in the evidence: {unknown}")
    return names


def _in_window(e: ReplayEvent, p: dict[str, Any]) -> bool:
    lo = int(round(p.get("from_s", 0.0) * NS))
    hi = p.get("to_s")
    return e.t_ns >= lo and (hi is None or e.t_ns < int(round(hi * NS)))


def _selected(events: list[ReplayEvent], p: dict[str, Any], ctx: _Ctx) -> list[int]:
    names = _topics_of(p, ctx.topics)
    return [i for i, e in enumerate(events)
            if e.kind == "msg" and e.topic in names and _in_window(e, p)]


def _need_payload(e: ReplayEvent, ctx: _Ctx) -> dict[str, Any]:
    if e.data is None:
        raise FaultError(f"{ctx.fault.id}: {e.eid} on {e.topic} has no stored payload "
                         "(decimated by store_max_hz); cannot inject a data fault into it")
    return e.data


# ---------------------------------------------------------------------------
# timing / transport
# ---------------------------------------------------------------------------


def _drop(events, p, ctx):
    idx = _selected(events, p, ctx)
    n = p["every_n"]
    if n < 1:
        raise FaultError("every_n must be >= 1")
    gone = {i for k, i in enumerate(idx) if (k + 1) % n == 0}
    for i in sorted(gone):
        ctx.touched.append(events[i].touched(ctx.fault.id))
    return [e for i, e in enumerate(events) if i not in gone]


def _gap(events, p, ctx):
    if p.get("to_s") is None:
        raise FaultError("gap needs to_s (use drop for a loss that never recovers)")
    return _drop(events, {**p, "every_n": 1}, ctx)


def _delay(events, p, ctx):
    d = int(round(p["delay_s"] * NS))
    if d <= 0:
        raise FaultError("delay_s must be > 0")
    out = list(events)
    for i in _selected(events, p, ctx):
        e = events[i]
        out[i] = ctx.mark(replace(e, t_ns=e.t_ns + d,
                                  rx_wall_ns=None if e.rx_wall_ns is None else e.rx_wall_ns + d))
    return out


def _duplicate(events, p, ctx):
    lag = int(round(p["lag_s"] * NS))
    if lag <= 0:
        raise FaultError("lag_s must be > 0 (a copy at the same instant has no receipt order)")
    n = p["every_n"]
    out = list(events)
    for k, i in enumerate(_selected(events, p, ctx)):
        if (k + 1) % n:
            continue
        e = events[i]
        if e.order[0] != 0:
            raise FaultError("duplicate applies to evidence events only")
        sub = ctx.next_sub(e.order[1])
        dup = ctx.mark(replace(e, t_ns=e.t_ns + lag, order=(0, e.order[1], sub),
                               eid=f"{e.eid}.dup{sub}",
                               rx_wall_ns=None if e.rx_wall_ns is None else e.rx_wall_ns + lag))
        out.append(dup)
    return out


def _reorder(events, p, ctx):
    idx = _selected(events, p, ctx)
    out = list(events)
    for a, b in zip(idx[0::2], idx[1::2]):
        ea, eb = events[a], events[b]
        out[a] = ctx.mark(replace(ea, t_ns=eb.t_ns, rx_wall_ns=eb.rx_wall_ns))
        out[b] = ctx.mark(replace(eb, t_ns=ea.t_ns, rx_wall_ns=ea.rx_wall_ns))
    return out


def _stale_redelivery(events, p, ctx):
    at = int(round(p["at_s"] * NS))
    age = int(round(p["age_s"] * NS))
    if age <= 0:
        raise FaultError("age_s must be > 0")
    names = _topics_of(p, ctx.topics)
    cands = [e for e in events if e.kind == "msg" and e.topic in names and e.t_ns <= at - age
             and e.order[0] == 0]
    if not cands:
        raise FaultError(f"no message on {sorted(names)} at or before {(at - age) / NS:.3f} s")
    src = cands[-1]
    sub = ctx.next_sub(src.order[1])
    shift = at - src.t_ns
    copy_ev = ctx.mark(replace(src, t_ns=at, order=(0, src.order[1], sub),
                               eid=f"{src.eid}.stale{sub}",
                               rx_wall_ns=None if src.rx_wall_ns is None
                               else src.rx_wall_ns + shift))
    return list(events) + [copy_ev]


def _shift_stamps(e: ReplayEvent, off_s: float) -> dict[str, Any]:
    ch: dict[str, Any] = {}
    if e.src_ns is not None:
        ch["src_ns"] = e.src_ns + int(round(off_s * NS))
    if e.pub_stamp_s is not None:
        ch["pub_stamp_s"] = e.pub_stamp_s + off_s
    return ch


def _clock_skew(events, p, ctx):
    out = list(events)
    hit = 0
    for i in _selected(events, {**p}, ctx):
        e = events[i]
        ch = _shift_stamps(e, p["offset_s"])
        if ch:
            out[i] = ctx.mark(replace(e, **ch))
            hit += 1
    if not hit:
        raise FaultError("clock_skew: no selected message carries a publisher timestamp")
    return out


def _timestamp_jump(events, p, ctx):
    return _clock_skew(events, {**p, "offset_s": p["jump_s"], "from_s": p["at_s"]}, ctx)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def _field_list(p: dict[str, Any]) -> list[str]:
    fields = [p["field"]] if p.get("field") else list(p.get("fields") or [])
    if not fields:
        raise FaultError("give field or fields")
    return fields


def _set_fields(events, p, ctx, value_for: Callable[[ReplayEvent, str], Any],
                *, require_existing: bool = True):
    out = list(events)
    fields = _field_list(p)
    for i in _selected(events, p, ctx):
        e = events[i]
        data = _need_payload(e, ctx)
        for f in fields:
            found, _ = get_path(data, f)
            if require_existing and not found:
                raise FaultError(f"{ctx.fault.id}: field {f!r} not in {e.topic} payload")
            v = value_for(e, f)
            data = del_path(data, f) if v is _REMOVE else set_path(data, f, v)
        out[i] = ctx.mark(replace(e, data=data))
    return out


_REMOVE = object()


def _nan(events, p, ctx):
    return _set_fields(events, p, ctx, lambda e, f: math.nan)


def _inf(events, p, ctx):
    sign = p["sign"]
    if sign not in (1, -1):
        raise FaultError("sign must be 1 or -1")
    return _set_fields(events, p, ctx, lambda e, f: math.inf * sign)


def _malformed(events, p, ctx):
    if p["remove"]:
        return _set_fields(events, p, ctx, lambda e, f: _REMOVE)
    return _set_fields(events, p, ctx, lambda e, f: p["value"])


def _set_value(events, p, ctx):
    return _set_fields(events, p, ctx, lambda e, f: copy.deepcopy(p["value"]))


def _freeze(events, p, ctx):
    names = _topics_of(p, ctx.topics)
    lo = int(round(p.get("from_s", 0.0) * NS))
    fields = _field_list(p)
    held: dict[str, Any] = {}
    for e in events:
        if e.kind == "msg" and e.topic in names and e.t_ns < lo and e.data is not None:
            for f in fields:
                found, v = get_path(e.data, f)
                if found:
                    held[f] = v
    missing = [f for f in fields if f not in held]
    if missing:
        raise FaultError(f"freeze: no value of {missing} before {lo / NS:.3f} s to hold")
    return _set_fields(events, p, ctx, lambda e, f: copy.deepcopy(held[f]))


def _step(events, p, ctx):
    def bump(e: ReplayEvent, f: str) -> Any:
        v, problem = as_number(get_path(e.data, f)[1])
        if problem:
            raise FaultError(f"step: {f!r} on {e.topic} is not a finite number")
        return v + p["delta"]
    lo = p["at_s"]
    return _set_fields(events, {**p, "from_s": lo}, ctx, bump)


# ---------------------------------------------------------------------------
# safety / control
# ---------------------------------------------------------------------------


def _host_offset_ns(events: list[ReplayEvent], host: str, topics: dict[str, TopicInfo]) -> int:
    d = [e.src_ns - e.rx_wall_ns for e in events
         if e.kind == "msg" and e.src_ns and e.rx_wall_ns and e.topic in topics
         and topics[e.topic].host == host]
    return int(statistics.median(d)) if d else 0


def _inject_stream(events, p, ctx):
    if p["rate_hz"] <= 0:
        raise FaultError("rate_hz must be > 0")
    if p.get("to_s") is None:
        raise FaultError("inject_stream needs to_s")
    for key in ("data", "final_data"):
        if p.get(key) is not None and not isinstance(p[key], dict):
            raise FaultError(f"inject_stream: {key} must be a message object, "
                             f"got {p[key]!r}")
    topic = p["topic"]
    info = ctx.topics.get(topic)
    role = p.get("role") or (info.role if info else None)
    if role is None:
        raise FaultError(f"inject_stream: {topic} is not in the evidence; give role")
    host = p.get("host") or (info.host if info else "payload")
    anchor = next((e for e in events if e.kind == "msg" and e.rx_wall_ns), None)
    if anchor is None:
        raise FaultError("inject_stream: evidence has no receipt wall clock to anchor on")
    wall0 = anchor.rx_wall_ns - anchor.t_ns
    mono0 = anchor.record.get("t_mono_ns", anchor.t_ns) - anchor.t_ns
    off = _host_offset_ns(events, host, ctx.topics)
    lo = int(round(p["from_s"] * NS))
    hi = int(round(p["to_s"] * NS))
    period = NS / p["rate_hz"]
    times = []
    k = 0
    while lo + int(round(k * period)) < hi:
        times.append(lo + int(round(k * period)))
        k += 1
    payloads = [p["data"]] * len(times)
    if p.get("final_data") is not None:
        times.append(hi)
        payloads.append(p["final_data"])
    new = []
    for k, (t, data) in enumerate(zip(times, payloads)):
        rx = wall0 + t
        rec = {"kind": "msg", "topic": topic, "role": role, "type": p["type"],
               "t_mono_ns": mono0 + t, "t_wall_ns": rx, "t_ros_ns": rx,
               "dds_src_ns": rx + off, "dds_rx_ns": rx, "pub_stamp_s": None,
               "pub_stamp_domain": None, "data": copy.deepcopy(data)}
        new.append(ctx.mark(ReplayEvent(
            t_ns=t, order=(1, ctx.index, k), eid=f"{ctx.fault.id}.{k}", kind="msg",
            topic=topic, role=role, type=p["type"], data=copy.deepcopy(data), src_ns=rx + off,
            rx_wall_ns=rx, record=rec)))
    if not new:
        raise FaultError("inject_stream: window produced no messages")
    if topic not in ctx.topics:
        ctx.topics[topic] = TopicInfo(topic, role, host, "event", None)
    return list(events) + new


def _node_exit(events, p, ctx):
    """A node leaves the ROS graph at at_s (crash, kill, host down).

    Adds a graph change record at at_s and removes the node from every later
    recorded graph snapshot, so the graph never shows it again. Its topics
    keep arriving unless another fault removes them (compose with drop/gap).
    """
    at = int(round(p["at_s"] * NS))
    node = p["node"]
    before = [e for e in events if e.kind == "graph" and e.t_ns <= at]
    nodes: set[str] = set()
    pubs: dict[str, list[str]] = {}
    for g in before:
        r = g.record
        if r.get("full"):
            nodes = set(r.get("nodes") or [])
        else:
            nodes = (nodes - set(r.get("nodes_gone") or [])) | set(r.get("nodes_new") or [])
        pubs = {k: list(v) for k, v in (r.get("publishers") or pubs).items()}
    if node not in nodes:
        raise FaultError(f"node_exit: {node} is not on the recorded graph before "
                         f"{at / NS:.3f} s")

    def without(r: dict[str, Any]) -> dict[str, Any]:
        r = copy.deepcopy(r)
        for k in ("nodes", "nodes_new"):
            if k in r:
                r[k] = [n for n in r[k] if n != node]
        if "publishers" in r:
            r["publishers"] = {t: [n for n in v if n != node]
                               for t, v in r["publishers"].items()}
        return r

    out = []
    for e in events:
        if e.kind == "graph" and e.t_ns > at:
            e = ctx.mark(replace(e, record=without(e.record)))
        out.append(e)
    anchor = before[-1] if before else events[0]
    shift = at - anchor.t_ns
    rec = {"kind": "graph", "full": False, "nodes_gone": [node], "nodes_new": [],
           "publishers": {t: [n for n in v if n != node] for t, v in sorted(pubs.items())},
           "t_mono_ns": anchor.record.get("t_mono_ns", anchor.t_ns) + shift,
           "t_wall_ns": anchor.record.get("t_wall_ns", 0) + shift}
    out.append(ctx.mark(ReplayEvent(t_ns=at, order=(1, ctx.index, 0), eid=f"{ctx.fault.id}.0",
                                    kind="graph", record=rec)))
    return out


_FIELD = {"field": Param("str", None, "dotted payload field"),
          "fields": Param("list", None, "several dotted payload fields")}

KINDS: dict[str, KindSpec] = {}


def _kind(name: str, category: str, summary: str, fn, params: dict[str, Param],
          *, selects: bool = True, window: bool = True) -> None:
    ps = {**(_SELECT if selects else {}), **(_WINDOW if window else {}), **params}
    KINDS[name] = KindSpec(name, category, summary, ps, fn, selects)


_kind("drop", "transport", "drop messages (every_n=1: all) in the window", _drop,
      {"every_n": Param("int", 1, "drop every n-th selected message")})
_kind("gap", "transport", "temporary telemetry gap: nothing arrives in [from_s, to_s)", _gap, {})
_kind("delay", "transport", "deliver messages delay_s late (publisher stamps unchanged)", _delay,
      {"delay_s": Param("float", help="added receipt delay")})
_kind("duplicate", "transport", "deliver a second copy lag_s after the original", _duplicate,
      {"every_n": Param("int", 1, "duplicate every n-th message"),
       "lag_s": Param("float", 0.0002, "delay of the copy")})
_kind("reorder", "transport", "swap the arrival of consecutive message pairs", _reorder, {})
_kind("stale_redelivery", "transport",
      "re-deliver at at_s the last message that is age_s old (stale command)", _stale_redelivery,
      {"at_s": Param("float", help="delivery time"), "age_s": Param("float", help="its age")},
      window=False)
_kind("clock_skew", "transport",
      "offset publisher timestamps (DDS source + embedded) by offset_s from from_s on",
      _clock_skew, {"offset_s": Param("float", help="seconds added to publisher clocks")})
_kind("timestamp_jump", "transport", "publisher timestamps step by jump_s at at_s",
      _timestamp_jump, {"at_s": Param("float", help="jump time"),
                        "jump_s": Param("float", help="step size")}, window=False)
_kind("nan", "data", "set payload field(s) to NaN", _nan, dict(_FIELD))
_kind("inf", "data", "set payload field(s) to +Inf or -Inf", _inf,
      {**_FIELD, "sign": Param("int", 1, "1 or -1")})
_kind("malformed", "data", "replace field(s) with a non-numeric value, or remove them",
      _malformed, {**_FIELD, "value": Param("json", "not-a-number", "replacement value"),
                   "remove": Param("bool", False, "delete the field instead")})
_kind("set_value", "data", "overwrite field(s) with a given value", _set_value,
      {**_FIELD, "value": Param("json", help="value to write")})
_kind("freeze", "data", "hold field(s) at their last value before from_s (stuck sensor)",
      _freeze, dict(_FIELD))
_kind("step", "data", "add delta to a numeric field from at_s on (discontinuity)", _step,
      {"field": Param("str", help="dotted payload field"), "delta": Param("float", help="step"),
       "at_s": Param("float", help="step time"), "to_s": Param("float", None, "step end")},
      window=False)
_kind("node_exit", "transport", "a node leaves the ROS graph at at_s (crash or host down)",
      _node_exit, {"node": Param("str", help="fully qualified node name"),
                   "at_s": Param("float", help="exit time")}, selects=False, window=False)
_kind("inject_stream", "control",
      "publish a message stream that is not in the evidence (e.g. a teleop stream)",
      _inject_stream,
      {"topic": Param("str", help="topic to publish on"),
       "data": Param("json", help="payload of every message"),
       "rate_hz": Param("float", 20.0, "publish rate"),
       "from_s": Param("float", help="first message"), "to_s": Param("float", help="stream end"),
       "final_data": Param("json", None, "one last payload published at to_s"),
       "role": Param("str", None, "role (default: from the evidence)"),
       "type": Param("str", "geometry_msgs/msg/Twist", "message type"),
       "host": Param("str", None, "publishing host (default: from the evidence, else payload)")},
      selects=False, window=False)


# ---------------------------------------------------------------------------
# parsing and application
# ---------------------------------------------------------------------------


def _coerce(name: str, spec: Param, v: Any) -> Any:
    if v is None:
        return None
    try:
        if spec.kind == "float":
            if isinstance(v, bool):
                raise TypeError
            out = float(v)
            if not math.isfinite(out):
                raise TypeError
            return out
        if spec.kind == "int":
            if isinstance(v, bool) or float(v) != int(float(v)):
                raise TypeError
            return int(float(v))
        if spec.kind == "bool":
            if isinstance(v, bool):
                return v
            if str(v).lower() in ("true", "1", "yes"):
                return True
            if str(v).lower() in ("false", "0", "no"):
                return False
            raise TypeError
        if spec.kind == "list":
            if isinstance(v, str):
                return [x for x in v.split("|") if x]
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return list(v)
            raise TypeError
        if spec.kind == "str":
            if not isinstance(v, str):
                raise TypeError
            return v
        return v  # json
    except (TypeError, ValueError):
        raise FaultError(f"parameter {name}: expected {spec.kind}, got {v!r}") from None


def parse_fault(raw: dict[str, Any], index: int) -> Fault:
    if not isinstance(raw, dict):
        raise FaultError(f"fault #{index + 1}: must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise FaultError(f"fault #{index + 1}: unknown kind {kind!r}; "
                         f"known: {', '.join(sorted(KINDS))}")
    spec = KINDS[kind]
    fid = raw.get("id") or f"F{index + 1}"
    if not isinstance(fid, str):
        raise FaultError(f"fault #{index + 1}: id must be a string")
    extra = sorted(set(raw) - set(spec.params) - {"kind", "id"})
    if extra:
        raise FaultError(f"fault {fid} ({kind}): unknown parameter(s) {extra}")
    params: dict[str, Any] = {}
    for name, ps in spec.params.items():
        if name in raw:
            params[name] = _coerce(name, ps, raw[name])
        elif ps.default is _REQUIRED:
            raise FaultError(f"fault {fid} ({kind}): missing parameter {name!r}")
        else:
            params[name] = ps.default
    return Fault(fid, kind, params)


def parse_cli_fault(text: str, index: int) -> Fault:
    """``kind:key=value,key=value`` or a JSON object.

    Values are read as JSON when they parse (numbers, true, null,
    "NaN" strings), otherwise as plain strings. Lists use ``|``.
    """
    text = text.strip()
    if text.startswith("{"):
        try:
            return parse_fault(json.loads(text), index)
        except ValueError as exc:
            raise FaultError(f"--inject {text!r}: invalid JSON: {exc}") from exc
    kind, _, rest = text.partition(":")
    raw: dict[str, Any] = {"kind": kind.strip()}
    for part in [x for x in rest.split(",") if x.strip()]:
        k, sep, v = part.partition("=")
        if not sep:
            raise FaultError(f"--inject {text!r}: expected key=value, got {part!r}")
        try:
            raw[k.strip()] = json.loads(v)
        except ValueError:
            raw[k.strip()] = v.strip()
    return parse_fault(raw, index)


def apply_faults(events: list[ReplayEvent], faults: list[Fault],
                 topics: dict[str, TopicInfo]) -> tuple[list[ReplayEvent], list[dict[str, Any]]]:
    """Apply ``faults`` in order. Returns the new ordered stream and the injection log."""
    ids = [f.id for f in faults]
    if len(set(ids)) != len(ids):
        raise FaultError(f"duplicate fault ids: {ids}")
    log = []
    cur = list(events)
    for i, f in enumerate(faults):
        ctx = _Ctx(f, i, topics, cur)
        try:
            cur = KINDS[f.kind].fn(cur, f.params, ctx)
        except FaultError as exc:
            raise FaultError(f"fault {f.id} ({f.kind}): {exc}") from None
        if not ctx.touched:
            raise FaultError(f"fault {f.id} ({f.kind}) matched no event; check the topic "
                             "and the time window")
        cur.sort(key=lambda e: e.key)
        touched = sorted(ctx.touched, key=lambda e: e.key)
        log.append({**f.describe(), "category": KINDS[f.kind].category,
                    "events_affected": len(touched),
                    "topics_affected": sorted({e.topic for e in touched if e.topic}),
                    "first_t_ns": touched[0].t_ns, "last_t_ns": touched[-1].t_ns,
                    "first_events": [e.eid for e in touched[:10]]})
    check_total_order(cur)
    return cur, log
