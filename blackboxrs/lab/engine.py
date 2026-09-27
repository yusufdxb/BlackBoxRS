"""Replay engine: evidence -> faults -> ordered dispatch -> findings -> verdict.

Dispatch order at one replay instant: every event at time ``t`` (in
``(t, order)`` order), then the arbitration tick at ``t`` if one falls
there. Ticks are generated at ``window_start + k * period``: they are
the arbiter's timer (50 Hz by default) and the moments the monitors sample
the robot-facing command.

Within one event, handlers run in a fixed order: timeline, system under
test, monitors, liveness. Within one tick: system under test, timeline,
monitors, liveness. No handler sees the wall clock.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from blackboxrs.lab.clock import Pacer, ReplayClock
from blackboxrs.lab.events import ReplayEvent, normalize
from blackboxrs.lab.evidence import Evidence, EvidenceError, topic_table
from blackboxrs.lab.faults import Fault, FaultError, apply_faults
from blackboxrs.lab.liveness import Liveness
from blackboxrs.lab.monitors import (
    ClockMonitor,
    CommandPath,
    ConsistentState,
    Finding,
    OdometryConsistency,
    StopDominance,
)
from blackboxrs.lab.helix import HelixArbiterAdapter
from blackboxrs.lab.sut import ArbiterConfig, ObservedOutput, TwistMuxModel, build_config
from blackboxrs.lab.timeline import Timeline
from blackboxrs.lab.transport import analyze_delivered
from blackboxrs.lab.values import NS, digest, jsonable
from blackboxrs.lab.verdict import compute_verdict, summarize

RESULT_SCHEMA = "blackboxrs.lab.result.v1"
# Roles downstream of the arbiter. In reference mode the model produces the
# arbiter output, so the recorded one (and what the sink and robot did with
# it) is not replayed; the count is reported.
DOWNSTREAM_ROLES = frozenset({"cmd_vel_out", "arbiter_status", "sink_trace", "sport_request",
                              "sport_response"})


@dataclass(frozen=True)
class ReplayConfig:
    sut_mode: str = "reference"                  # reference | observed
    preset: str = "helix_arbiter"
    overrides: dict[str, Any] = field(default_factory=dict)
    faults: tuple[Fault, ...] = ()
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)
    from_s: float | None = None
    to_s: float | None = None
    command_sources: dict[str, float] = field(default_factory=dict)   # observed mode
    stop_grace_s: float = 0.05
    fresh_grace_s: float = 0.05
    clock_step_threshold_s: float = 0.2
    clock_offset_info_s: float = 0.5
    observed_period_s: float = 0.02

    def describe(self) -> dict[str, Any]:
        d = asdict(self)
        d["faults"] = [f.describe() for f in self.faults]
        return d


def _validate(cfg: ReplayConfig) -> None:
    def real(name: str, v: object) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            raise ValueError(f"{name} must be a number, got {v!r}")
        return float(v)
    for name in ("from_s", "to_s"):
        v = getattr(cfg, name)
        if v is not None and real(name, v) < 0:
            raise ValueError(f"{name} must be >= 0")
    # A grace window is tolerance for arbiter tick and transport delay. Past
    # half a second it would hide the very violations it is meant to judge.
    for name in ("stop_grace_s", "fresh_grace_s"):
        if not 0.0 <= real(name, getattr(cfg, name)) <= 0.5:
            raise ValueError(f"{name} must be within [0, 0.5] s")
    for name in ("clock_step_threshold_s", "clock_offset_info_s", "observed_period_s"):
        if not 0.0 < real(name, getattr(cfg, name)) <= 10.0:
            raise ValueError(f"{name} must be within (0, 10] s")


def _sut(cfg: ReplayConfig) -> ArbiterConfig | None:
    if cfg.sut_mode == "reference":
        return build_config(cfg.preset, cfg.overrides)
    if cfg.sut_mode != "observed":
        raise ValueError(f"sut mode must be reference or observed, not {cfg.sut_mode!r}")
    if cfg.overrides:
        raise ValueError("sut overrides apply to reference mode only")
    return None


def replay(ev: Evidence, cfg: ReplayConfig, *, pacer: Pacer | None = None,
           observer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    pacer = pacer or Pacer()
    _validate(cfg)
    arb_cfg = _sut(cfg)
    topics = topic_table(ev, cfg.topics)
    events, skipped = normalize(ev)
    recorded = list(events)          # the evidence as recorded, before any fault
    # apply_faults may add a topic to the table (inject_stream on a new topic)
    events, faultlog = apply_faults(events, list(cfg.faults), topics)
    by_role: dict[str, list[str]] = {}
    for name, info in sorted(topics.items()):
        by_role.setdefault(info.role, []).append(name)

    # --- window ---------------------------------------------------------------
    lo = int(round((cfg.from_s or 0.0) * NS))
    hi = None if cfg.to_s is None else int(round(cfg.to_s * NS))
    if hi is not None and hi <= lo:
        raise EvidenceError("--to must be after --from")
    events = [e for e in events if e.t_ns >= lo and (hi is None or e.t_ns < hi)]

    # --- system under test ----------------------------------------------------
    anchor = next((e for e in events if e.kind == "msg" and e.rx_wall_ns), None)
    if anchor is None:
        raise EvidenceError("no message in the replay window")
    wall0 = anchor.rx_wall_ns - anchor.t_ns
    suppressed: dict[str, int] = {}
    if arb_cfg is not None:
        downstream = {n for n, t in topics.items() if t.role in DOWNSTREAM_ROLES}
        for f in faultlog:
            if f["topics_affected"] and set(f["topics_affected"]) <= downstream:
                raise FaultError(
                    f"fault {f['id']} only touches recorded arbiter outputs "
                    f"{f['topics_affected']}, which reference mode replaces with the model's "
                    "output, so it would have no effect; use --sut observed")
        kept = []
        for e in events:
            if e.kind == "msg" and e.role in DOWNSTREAM_ROLES:
                suppressed[e.topic] = suppressed.get(e.topic, 0) + 1
            else:
                kept.append(e)
        events = kept
        if arb_cfg.preset == "helix_arbiter":
            sut: Any = HelixArbiterAdapter(arb_cfg, wall0_ns=wall0)
        else:
            # the recovery node's zero twists, unless the evidence recorded them
            has_recovery = any(e.kind == "msg" and e.topic == arb_cfg.recovery_topic
                               for e in events)
            sut = TwistMuxModel(arb_cfg, derive_recovery_from_hold=not has_recovery)
        period = int(round(arb_cfg.period_s * NS))
        sources = {s.topic: s.timeout_s for s in arb_cfg.sources
                   if s.topic != arb_cfg.recovery_topic}
    else:
        sut = ObservedOutput(by_role)
        period = int(round(cfg.observed_period_s * NS))
        sources = dict(cfg.command_sources) or {t: 0.5 for t in by_role.get("cmd_vel_source", [])}
    unknown = sorted(set(sources) - set(topics))
    hold_topics = set(by_role.get("helix_hold", []))

    # --- detectors ------------------------------------------------------------
    stop = StopDominance(hold_topics, set(sources), cfg.stop_grace_s,
                         arb_cfg.hold_timeout_s if arb_cfg is not None else 0.5)
    # The oracle also follows the hold as recorded, so a fault on the STOP
    # signal cannot erase a STOP that the evidence contains.
    shadow = [e for e in recorded if e.kind == "msg" and e.topic in hold_topics
              and e.t_ns >= lo and (hi is None or e.t_ns < hi)]
    recorded_hold_at = [e.t_ns for e in recorded if e.kind == "msg" and e.topic in hold_topics
                        and (e.data or {}).get("hold") is True]
    cmd = CommandPath(sources, cfg.fresh_grace_s)
    monitors = [stop, cmd, ConsistentState(set(by_role.get("arbiter_status", [])) if
                                           arb_cfg is None else set()),
                ClockMonitor({n: t.host for n, t in topics.items()}, cfg.clock_step_threshold_s,
                             cfg.clock_offset_info_s),
                OdometryConsistency(set(by_role.get("odometry", [])))]
    t0_mono = ev.t0_mono_ns
    live = Liveness(ev.profile, topics, t0_mono, wall0)
    timeline = Timeline(faultlog, hold_topics, set(sources), observer)
    findings: list[tuple[str, Finding]] = []

    def emit(fs: list[Finding]) -> None:
        for f in fs:
            tmp = f"tmp{len(findings)}"
            findings.append((tmp, f))
            timeline.finding(f, tmp)

    # --- dispatch ---------------------------------------------------------------
    clock = ReplayClock(lo)
    t_last = events[-1].t_ns if events else lo
    next_tick = lo
    ticks = 0
    delivered: list[ReplayEvent] = []
    publications = 0
    i = j = 0

    def shadow_until(t: int) -> None:
        nonlocal j
        while j < len(shadow) and shadow[j].t_ns <= t:
            stop.on_recorded(shadow[j])
            j += 1

    while i < len(events) or next_tick <= t_last:
        if i < len(events) and events[i].t_ns <= next_tick:
            e = events[i]
            i += 1
            pacer.wait(clock.advance_to(e.t_ns))
            shadow_until(e.t_ns)
            timeline.advance(e.t_ns)
            timeline.input(e)
            sut.on_event(e, e.t_ns)
            for m in monitors:
                emit(m.on_event(e))
            emit(live.on_event(e))
            delivered.append(e)
            # A callback publisher (legacy mux, or a recorded output message)
            # can put a command on the robot between ticks: judge it now.
            pub = sut.take_publication(e.t_ns)
            if pub is not None:
                publications += 1
                timeline.decision(pub)
                for m in monitors:
                    emit(m.on_decision(pub))
        else:
            t = next_tick
            next_tick += period
            ticks += 1
            pacer.wait(clock.advance_to(t))
            shadow_until(t)
            timeline.advance(t)
            d = sut.tick(t)
            timeline.decision(d)
            for m in monitors:
                emit(m.on_decision(d))
            emit(live.tick(t))
    t_end = clock.now_ns
    for m in monitors:
        emit(m.finish(t_end))
    emit(live.finish(t_end))
    tfind, transport = analyze_delivered(ev.manifest, delivered, t0_mono, t_end)
    emit(tfind)

    # --- stable ids -------------------------------------------------------------
    ordered = sorted(findings, key=lambda x: x[1].sort_key())
    fid = {tmp: f"D{n + 1:03d}" for n, (tmp, _) in enumerate(ordered)}
    tl = timeline.result()
    for entry in tl:
        if "finding" in entry:
            entry["finding"] = fid[entry["finding"]]
    topic_of = {e.eid: e.topic for e in delivered if e.topic}

    def related(f: Finding) -> list[str]:
        # Association, not causation: faults that touched a topic this finding
        # names (subject or evidence) and began at or before it. A dropped
        # message leaves no event to link, so this is how a drop is tied to
        # the finding it produced.
        names = {f.subject} | {topic_of[x] for x in f.evidence if x in topic_of}
        return [x["id"] for x in faultlog
                if names & set(x["topics_affected"]) and x["first_t_ns"] <= f.t_ns]

    finding_rows = [{"id": fid[tmp], "t_ns": f.t_ns, "t_s": round(f.t_ns / NS, 9),
                     "monitor": f.monitor, "kind": f.kind, "severity": f.severity,
                     "subject": f.subject, "message": f.message, "evidence": list(f.evidence),
                     "invariant": f.invariant, "related_faults": related(f),
                     "data": jsonable(f.data)}
                    for tmp, f in ordered]
    rel = {r["id"]: r["related_faults"] for r in finding_rows}
    for entry in tl:
        if entry.get("finding") and rel[entry["finding"]]:
            entry["related_faults"] = rel[entry["finding"]]
    invariants = {inv.name: inv.to_dict() for m in monitors for inv in m.invariants()}

    def incomplete(names: tuple[str, ...], reason: str) -> None:
        for name in names:
            if invariants[name]["status"] != "FAIL":
                invariants[name]["status"] = "INCOMPLETE"
                invariants[name]["incomplete_reason"] = reason

    # A replay that never saw what it needs must not pass by default.
    seen_topics = {e.topic for e in delivered if e.kind == "msg"}
    output_invariants = ("finite_output", "fresh_output", "stop_dominance")
    if arb_cfg is None and not sut.available:
        incomplete(output_invariants,
                   "evidence records no arbitration output (/cmd_vel or ArbiterStatus)")
    if not seen_topics & set(sources):
        incomplete(output_invariants if arb_cfg is not None else ("fresh_output",),
                   f"no message on any command source {sorted(sources)} in the replay; "
                   "the arbitration path was never exercised")
    if recorded_hold_at and invariants["stop_dominance"]["status"] == "NOT_EXERCISED":
        incomplete(("stop_dominance",),
                   f"the evidence asserts a hold at {recorded_hold_at[0] / NS:.3f} s, but no "
                   "robot-facing command was judged against it in this replay window")
    if hold_topics and not seen_topics & hold_topics and recorded_hold_at:
        incomplete(output_invariants,
                   "the evidence has hold messages but none reached the system under test")
    if arb_cfg is not None and arb_cfg.preset == "helix_arbiter" and \
            arb_cfg.hold_topic not in seen_topics:
        incomplete(output_invariants,
                   f"no {arb_cfg.hold_topic} message in the replay: the model holds zero "
                   "(HELIX_STATE_MISSING) for the whole run, which proves nothing")

    config = cfg.describe()
    config["sut"] = ({"mode": "reference", **arb_cfg.to_dict()} if arb_cfg is not None
                     else {"mode": "observed", "output_source": sut.source_label,
                           "command_sources": dict(sorted(sources.items()))})
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "run_id": digest({"evidence": ev.digest, "config": config}),
        "evidence": {"source": ev.source, "digest": ev.digest, "synthetic": ev.synthetic,
                     "partial": ev.partial, "problems": ev.read_info.get("problems", []),
                     "records": len(ev.records), "profile": ev.profile.name,
                     "profile_sha256": ev.profile.sha256},
        "config": config,
        "topics": {n: asdict(t) for n, t in sorted(topics.items())},
        "injections": faultlog,
        "replay": {
            "window_start_ns": lo, "window_end_ns": t_end, "events_delivered": len(delivered),
            "ticks": ticks, "period_ns": period, "publications_between_ticks": publications,
            "suppressed_recorded_outputs": dict(sorted(suppressed.items())),
            "skipped_record_kinds": skipped,
            "command_sources_not_in_evidence": unknown,
            "notes": ([f"replay starts at {lo / NS:g} s: state before it (holds, commands) "
                       "is not replayed"] if lo > 0 else []),
        },
        "findings": finding_rows,
        "invariants": dict(sorted(invariants.items())),
        "liveness": live.summary(t_end),
        "clock": monitors[3].summary(),
        "transport": transport,
        "sut_state": sut.state(),
        "stop_oracle": {"hold_messages_ignored_as_older": stop.ignored_older},
        "timeline": tl,
    }
    result["verdict"] = compute_verdict(result)
    result["summary"] = summarize(result)
    return jsonable(result)

