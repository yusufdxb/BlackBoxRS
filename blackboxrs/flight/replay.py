"""Offline replay of a flight bundle.

Two levels:

* ``regenerate``: re-run the analysis on the recorded manifest and records.
  The analysis is a pure function, so the regenerated report must equal the
  stored one; any difference is reported.
* ``retrigger``: stream the recorded messages, graph changes and markers back
  through a fresh recorder core built from the profile embedded in the
  bundle, and list the triggers it fires. The recorded pre-trigger window
  must reproduce the recorded primary trigger.

Neither touches ROS or DDS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blackboxrs.flight.analysis import analyze
from blackboxrs.flight.bundle import load_bundle
from blackboxrs.flight.core import FlightCore
from blackboxrs.flight.profile import profile_from_text

_NS = 1_000_000_000


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def regenerate(path: str | Path) -> dict[str, Any]:
    manifest, records, info = load_bundle(path)
    report = analyze(manifest, records)
    stored_path = Path(path) / "report.json"
    stored = None
    if stored_path.exists():
        try:
            stored = json.loads(stored_path.read_text(encoding="utf-8"))
        except ValueError:
            stored = None
    diff: list[str] = []
    if stored is not None:
        for k in sorted(set(stored) | set(report)):
            if canonical(stored.get(k)) != canonical(report.get(k)):
                diff.append(k)
    return {"report": report, "read_info": info, "stored_report": stored is not None,
            "identical": stored is not None and not diff, "differs_in": diff}


class _MemSink:
    def __init__(self) -> None:
        self.triggers: list[dict[str, Any]] = []

    def open(self, trigger: dict[str, Any], pre: list[dict[str, Any]], pw: dict[str, Any]) -> str:
        self.triggers.append(trigger)
        return "replay"

    def append(self, record: dict[str, Any]) -> None:
        pass

    def add_trigger(self, trigger: dict[str, Any]) -> None:
        self.triggers.append(trigger)

    def close(self, status: str, stats: dict[str, Any]) -> str | None:
        return None

    def wait(self, timeout: float | None = None) -> None:
        pass


class RecordFeeder:
    """Stream recorded records back into a :class:`FlightCore`.

    Messages and system samples are re-ingested as recorded. Graph records
    are rebuilt into full node sets (a diff alone does not give the node
    set, so diffs before the first full snapshot are skipped) and passed to
    ``FlightCore.graph`` so node-disappearance triggers fire as they did
    live. Markers go through ``FlightCore.mark``. Records of other kinds
    (triggers, health, clock jumps) are outputs of the core, not inputs, and
    are ignored. Used by ``retrigger`` and by the replay lab.
    """

    def __init__(self, core: FlightCore) -> None:
        self.core = core
        self.nodes: set[str] | None = None

    def feed(self, r: dict[str, Any]) -> None:
        kind = r.get("kind")
        t = r.get("t_mono_ns")
        if kind == "msg" or kind == "sys":
            self.core.ingest({k: v for k, v in r.items() if k != "seq"})
        elif kind == "graph":
            if r.get("full"):
                self.nodes = set(r.get("nodes") or [])
            elif self.nodes is None:
                return  # no full snapshot yet: a diff alone does not give the node set
            else:
                self.nodes = ((self.nodes - set(r.get("nodes_gone") or []))
                              | set(r.get("nodes_new") or []))
            self.core.graph(t, r["t_wall_ns"], sorted(self.nodes), {},
                            r.get("publishers") or {})
        elif kind == "marker":
            self.core.mark(t, r["t_wall_ns"], note=r.get("note", ""),
                           source=r.get("source", ""))


def retrigger(path: str | Path) -> dict[str, Any]:
    manifest, records, _ = load_bundle(path)
    text = (manifest.get("profile") or {}).get("text")
    if not text:
        return {"ok": False, "reason": "bundle has no embedded profile text"}
    profile = profile_from_text(text)
    sinks: list[_MemSink] = []

    def factory() -> _MemSink:
        sinks.append(_MemSink())
        return sinks[-1]

    core = FlightCore(profile, factory)
    tick_ns = int(profile.sampling.health_tick_sec * _NS)
    feeder = RecordFeeder(core)
    next_tick = None
    for r in sorted(records, key=lambda x: x.get("seq", 0)):
        t = r.get("t_mono_ns")
        if t is not None:
            if next_tick is None:
                next_tick = t + tick_ns
            while next_tick <= t:
                core.tick(next_tick, r["t_wall_ns"] - (t - next_tick))
                next_tick += tick_ns
        feeder.feed(r)
    fired = [tr for s in sinks for tr in s.triggers]
    recorded = [t for t in manifest.get("triggers") or [] if t.get("role") != "note"]
    prim = recorded[0] if recorded else None
    match = None
    if prim is not None:
        match = next((f for f in fired if f["type"] == prim["type"]
                      and abs(f["t_mono_ns"] - prim["t_mono_ns"]) <= tick_ns), None)
    return {
        "ok": match is not None,
        "recorded_primary": None if prim is None else {k: prim.get(k) for k in
                                                         ("type", "t_mono_ns", "topic", "node")},
        "replayed": [{k: f.get(k) for k in ("type", "t_mono_ns", "topic", "node")} for f in fired],
        "note": ("a graph-change or staleness trigger fired by the live recorder between two "
                 "records is reproduced to within one health tick"),
    }
