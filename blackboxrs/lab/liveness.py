"""Telemetry liveness: legitimate inactivity, stale telemetry, transport loss.

Silence detection is not reimplemented here. The replayed stream is fed
into the flight recorder's own :class:`~blackboxrs.flight.core.FlightCore`
(through :class:`~blackboxrs.flight.replay.RecordFeeder`, the same path
``flight replay --retrigger`` uses), and its ``topic_stale`` and
``node_disappeared`` triggers are the detections. Its other triggers
(hold asserted, recovery STOP, arbiter forced zero, manual marker) are kept
as info findings so the timeline shows what the recorder would have fired
on.

On top of those triggers this module only classifies silence:

* an ``event`` topic (joystick, teleop, fault reports) is never judged
  stale: it publishes when something happens. Its silences are reported
  as inactivity in the summary.
* a ``periodic`` topic that goes stale while its host's other periodic
  topics keep arriving is ``stale_telemetry`` (or ``publisher_lost`` when
  the graph shows its publisher node leaving).
* when every periodic topic of one host goes stale together (starts within
  the host's largest staleness window of each other), the finding is one
  ``transport_loss`` for the host. The graph evidence is attached: if the
  publishers are still advertised, the loss matches the documented CycloneDDS
  wrong-interface failure (topics advertised, no data; HELIX
  docs/GO2_FIELD_NOTES.md section 1). That is a match, not a diagnosis.
  A host with a single periodic topic cannot tell the two apart, and the
  finding says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from blackboxrs.flight.core import FlightCore
from blackboxrs.flight.profile import FlightProfile, TopicSpec
from blackboxrs.flight.replay import RecordFeeder
from blackboxrs.lab.events import ReplayEvent, to_record
from blackboxrs.lab.evidence import TopicInfo
from blackboxrs.lab.monitors import Finding
from blackboxrs.lab.values import NS


class _TriggerSink:
    def __init__(self, triggers: list[dict[str, Any]]) -> None:
        self.triggers = triggers

    def open(self, trigger, pre, pw):
        self.triggers.append(trigger)
        return "replay"

    def append(self, record):
        pass

    def add_trigger(self, trigger):
        self.triggers.append(trigger)

    def close(self, status, stats):
        return None

    def wait(self, timeout=None):
        pass


@dataclass
class _Episode:
    topic: str
    host: str
    start_ns: int
    last_eid: str
    publishers: tuple[str, ...]
    end_ns: int | None = None
    limit_s: float = 0.0


@dataclass
class _TopicSeen:
    count: int = 0
    last_ns: int | None = None
    last_eid: str = ""
    longest_silence_ns: int = 0
    silences: list[tuple[int, int]] = field(default_factory=list)


class Liveness:
    name = "liveness"

    def __init__(self, profile: FlightProfile, topics: dict[str, TopicInfo],
                 t0_mono_ns: int, wall0_ns: int) -> None:
        self.topics = topics
        self.t0 = t0_mono_ns
        self.wall0 = wall0_ns
        specs = []
        known = {t.name for t in profile.topics}
        for t in profile.topics:
            info = topics.get(t.name)
            specs.append(replace(t, stale_after_sec=info.stale_after_s) if info else t)
        for name, info in sorted(topics.items()):
            if name not in known and info.liveness == "periodic":
                specs.append(TopicSpec(name=name, type="unknown", role=info.role,
                                       stale_after_sec=info.stale_after_s))
        # The recorder core only needs its triggers here: disable bundle
        # opening limits so a long replay never stops reporting.
        trig = replace(profile.triggers, max_incidents_per_run=1_000_000)
        self.profile = replace(profile, topics=tuple(specs), triggers=trig)
        self.triggers: list[dict[str, Any]] = []
        sink = _TriggerSink(self.triggers)
        self.core = FlightCore(self.profile, lambda: sink)
        self.feeder = RecordFeeder(self.core)
        self._n_trig = 0
        self._seq = 0
        self.seen: dict[str, _TopicSeen] = {}
        self.open: dict[str, _Episode] = {}
        self.episodes: list[_Episode] = []
        self.gone_nodes: list[tuple[int, str]] = []
        self.publishers: dict[str, tuple[str, ...]] = {}
        self.nodes: set[str] = set()

    def _mono(self, t_ns: int) -> int:
        return self.t0 + t_ns

    def on_event(self, e: ReplayEvent) -> list[Finding]:
        self._seq += 1
        if e.kind == "graph":
            pubs = e.record.get("publishers") or {}
            for topic, plist in pubs.items():
                # every node ever seen publishing it: a node that has just
                # left must still count as this topic's publisher
                self.publishers[topic] = tuple(sorted(set(self.publishers.get(topic, ()))
                                                      | set(plist)))
            if e.record.get("full"):
                self.nodes = set(e.record.get("nodes") or [])
            else:
                self.nodes = ((self.nodes - set(e.record.get("nodes_gone") or []))
                              | set(e.record.get("nodes_new") or []))
        if e.kind == "msg" and e.topic:
            s = self.seen.setdefault(e.topic, _TopicSeen())
            if s.last_ns is not None:
                gap = e.t_ns - s.last_ns
                s.longest_silence_ns = max(s.longest_silence_ns, gap)
                if gap > NS:
                    s.silences.append((s.last_ns, e.t_ns))
            s.count += 1
            s.last_ns = e.t_ns
            s.last_eid = e.eid
            ep = self.open.pop(e.topic, None)
            if ep is not None:
                ep.end_ns = e.t_ns
        self.feeder.feed(to_record(e, self.t0, self._seq))
        return self._drain()

    def tick(self, t_ns: int) -> list[Finding]:
        self.core.tick(self._mono(t_ns), self.wall0 + t_ns)
        return self._drain()

    def _drain(self) -> list[Finding]:
        out: list[Finding] = []
        while self._n_trig < len(self.triggers):
            trig = self.triggers[self._n_trig]
            self._n_trig += 1
            t = trig["t_mono_ns"] - self.t0
            typ = trig["type"]
            if typ == "topic_stale":
                topic = trig["topic"]
                s = self.seen.get(topic, _TopicSeen())
                ep = _Episode(topic, self.topics[topic].host if topic in self.topics
                              else "unknown", t, s.last_eid,
                              self.publishers.get(topic, ()), limit_s=trig.get("limit_s", 0.0))
                self.open[topic] = ep
                self.episodes.append(ep)
            elif typ == "node_disappeared":
                self.gone_nodes.append((t, trig["node"]))
                out.append(Finding(t, self.name, "node_disappeared", "warning", trig["node"],
                                   f"node {trig['node']} left the ROS graph (recorder trigger)"))
            else:
                subject = trig.get("topic") or trig.get("note") or typ
                out.append(Finding(t, self.name, f"recorder_trigger:{typ}", "info", subject,
                                   f"flight recorder trigger {typ} fired",
                                   data={k: trig[k] for k in sorted(trig)
                                         if k in ("reason", "fault_id", "action", "status")}))
        return out

    def finish(self, t_end_ns: int) -> list[Finding]:
        out: list[Finding] = []
        used: set[int] = set()
        by_host: dict[str, list[str]] = {}
        for name, info in sorted(self.topics.items()):
            if info.liveness == "periodic" and name in self.seen:
                by_host.setdefault(info.host, []).append(name)
        eps = sorted(enumerate(self.episodes), key=lambda x: (x[1].start_ns, x[1].topic))
        for host, periodic in sorted(by_host.items()):
            if len(periodic) < 2:
                continue
            window = int(round(max(self.topics[t].stale_after_s or 0 for t in periodic) * NS))
            host_eps = [(i, ep) for i, ep in eps if ep.host == host and i not in used]
            for i, first in host_eps:
                if i in used:
                    continue
                group = {first.topic: (i, first)}
                for j, ep in host_eps:
                    if j in used or ep.topic in group:
                        continue
                    if first.start_ns <= ep.start_ns <= first.start_ns + window:
                        group[ep.topic] = (j, ep)
                if set(group) != set(periodic):
                    continue
                latest_start = max(ep.start_ns for _, ep in group.values())
                if any(ep.end_ns is not None and ep.end_ns <= latest_start
                       for _, ep in group.values()):
                    continue
                used.update(j for j, _ in group.values())
                members = [ep for _, ep in sorted(group.values(), key=lambda x: x[1].topic)]
                graph = self._graph_verdict(members, latest_start)
                ends = [ep.end_ns for ep in members]
                recovered = None if any(x is None for x in ends) else max(ends)
                note = {
                    "publishers_still_advertised":
                        "publishers still advertised on the graph while no data arrives: "
                        "matches the DDS bound-to-the-wrong-interface signature",
                    "publishers_gone": "publisher nodes left the graph",
                    "no_graph_evidence": "no graph evidence for these publishers",
                }[graph]
                out.append(Finding(
                    first.start_ns, self.name, "transport_loss", "warning", host,
                    f"every periodic topic from host {host} went silent together "
                    f"({', '.join(ep.topic for ep in members)}); {note}",
                    tuple(ep.last_eid for ep in members if ep.last_eid),
                    data={"topics": [ep.topic for ep in members], "graph": graph,
                          "recovered_t_ns": recovered}))
        for i, ep in eps:
            if i in used:
                continue
            gone = [n for (t, n) in self.gone_nodes
                    if n in ep.publishers and t <= ep.start_ns + int(ep.limit_s * NS)]
            single = len(by_host.get(ep.host, [])) < 2
            if gone:
                kind, why = "publisher_lost", f"its publisher {', '.join(gone)} left the graph"
            else:
                kind = "stale_telemetry"
                why = ("other periodic topics from the same host kept arriving" if not single
                       else f"it is the only periodic topic from host {ep.host}, so a transport "
                            "loss cannot be told apart")
            out.append(Finding(
                ep.start_ns, self.name, kind, "warning", ep.topic,
                f"{ep.topic} silent past {ep.limit_s:g} s; {why}",
                tuple(x for x in (ep.last_eid,) if x),
                data={"host": ep.host, "recovered_t_ns": ep.end_ns}))
        return out

    def _graph_verdict(self, members: list[_Episode], t_ns: int) -> str:
        pubs = {p for ep in members for p in ep.publishers}
        if not pubs:
            return "no_graph_evidence"
        gone = {n for (t, n) in self.gone_nodes if t <= t_ns}
        return "publishers_gone" if pubs <= gone else "publishers_still_advertised"

    def summary(self, t_end_ns: int) -> dict[str, Any]:
        out = {}
        for name, info in sorted(self.topics.items()):
            s = self.seen.get(name)
            row: dict[str, Any] = {"liveness": info.liveness, "host": info.host,
                                   "stale_after_s": info.stale_after_s,
                                   "messages": s.count if s else 0}
            if s and s.last_ns is not None:
                row["silent_at_end_s"] = round((t_end_ns - s.last_ns) / NS, 6)
                row["longest_silence_s"] = round(max(s.longest_silence_ns,
                                                     t_end_ns - s.last_ns) / NS, 6)
                if info.liveness == "event":
                    row["classification"] = "event topic: silence is inactivity, never stale"
            out[name] = row
        return out
