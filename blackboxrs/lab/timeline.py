"""Causal timeline: input -> detector -> safety state -> decision -> robot output.

The timeline keeps state changes, not every message: the first event of each
fault, hold messages that change the hold value, command-source messages
that change that source's command or its validity, arbitration decisions
whose reason or winner changed, robot-facing commands that changed, and
every finding. Each entry lists the entries it follows from (``caused_by``)
and the replay event ids that are its evidence (``events``), so a reader can
walk a violation back to the injected fault.

A decision's cause is the last input the arbitration path handled before
that tick, or ``clock`` when only time passed (a timeout). Entry ids are
assigned after the replay, in time order, so they are stable.
"""

from __future__ import annotations

from typing import Any, Callable

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.monitors import Finding
from blackboxrs.lab.sut import Decision
from blackboxrs.lab.values import NS, as_number, canonical_json, fmt_s


def _fmt_cmd(raw: tuple[Any, ...] | None) -> str:
    if raw is None:
        return "none"
    parts = []
    for v in raw:
        n, p = as_number(v)
        parts.append(repr(v) if p == "malformed" else f"{n:.3f}" if not p else str(n))
    return "(" + ", ".join(parts) + ")"


class Timeline:
    def __init__(self, faultlog: list[dict[str, Any]], hold_topics: set[str],
                 source_topics: set[str],
                 observer: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.entries: list[dict[str, Any]] = []
        self._by_eid: dict[str, int] = {}
        self._fault_entry: dict[str, int] = {}
        self._pending = sorted(faultlog, key=lambda f: (f["first_t_ns"], f["id"]))
        self.hold_topics = hold_topics
        self.source_topics = source_topics
        self.observer = observer
        self._last_hold: bool | None = None
        self._last_src: dict[str, str] = {}
        self._last_dec: tuple[str, str] | None = None
        self._last_out: str | None = None
        self._last_out_entry: int | None = None
        self.events: dict[str, ReplayEvent] = {}

    def _add(self, t_ns: int, layer: str, text: str, caused_by: list[int] | None = None,
             events: list[str] | None = None, **extra: Any) -> int:
        entry = {"t_ns": t_ns, "layer": layer, "text": text,
                 "caused_by": sorted(set(caused_by or [])), "events": list(events or []),
                 **extra}
        self.entries.append(entry)
        if self.observer is not None:
            self.observer({**entry, "t": fmt_s(t_ns)})
        return len(self.entries) - 1

    # -- faults and inputs ---------------------------------------------------

    def advance(self, t_ns: int) -> None:
        while self._pending and self._pending[0]["first_t_ns"] <= t_ns:
            f = self._pending.pop(0)
            p = ", ".join(f"{k}={v}" for k, v in f["params"].items() if v is not None)
            self._fault_entry[f["id"]] = self._add(
                f["first_t_ns"], "fault", f"fault {f['id']} {f['kind']} ({p}): "
                f"{f['events_affected']} event(s) affected", events=f["first_events"][:3],
                fault=f["id"])

    def _fault_links(self, e: ReplayEvent) -> list[int]:
        return [self._fault_entry[f] for f in e.injected if f in self._fault_entry]

    def _describe(self, e: ReplayEvent) -> str:
        d = e.data or {}
        if e.topic in self.hold_topics:
            return (f"{e.topic} hold={str(d.get('hold')).lower()} fault_id="
                    f"{d.get('fault_id') or '-'} epoch={d.get('epoch')} seq={d.get('seq')}")
        if e.topic in self.source_topics:
            lin, ang = d.get("linear") or {}, d.get("angular") or {}
            return f"{e.topic} command {_fmt_cmd((lin.get('x'), lin.get('y'), ang.get('z')))}"
        return f"{e.topic or e.kind} message"

    def input(self, e: ReplayEvent) -> None:
        self.events[e.eid] = e
        if e.kind != "msg" or e.data is None:
            return
        changed = False
        if e.topic in self.hold_topics:
            hold = e.data.get("hold")
            if isinstance(hold, bool) and hold != self._last_hold:
                self._last_hold = hold
                changed = True
        elif e.topic in self.source_topics:
            lin = e.data.get("linear") or {}
            ang = e.data.get("angular") or {}
            sig = canonical_json([lin.get("x"), lin.get("y"), ang.get("z")])
            if sig != self._last_src.get(e.topic):
                self._last_src[e.topic] = sig
                changed = True
        if changed:
            self._by_eid[e.eid] = self._add(e.t_ns, "input", self._text(e),
                                            self._fault_links(e), [e.eid])

    def _text(self, e: ReplayEvent) -> str:
        text = self._describe(e)
        if e.injected:
            text += f" [injected by {', '.join(e.injected)}]"
        return text

    def _entry_for(self, eid: str) -> int | None:
        if eid in self._by_eid:
            return self._by_eid[eid]
        e = self.events.get(eid)
        if e is None:
            return None
        self._by_eid[eid] = self._add(e.t_ns, "input", self._text(e), self._fault_links(e),
                                      [eid])
        return self._by_eid[eid]

    # -- decisions and outputs ----------------------------------------------

    def _cause(self, d: Decision) -> list[int]:
        if d.cause == "clock":
            return []
        return [x for x in [self._entry_for(d.cause)] if x is not None]

    def decision(self, d: Decision) -> None:
        dec_entry = None
        key = (d.reason, d.source)
        if key != self._last_dec:
            self._last_dec = key
            cause = self._cause(d)
            why = "time-driven, no new input" if d.cause == "clock" else f"after {d.cause}"
            silent = (": no live input, nothing is published, the robot-facing sink keeps "
                      f"{_fmt_cmd(d.robot_raw)}" if d.reason == "SILENT" else "")
            dec_entry = self._add(d.t_ns, "decision",
                                  f"arbitration {d.reason}"
                                  + (f", winner {d.source}" if d.source else "") + f" ({why})"
                                  + silent,
                                  cause, [] if d.cause == "clock" else [d.cause])
        out = canonical_json(list(d.robot_raw) if d.robot_raw is not None else None)
        if out != self._last_out:
            self._last_out = out
            self._last_out_entry = self._add(
                d.t_ns, "output", f"robot-facing command {_fmt_cmd(d.robot_raw)}",
                [dec_entry] if dec_entry is not None else self._cause(d))

    # -- findings -------------------------------------------------------------

    def finding(self, f: Finding, fid: str) -> None:
        causes = [x for x in (self._entry_for(eid) for eid in f.evidence) if x is not None]
        if f.invariant and self._last_out_entry is not None:
            causes.append(self._last_out_entry)
        layer = "invariant" if f.invariant else "detector"
        self._add(f.t_ns, layer, f"[{f.severity}] {f.kind} {f.subject}: {f.message}", causes,
                  list(f.evidence), finding=fid)

    # -- result -----------------------------------------------------------------

    def result(self) -> list[dict[str, Any]]:
        order = sorted(range(len(self.entries)), key=lambda i: (self.entries[i]["t_ns"], i))
        new_id = {old: f"T{n + 1:04d}" for n, old in enumerate(order)}
        out = []
        for old in order:
            e = dict(self.entries[old])
            e["id"] = new_id[old]
            e["t_s"] = round(e["t_ns"] / NS, 9)
            e["caused_by"] = sorted(new_id[c] for c in e["caused_by"])
            out.append(e)
        return out
