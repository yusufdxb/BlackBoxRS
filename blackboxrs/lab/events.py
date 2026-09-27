"""Replay events and their total order.

Every event gets a replay time ``t_ns`` (nanoseconds since the start of the
replayed evidence, on the recorder's monotonic receipt clock) and an
``order`` key. The replay order is ``(t_ns, order)``, which is total:

* evidence events: ``order = (0, seq, sub)``. Equal receipt times are
  broken by the recorder's ingest sequence ``seq``, which is the order the
  recorder actually handled them. ``sub`` is 0 for the recorded event and
  1, 2, ... for copies a fault injector derives from it (duplicates).
* events a fault injector synthesizes from nothing (an injected teleop
  stream): ``order = (1, fault_index, k)``. At an equal time they come after
  every evidence event, in fault order, then in generation order.

No ordering ever depends on dict or set iteration, object identity or the
wall clock. ``check_total_order`` refuses two events with the same key.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from blackboxrs.lab.evidence import INPUT_KINDS, Evidence, EvidenceError


@dataclass(frozen=True)
class ReplayEvent:
    t_ns: int
    order: tuple[int, int, int]
    eid: str
    kind: str
    topic: str | None = None
    role: str | None = None
    type: str | None = None
    data: dict[str, Any] | None = None
    # publisher-side times (see blackboxrs/flight/records.py)
    src_ns: int | None = None          # DDS source timestamp, publisher host wall clock
    rx_wall_ns: int | None = None      # DDS reception (or callback) time, recorder wall clock
    pub_stamp_s: float | None = None   # stamp embedded in the message
    pub_stamp_domain: str | None = None
    injected: tuple[str, ...] = ()     # ids of the faults that created or changed this event
    record: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> tuple[int, tuple[int, int, int]]:
        return (self.t_ns, self.order)

    def touched(self, fault_id: str, **changes: Any) -> "ReplayEvent":
        """A modified copy that records which fault changed it."""
        tags = self.injected if fault_id in self.injected else self.injected + (fault_id,)
        return replace(self, injected=tags, **changes)


def from_record(r: dict[str, Any], t0_mono_ns: int) -> ReplayEvent:
    rx = r.get("dds_rx_ns") or r.get("t_wall_ns")
    return ReplayEvent(
        t_ns=r["t_mono_ns"] - t0_mono_ns,
        order=(0, r["seq"], 0),
        eid=f"r{r['seq']}",
        kind=r["kind"],
        topic=r.get("topic"),
        role=r.get("role"),
        type=r.get("type"),
        data=r.get("data"),
        src_ns=r.get("dds_src_ns") or None,
        rx_wall_ns=rx,
        pub_stamp_s=r.get("pub_stamp_s"),
        pub_stamp_domain=r.get("pub_stamp_domain"),
        record=r,
    )


def normalize(ev: Evidence) -> tuple[list[ReplayEvent], dict[str, int]]:
    """Evidence records -> ordered replay events, plus counts of skipped kinds."""
    t0 = ev.t0_mono_ns
    out: list[ReplayEvent] = []
    skipped: dict[str, int] = {}
    for r in ev.records:
        if r["kind"] not in INPUT_KINDS:
            skipped[r["kind"]] = skipped.get(r["kind"], 0) + 1
            continue
        out.append(from_record(r, t0))
    out.sort(key=lambda e: e.key)
    return out, dict(sorted(skipped.items()))


def check_total_order(events: list[ReplayEvent]) -> None:
    keys = [e.key for e in events]
    if len(set(keys)) != len(keys):
        raise EvidenceError("two replay events share one (time, order) key; "
                            "the replay order would be ambiguous")
    ids = [e.eid for e in events]
    if len(set(ids)) != len(ids):
        raise EvidenceError("two replay events share one event id")


def to_record(e: ReplayEvent, t0_mono_ns: int, seq: int) -> dict[str, Any]:
    """Back to a flight record (delivery order ``seq``) for the flight analyzers."""
    r = dict(e.record)
    shift = e.t_ns + t0_mono_ns - r.get("t_mono_ns", e.t_ns + t0_mono_ns)
    r.update({"kind": e.kind, "seq": seq, "t_mono_ns": e.t_ns + t0_mono_ns,
              "t_wall_ns": r.get("t_wall_ns", 0) + shift})
    if e.kind == "msg":
        r.update({"topic": e.topic, "role": e.role, "type": e.type, "data": e.data,
                  "dds_src_ns": e.src_ns, "dds_rx_ns": e.rx_wall_ns,
                  "pub_stamp_s": e.pub_stamp_s, "pub_stamp_domain": e.pub_stamp_domain})
    return r
