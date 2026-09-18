"""ROS-free recorder core: rolling buffer, triggers and incident lifecycle.

The live recorder (``recorder.py``), the offline rehearsal and the tests all
drive this same class. It sees only records and explicit times, so a replay
of a bundle through it runs exactly the logic that ran live.

Window model
------------
Every record enters a ring buffer keyed on recorder monotonic time. The ring
keeps at least ``pre_trigger_sec`` of history, bounded by ``max_records`` and
``max_bytes``. When a trigger fires, the ring's contents inside the
pre-trigger window are written to a new bundle at once, and every later
record streams straight to that bundle until ``post_trigger_sec`` after the
last trigger attached to it (capped at 3x post_trigger_sec after the first).
A trigger that fires while a bundle is open is attached to it as a secondary
trigger rather than opening another bundle, so one fault chain gives one
bundle.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from blackboxrs.flight.profile import FlightProfile
from blackboxrs.flight.records import make_event_record, record_size

logger = logging.getLogger(__name__)

_NS = 1_000_000_000
_FULL_GRAPH_EVERY_NS = 5 * _NS
# Nodes that come and go as part of normal tooling, never a trigger.
_IGNORED_NODE_PREFIXES = ("/_ros2cli", "/blackbox", "/_", "/launch_ros")


class IncidentSink(Protocol):
    """What the core needs from a bundle writer."""

    def open(self, trigger: dict[str, Any], pre_records: list[dict[str, Any]],
             pre_window: dict[str, Any]) -> str: ...
    def append(self, record: dict[str, Any]) -> None: ...
    def add_trigger(self, trigger: dict[str, Any]) -> None: ...
    def close(self, status: str, stats: dict[str, Any]) -> str | None: ...
    def wait(self, timeout: float | None = None) -> None: ...


@dataclass
class _Open:
    bundle_id: str
    sink: IncidentSink
    first_trigger_mono: int
    close_at_mono: int
    hard_close_mono: int
    triggers: int = 1


@dataclass
class CoreStats:
    records_in: int = 0
    ring_evicted_by_cap: int = 0
    ring_evicted_by_cap_in_window: int = 0
    incidents_opened: int = 0
    incidents_closed: int = 0
    incidents_skipped: int = 0
    triggers_fired: int = 0
    triggers_attached: int = 0
    triggers_suppressed_limit: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)


class FlightCore:
    """Ingest records, keep the rolling window, fire triggers, run bundles."""

    def __init__(
        self,
        profile: FlightProfile,
        sink_factory: Callable[[], IncidentSink],
        *,
        can_open: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        self.profile = profile
        self._sink_factory = sink_factory
        self._can_open = can_open or (lambda: (True, ""))
        self._ring: deque[tuple[int, int, dict[str, Any]]] = deque()
        self._ring_bytes = 0
        self._seq = 0
        self._open: _Open | None = None
        self.stats = CoreStats()
        self.closed_bundles: list[str] = []
        self._sinks: list[IncidentSink] = []
        self._last_wall_mono: tuple[int, int] | None = None
        # trigger state
        self._hold: bool | None = None
        self._arbiter_forced = False
        self._last_rx: dict[str, int] = {}
        self._stale: set[str] = set()
        self._stale_after = {
            t.name: int(t.stale_after_sec * _NS) for t in profile.topics if t.stale_after_sec
        }
        self._nodes: set[str] | None = None
        self._last_full_graph = 0
        self._watched_nodes: set[str] = set(profile.expected_nodes)
        self._pre_ns = int(profile.buffer.pre_trigger_sec * _NS)
        self._post_ns = int(profile.buffer.post_trigger_sec * _NS)

    # -- ingestion -------------------------------------------------------

    def ingest(self, rec: dict[str, Any]) -> None:
        """Take one record (message or event). Assigns ``seq``."""
        self._seq += 1
        rec["seq"] = self._seq
        self.stats.records_in += 1
        jump = self._clock_jump(rec)
        self._push(rec)
        if self._open is not None:
            self._open.sink.append(rec)
        if rec.get("kind") == "msg":
            self._last_rx[rec["topic"]] = rec["t_mono_ns"]
            if rec["topic"] in self._stale:
                self._stale.discard(rec["topic"])
            for trig in self._message_triggers(rec):
                self._fire(trig)
        if jump is not None:
            self.ingest(jump)
        self._maybe_close(rec["t_mono_ns"])

    def _clock_jump(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """A clock_jump record if the wall clock stepped > 50 ms against monotonic."""
        mono, wall = rec.get("t_mono_ns"), rec.get("t_wall_ns")
        if mono is None or wall is None or rec.get("kind") == "clock_jump":
            return None
        last, self._last_wall_mono = self._last_wall_mono, (mono, wall)
        if last is None:
            return None
        step = (wall - last[1]) - (mono - last[0])
        if abs(step) <= 50_000_000:
            return None
        return make_event_record(
            "clock_jump", t_mono_ns=mono, t_wall_ns=wall, wall_step_ns=int(step),
            note="recorder wall clock stepped against its monotonic clock")

    def _push(self, rec: dict[str, Any]) -> None:
        size = record_size(rec)
        self._ring.append((rec["t_mono_ns"], size, rec))
        self._ring_bytes += size
        newest = rec["t_mono_ns"]
        buf = self.profile.buffer
        # Keep one extra second so a trigger at the edge still sees a full window.
        horizon = newest - self._pre_ns - _NS
        while self._ring and self._ring[0][0] < horizon:
            self._evict()
        while self._ring and (len(self._ring) > buf.max_records
                              or self._ring_bytes > buf.max_bytes):
            t, _, _ = self._ring[0]
            self.stats.ring_evicted_by_cap += 1
            if t >= newest - self._pre_ns:
                self.stats.ring_evicted_by_cap_in_window += 1
            self._evict()

    def _evict(self) -> None:
        _, size, _ = self._ring.popleft()
        self._ring_bytes -= size

    # -- triggers --------------------------------------------------------

    def _message_triggers(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        tr = self.profile.triggers
        role, data = rec.get("role"), rec.get("data")
        out: list[dict[str, Any]] = []
        if data is None:
            return out
        base = {"t_mono_ns": rec["t_mono_ns"], "t_wall_ns": rec["t_wall_ns"],
                "topic": rec["topic"], "record_seq": rec["seq"]}
        if role == "helix_hold":
            hold = bool(data.get("hold"))
            if hold and self._hold is not True and tr.helix_hold_asserted:
                out.append({**base, "type": "helix_hold_asserted",
                            "observed_edge": self._hold is False,
                            "fault_id": data.get("fault_id", ""),
                            "reason": data.get("reason", "")})
            self._hold = hold
        elif role == "recovery_action" and tr.recovery_action_stop:
            if (data.get("action") in tr.recovery_actions
                    and data.get("status") in tr.recovery_statuses):
                out.append({**base, "type": "recovery_action_stop",
                            "action": data.get("action"), "status": data.get("status"),
                            "fault_id": data.get("fault_id", "")})
        elif role == "arbiter_status":
            forced = data.get("reason") in tr.arbiter_reasons
            if forced and not self._arbiter_forced and tr.arbiter_forced_zero:
                out.append({**base, "type": "arbiter_forced_zero",
                            "reason": data.get("reason"),
                            "hold_fault_id": data.get("hold_fault_id", "")})
            self._arbiter_forced = forced
        return out

    def mark(self, t_mono_ns: int, t_wall_ns: int, note: str = "", source: str = "manual") -> None:
        """Manual marker. Always recorded; triggers a bundle if armed."""
        rec = make_event_record("marker", t_mono_ns=t_mono_ns, t_wall_ns=t_wall_ns,
                                note=note, source=source)
        self.ingest(rec)
        if self.profile.triggers.manual_marker:
            self._fire({"type": "manual_marker", "t_mono_ns": t_mono_ns,
                        "t_wall_ns": t_wall_ns, "note": note, "source": source,
                        "record_seq": rec["seq"]})

    def graph(self, t_mono_ns: int, t_wall_ns: int, nodes: list[str],
              topics: dict[str, list[str]], publishers: dict[str, list[str]]) -> None:
        """Graph snapshot. Records only the diff (plus the first full snapshot)."""
        now = set(nodes)
        for plist in publishers.values():
            self._watched_nodes.update(plist)
        if self._nodes is None or t_mono_ns - self._last_full_graph >= _FULL_GRAPH_EVERY_NS:
            # A full snapshot at least every few seconds guarantees that any
            # pre-trigger window starts from a known node set.
            gone = (self._nodes - now) if self._nodes is not None else set()
            new = (now - self._nodes) if self._nodes is not None else set()
            self.ingest(make_event_record("graph", t_mono_ns=t_mono_ns, t_wall_ns=t_wall_ns,
                                          full=True, nodes=sorted(now), topics=topics,
                                          publishers=publishers, nodes_gone=sorted(gone),
                                          nodes_new=sorted(new)))
            self._last_full_graph = t_mono_ns
            first = self._nodes is None
            self._nodes = now
            if first or not gone:
                return
        else:
            gone, new = self._nodes - now, now - self._nodes
            self._nodes = now
            if not gone and not new:
                return
            self.ingest(make_event_record("graph", t_mono_ns=t_mono_ns, t_wall_ns=t_wall_ns,
                                          full=False, nodes_gone=sorted(gone),
                                          nodes_new=sorted(new), publishers=publishers))
        if not self.profile.triggers.node_disappeared:
            return
        for node in sorted(gone):
            if node.startswith(_IGNORED_NODE_PREFIXES) or node not in self._watched_nodes:
                continue
            self._fire({"type": "node_disappeared", "node": node,
                        "t_mono_ns": t_mono_ns, "t_wall_ns": t_wall_ns})

    def tick(self, t_mono_ns: int, t_wall_ns: int) -> None:
        """Periodic: freshness checks and bundle closing."""
        if self.profile.triggers.topic_stale:
            for topic, limit in self._stale_after.items():
                last = self._last_rx.get(topic)
                if last is None or topic in self._stale:
                    continue
                age = t_mono_ns - last
                if age > limit:
                    self._stale.add(topic)
                    self.ingest(make_event_record(
                        "health", t_mono_ns=t_mono_ns, t_wall_ns=t_wall_ns,
                        event="topic_stale", topic=topic, age_s=age / _NS,
                        limit_s=limit / _NS))
                    self._fire({"type": "topic_stale", "topic": topic,
                                "age_s": age / _NS, "limit_s": limit / _NS,
                                "t_mono_ns": t_mono_ns, "t_wall_ns": t_wall_ns})
        self._maybe_close(t_mono_ns)

    def _fire(self, trig: dict[str, Any]) -> None:
        self.stats.triggers_fired += 1
        self._seq += 1
        trig["seq"] = self._seq
        t = trig["t_mono_ns"]
        if self._open is not None:
            o = self._open
            o.triggers += 1
            o.close_at_mono = min(max(o.close_at_mono, t + self._post_ns), o.hard_close_mono)
            o.sink.add_trigger({**trig, "role": "secondary"})
            self.stats.triggers_attached += 1
            return
        if self.stats.incidents_opened >= self.profile.triggers.max_incidents_per_run:
            self.stats.triggers_suppressed_limit += 1
            return
        ok, why = self._can_open()
        if not ok:
            self.stats.incidents_skipped += 1
            self.stats.skip_reasons[why] = self.stats.skip_reasons.get(why, 0) + 1
            logger.error("incident not opened for %s: %s", trig["type"], why)
            return
        start = t - self._pre_ns
        pre = [r for (tm, _, r) in self._ring if tm >= start]
        ring_start = self._ring[0][0] if self._ring else t
        pre_window = {
            "requested_s": self.profile.buffer.pre_trigger_sec,
            "available_s": max(0.0, (t - max(start, ring_start)) / _NS),
            "evicted_by_cap_in_window": self.stats.ring_evicted_by_cap_in_window,
        }
        sink = self._sink_factory()
        self._sinks.append(sink)
        bundle_id = sink.open({**trig, "role": "primary"}, pre, pre_window)
        self.stats.incidents_opened += 1
        self._open = _Open(bundle_id=bundle_id, sink=sink, first_trigger_mono=t,
                           close_at_mono=t + self._post_ns,
                           hard_close_mono=t + 3 * self._post_ns)

    def _maybe_close(self, now_mono: int) -> None:
        if self._open is not None and now_mono >= self._open.close_at_mono:
            self._close("complete")

    def _close(self, status: str) -> None:
        o = self._open
        if o is None:
            return
        self._open = None
        path = o.sink.close(status, self.stats_dict())
        self.stats.incidents_closed += 1
        if path:
            self.closed_bundles.append(path)

    def shutdown(self, reason: str = "recorder_stopped") -> None:
        """Close any open bundle as interrupted (post window not complete)."""
        if self._open is not None:
            self._seq += 1
            last = self._ring[-1][2] if self._ring else {}
            self._open.sink.add_trigger({"type": "recorder_shutdown", "reason": reason,
                                         "role": "note", "seq": self._seq,
                                         "t_mono_ns": last.get("t_mono_ns", 0),
                                         "t_wall_ns": last.get("t_wall_ns", 0)})
            self._close("interrupted")

    def wait_writers(self, timeout: float | None = 60.0) -> None:
        """Block until every bundle handed to a writer is finalized on disk."""
        for sink in self._sinks:
            wait = getattr(sink, "wait", None)
            if wait is not None:
                wait(timeout)

    def window_records(self) -> list[dict[str, Any]]:
        """Records currently held in the rolling window, oldest first."""
        return [r for (_, _, r) in self._ring]

    @property
    def incident_open(self) -> bool:
        return self._open is not None

    def stats_dict(self) -> dict[str, Any]:
        s = self.stats
        return {
            "records_in": s.records_in,
            "ring_records": len(self._ring),
            "ring_bytes": self._ring_bytes,
            "ring_evicted_by_cap": s.ring_evicted_by_cap,
            "ring_evicted_by_cap_in_window": s.ring_evicted_by_cap_in_window,
            "incidents_opened": s.incidents_opened,
            "incidents_skipped": s.incidents_skipped,
            "skip_reasons": dict(s.skip_reasons),
            "triggers_fired": s.triggers_fired,
            "triggers_attached": s.triggers_attached,
            "triggers_suppressed_limit": s.triggers_suppressed_limit,
        }
