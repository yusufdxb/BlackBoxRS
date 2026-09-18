"""Incident report from a bundle's manifest and records.

``analyze(manifest, records)`` is a pure function: no clock reads, no
filesystem, no randomness. Replaying a bundle regenerates the same report
byte for byte (``tests/unit/flight/test_replay.py``).

Timing rules (also in docs/FLIGHT_RECORDER.md):

* An interval between two stages is computed on EMISSION times only when
  both publishers share a clock: both roles are listed in the profile's
  ``co_hosted_roles`` and both records carry a DDS source timestamp, or
  both carry an embedded stamp in the same declared domain.
* Otherwise it is computed on the recorder's RECEIPT times (DDS reception
  timestamp when both have one and no wall-clock jump was seen, else the
  recorder monotonic clock). A receipt interval includes transport and
  queueing on both legs; it is labelled ``receipt`` and is an observation
  interval, not a causal latency.
* Stages that were not observed are reported as not observed. Nothing is
  interpolated.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

REPORT_SCHEMA = "blackboxrs.flight.report.v1"
_NS = 1_000_000_000
DURING_SEC = 2.0            # "during" phase: [trigger, trigger + DURING_SEC)
RECEIPT_ORDER_TOL_NS = 50_000_000
STOP_API_ID = 1003
MOVE_API_ID = 1008

CHAIN = (
    "fault", "diagnosis", "recovery_hint", "recovery_action", "helix_hold",
    "arbiter_forced_zero", "cmd_vel_zero", "sport_stopmove_request",
    "sport_response", "odometry_stopped",
)
# "diagnosis" is the HELIX diagnosis node's output, which is the RecoveryHint
# itself; the hint stage is reported once under recovery_hint and diagnosis
# refers to it. Keep both names so the report reads like the HELIX chain.
_STAGE_ROLE = {
    "fault": "helix_fault", "diagnosis": "recovery_hint", "recovery_hint": "recovery_hint",
    "recovery_action": "recovery_action", "helix_hold": "helix_hold",
    "arbiter_forced_zero": "arbiter_status", "cmd_vel_zero": "cmd_vel_out",
    "sport_stopmove_request": "sport_request", "sport_response": "sport_response",
    "odometry_stopped": "odometry",
}


def _r(x: float | None, nd: int = 6) -> float | None:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), nd)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if v in ("NaN", "Infinity", "-Infinity"):
        return float(v.replace("Infinity", "inf"))
    return None


def _get(d: Any, path: str, default: Any = None) -> Any:
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _twist_zero(data: dict[str, Any]) -> bool | None:
    vals = [_num(_get(data, f"{a}.{b}")) for a in ("linear", "angular") for b in "xyz"]
    if any(v is None for v in vals):
        return None
    return all(v == 0.0 for v in vals)


def _twist(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    return {"linear": _get(data, "linear"), "angular": _get(data, "angular")}


def _pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------


class _Clocks:
    def __init__(self, records: list[dict[str, Any]], co_hosted: set[str]) -> None:
        self.co_hosted = co_hosted
        self.jumps = [r for r in records if r.get("kind") == "clock_jump"]
        # Recorder receipt delay: callback wall time minus DDS source time on
        # co-hosted topics. Meaningful as the recorder's transport + queueing
        # delay only when the recorder runs on the same host as those
        # publishers (the Stage E setup); otherwise it also contains skew.
        d = sorted((r["t_wall_ns"] - r["dds_src_ns"]) / _NS for r in records
                   if r.get("kind") == "msg" and r.get("dds_src_ns")
                   and r.get("role") in co_hosted)
        self.delay = None
        if len(d) >= 20:
            self.delay = {"n": len(d), "p05_s": _r(_pctl(d, 0.05)), "p50_s": _r(_pctl(d, 0.5)),
                          "p95_s": _r(_pctl(d, 0.95)), "max_s": _r(d[-1]),
                          "spread_p05_p95_s": _r(_pctl(d, 0.95) - _pctl(d, 0.05)),
                          "spread_min_max_s": _r(d[-1] - d[0]),
                          "p95_abs_dev_s": _r(_pctl(sorted(abs(x - _pctl(d, 0.5)) for x in d),
                                                    0.95))}

    def _jump_between(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        lo, hi = sorted((a["t_mono_ns"], b["t_mono_ns"]))
        return any(lo <= j["t_mono_ns"] <= hi for j in self.jumps)

    def interval(self, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        """b - a in the best clock domain the evidence supports."""
        ra, rb = a.get("role"), b.get("role")
        if ra in self.co_hosted and rb in self.co_hosted:
            if a.get("dds_src_ns") and b.get("dds_src_ns"):
                return {"value_s": _r((b["dds_src_ns"] - a["dds_src_ns"]) / _NS),
                        "basis": "emission",
                        "clock_domain": "publisher host wall clock (DDS source timestamps); "
                                        "both roles declared co-hosted in the profile"}
        da, db = a.get("pub_stamp_domain"), b.get("pub_stamp_domain")
        if da and da == db and a.get("pub_stamp_s") and b.get("pub_stamp_s"):
            return {"value_s": _r(b["pub_stamp_s"] - a["pub_stamp_s"]),
                    "basis": "emission",
                    "clock_domain": f"embedded publisher stamps ({da})"}
        if a.get("dds_rx_ns") and b.get("dds_rx_ns") and not self._jump_between(a, b):
            return {"value_s": _r((b["dds_rx_ns"] - a["dds_rx_ns"]) / _NS),
                    "basis": "receipt",
                    "clock_domain": "recorder host wall clock (DDS reception timestamps)",
                    "caveat": "observation interval: includes transport on both legs; "
                              "not a causal emission latency"}
        return {"value_s": _r((b["t_mono_ns"] - a["t_mono_ns"]) / _NS),
                "basis": "receipt",
                "clock_domain": "recorder monotonic clock (callback time)",
                "uncertainty_s": (self.delay or {}).get("spread_min_max_s"),
                "caveat": "observation interval: includes transport and executor "
                          "queueing on both legs; not a causal emission latency. "
                          "uncertainty_s bounds the error: the min-max spread of the "
                          "recorder's own receipt delay in this bundle (null when it "
                          "could not be measured)"}

    def not_before(self, cand: dict[str, Any], ref: dict[str, Any]) -> bool:
        iv = self.interval(ref, cand)
        v = iv["value_s"]
        if v is None:
            return cand["seq"] >= ref["seq"]
        tol = 0.0 if iv["basis"] == "emission" else RECEIPT_ORDER_TOL_NS / _NS
        return v >= -tol


def _evidence(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "topic": r.get("topic"), "seq": r.get("seq"),
        "t_mono_ns": r.get("t_mono_ns"), "t_wall_ns": r.get("t_wall_ns"),
        "t_ros_ns": r.get("t_ros_ns"),
        "dds_src_ns": r.get("dds_src_ns"), "dds_rx_ns": r.get("dds_rx_ns"),
        "pub_stamp_s": r.get("pub_stamp_s"), "pub_stamp_domain": r.get("pub_stamp_domain"),
    }


# ---------------------------------------------------------------------------
# topic statistics
# ---------------------------------------------------------------------------


def _topic_stats(topic: str, recs: list[dict[str, Any]], status: dict[str, Any],
                 spec: dict[str, Any] | None, t0: int, t_end: int,
                 pre_s: float) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": (spec or {}).get("role") or (recs[0]["role"] if recs else None),
        "availability": status.get("status", "unknown"),
        "graph_types": status.get("graph_types"),
        "publishers": status.get("publishers"),
        "count": len(recs),
        "stored": sum(1 for r in recs if r.get("data") is not None),
    }
    if status.get("reason"):
        out["availability_reason"] = status["reason"]
    if out["availability"] == "subscribed" and not recs:
        out["availability"] = "subscribed_no_messages"
    if not recs:
        return out
    ts = [r["t_mono_ns"] for r in recs]
    phases = {"before": (t0 - int(pre_s * _NS), t0), "during": (t0, t0 + int(DURING_SEC * _NS)),
              "after": (t0 + int(DURING_SEC * _NS), t_end)}
    rates = {}
    for name, (lo, hi) in phases.items():
        n = sum(1 for t in ts if lo <= t < hi)
        span = (hi - lo) / _NS
        rates[name] = _r(n / span, 3) if span > 0 else None
    out["rate_hz"] = rates
    dts = [(b - a) / _NS for a, b in zip(ts, ts[1:])]
    if dts:
        med = statistics.median(dts)
        stale = (spec or {}).get("stale_after_sec")
        thr = stale if stale else max(5 * med, 0.1)
        gaps = [(ts[i], d) for i, d in enumerate(dts) if d > thr]
        out["median_interval_s"] = _r(med)
        out["max_gap_s"] = _r(max(dts))
        out["gap_threshold_s"] = _r(thr)
        out["gaps"] = [{"after_seq": recs[i]["seq"], "gap_s": _r(d)} for i, (_, d) in
                       enumerate(gaps)][:50]
        out["gap_count"] = len(gaps)
    # duplicates and ordering
    dup = ooo = 0
    seen: set[tuple[Any, ...]] = set()
    last_stamp: float | None = None
    for r in recs:
        if r.get("dds_src_ns") and r.get("data") is not None:
            key = (r["dds_src_ns"], repr(r["data"]))
            if key in seen:
                dup += 1
            seen.add(key)
        s = r.get("pub_stamp_s")
        if s is not None:
            if last_stamp is not None and s < last_stamp:
                ooo += 1
            last_stamp = s if last_stamp is None else max(last_stamp, s)
    out["duplicates"] = dup
    out["out_of_order_stamps"] = ooo
    role = out["role"]
    if role in ("helix_hold", "arbiter_status"):
        out["sequence"] = _seq_loss(recs, with_epoch=(role == "helix_hold"))
    return out


def _seq_loss(recs: list[dict[str, Any]], *, with_epoch: bool) -> dict[str, Any]:
    lost = dup = back = restarts = 0
    prev: tuple[int, int] | None = None
    for r in recs:
        d = r.get("data") or {}
        seq = d.get("seq")
        if not isinstance(seq, int):
            continue
        ep = d.get("epoch") if with_epoch else 0
        if prev is not None:
            pe, ps = prev
            if ep != pe:
                restarts += 1
            elif seq == ps:
                dup += 1
            elif seq < ps:
                if with_epoch or ps - seq < 1000:
                    back += 1
                else:
                    restarts += 1
            else:
                lost += seq - ps - 1
        prev = (ep, seq) if prev is None or ep != prev[0] or seq >= prev[1] else prev
    return {"lost": lost, "duplicates": dup, "reordered": back, "publisher_restarts": restarts,
            "method": "gaps in the publisher's own sequence counter"}


# ---------------------------------------------------------------------------
# odometry
# ---------------------------------------------------------------------------


def _odom_samples(recs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    out = []
    for r in recs:
        d = r.get("data")
        if not d:
            continue
        x, y = _num(_get(d, "pose.pose.position.x")), _num(_get(d, "pose.pose.position.y"))
        vx, vy = _num(_get(d, "twist.twist.linear.x")), _num(_get(d, "twist.twist.linear.y"))
        if None in (x, y, vx, vy):
            continue
        out.append({"rec": r, "t": r["t_mono_ns"] / _NS, "hdr": r.get("pub_stamp_s"),
                    "rx_wall": (r.get("dds_rx_ns") or r["t_wall_ns"]) / _NS,
                    "x": x, "y": y, "vx": vx, "vy": vy})
    hdrs = [s["hdr"] for s in out]
    use_hdr = bool(out) and all(h is not None for h in hdrs) and all(
        b > a for a, b in zip(hdrs, hdrs[1:]))
    base = "robot header stamps" if use_hdr else "recorder receipt times"
    for s in out:
        s["tb"] = s["hdr"] if use_hdr else s["t"]
    return out, base


def _speeds(samples: list[dict[str, Any]], baseline: float) -> None:
    """HELIX hw_stage speed: max(pose-differenced over >= baseline, twist)."""
    j = 0
    for i, o in enumerate(samples):
        while j < i and o["tb"] - samples[j]["tb"] > baseline:
            j += 1
        k = max(0, j - 1)
        dt = o["tb"] - samples[k]["tb"]
        vp = math.hypot(o["x"] - samples[k]["x"], o["y"] - samples[k]["y"]) / dt if dt > 0 else 0.0
        o["speed_pose"] = vp
        o["speed_twist"] = math.hypot(o["vx"], o["vy"])
        o["speed"] = max(vp, o["speed_twist"])


def _stop_analysis(odom: list[dict[str, Any]], anchor: dict[str, Any] | None,
                   fault: dict[str, Any] | None, crit: dict[str, Any],
                   odom_status: str, release: dict[str, Any] | None = None,
                   payload_delay: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"synthetic_warning": None}
    thr = crit.get("stopped_speed_mps", 0.03)
    deadline = crit.get("stop_deadline_sec", 1.5)
    moving = crit.get("moving_speed_mps", 0.05)
    if odom_status not in ("subscribed",):
        out["status"] = "insufficient"
        out["reason"] = f"odometry topic {odom_status}"
        return out
    samples, base = _odom_samples(odom)
    if len(samples) < 2:
        out["status"] = "insufficient"
        out["reason"] = f"{len(samples)} usable odometry samples"
        return out
    _speeds(samples, crit.get("pose_speed_baseline_sec", 0.05))
    out["speed_time_base"] = base
    out["samples"] = len(samples)
    # receipt jitter: spread of (receipt - robot stamp) around its median
    offs = [s["rx_wall"] - s["hdr"] for s in samples if s["hdr"] is not None]
    if len(offs) >= 5:
        med = statistics.median(offs)
        dev = [abs(o - med) for o in offs]
        out["robot_clock_offset_s"] = _r(med)
        out["robot_clock_offset_basis"] = ("median(recorder receipt wall time - odometry "
                                           "header stamp); includes transport delay")
        out["receipt_jitter_p95_s"] = _r(_pctl(dev, 0.95))
    period = statistics.median([b["t"] - a["t"] for a, b in zip(samples, samples[1:])])
    out["median_period_s"] = _r(period)

    def speed_near(rec: dict[str, Any] | None) -> tuple[float | None, float | None]:
        if rec is None:
            return None, None
        t = rec["t_mono_ns"] / _NS
        prior = [s for s in samples if s["t"] <= t]
        if not prior:
            return None, None
        s = prior[-1]
        return s["speed"], t - s["t"]

    sp, age = speed_near(fault)
    out["speed_at_fault_mps"] = _r(sp)
    out["odom_age_at_fault_s"] = _r(age)
    if anchor is None:
        out["status"] = "not_applicable"
        out["reason"] = "no HELIX hold observed to time a stop from"
        return out
    sp, age = speed_near(anchor)
    out["speed_at_hold_mps"] = _r(sp)
    out["odom_age_at_hold_s"] = _r(age)
    t_h = anchor["t_mono_ns"] / _NS
    # Only the held period counts: after the hold is released, motion is
    # legitimate and must not be read as a failed stop.
    t_rel = release["t_mono_ns"] / _NS if release is not None else None
    after = [s for s in samples if s["t"] >= t_h and (t_rel is None or s["t"] < t_rel)]
    if t_rel is not None:
        out["judged_until"] = "hold release"
    if not after:
        out["status"] = "insufficient"
        out["reason"] = "no odometry received after the hold"
        return out
    first_gap = after[0]["t"] - t_h
    gaps = [b["t"] - a["t"] for a, b in zip(after, after[1:])]
    gap_limit = max(0.25, 5 * period)
    horizon = [g for a, g in zip(after, gaps) if a["t"] - t_h <= deadline + 0.5]
    big = [g for g in horizon if g > gap_limit]
    if first_gap > gap_limit or big:
        out["status"] = "insufficient"
        out["reason"] = (f"odometry gap of {max([first_gap, *big]):.3f} s within "
                         f"{deadline} s of the hold (limit {gap_limit:.3f} s)")
        return out
    if age is not None and age > gap_limit:
        out["status"] = "insufficient"
        out["reason"] = f"odometry was {age:.3f} s old at the hold"
        return out
    stopped = None
    for i, s in enumerate(after):
        if all(p["speed"] < thr for p in after[i:]):
            stopped = s
            break
    coverage_after_hold = after[-1]["t"] - t_h
    out["odom_coverage_after_hold_s"] = _r(coverage_after_hold)
    if stopped is None:
        if coverage_after_hold >= deadline:
            out["status"] = "not_stopped"
            out["reason"] = (f"speed stayed >= {thr} m/s in the last odometry sample, "
                             f"{coverage_after_hold:.3f} s after the hold")
        else:
            out["status"] = "insufficient"
            out["reason"] = "bundle ends before the robot is seen stopped or the deadline passes"
        out["final_speed_mps"] = _r(after[-1]["speed"])
        return out
    confirm = after[-1]["t"] - stopped["t"]
    if confirm < 0.5:
        out["status"] = "insufficient"
        out["reason"] = f"only {confirm:.3f} s of odometry after the stopped sample (need 0.5 s)"
        return out
    out["status"] = "stopped"
    out["already_stopped_at_hold"] = bool(sp is not None and sp < thr)
    out["stopped_sample"] = _evidence(stopped["rec"])
    jitter = out.get("receipt_jitter_p95_s") or 0.0  # odometry receipt jitter
    # (a) Receipt interval on the recorder monotonic clock. A recorder stall
    # delays receipt of whatever is queued behind it, the hold included, so
    # the bound adds the largest odometry receipt stall up to the stop and the
    # hold's own excess receipt delay.
    lat = stopped["t"] - t_h
    near = [s for s in samples if t_h - 0.2 <= s["t"] <= stopped["t"]]
    stall = max((b["t"] - a["t"] for a, b in zip(near, near[1:])), default=0.0)
    stall = max(0.0, stall - period)
    hold_excess = 0.0
    if payload_delay and anchor.get("dds_src_ns"):
        hold_excess = max(0.0, (anchor["t_wall_ns"] - anchor["dds_src_ns"]) / _NS
                          - payload_delay["p50_s"])
    out["stop_latency_receipt_s"] = _r(lat)
    out["stop_latency_receipt_uncertainty_s"] = _r(period + jitter + stall + hold_excess)
    out["odom_receipt_stall_s"] = _r(stall)
    out["hold_receipt_excess_delay_s"] = _r(hold_excess)
    # (b) Emission times mapped onto the recorder wall clock with median
    # offsets: hold DDS source time (payload clock) + median payload receipt
    # delay, stopped sample robot stamp + median robot receipt offset. Medians
    # are not moved by stalls. Primary when both offsets are measurable.
    off_r = out.get("robot_clock_offset_s")
    hold_src = anchor.get("dds_src_ns")
    mapped = None
    if off_r is not None and stopped["hdr"] is not None and hold_src and payload_delay:
        mapped = (stopped["hdr"] + off_r) - (hold_src / _NS + payload_delay["p50_s"])
        unc = period + jitter + (payload_delay.get("p95_abs_dev_s") or 0.0)
        out["stop_latency_mapped_s"] = _r(mapped)
        out["stop_latency_mapped_uncertainty_s"] = _r(unc)
    if mapped is not None:
        out["stop_latency_s"] = _r(mapped)
        out["stop_latency_uncertainty_s"] = out["stop_latency_mapped_uncertainty_s"]
        out["stop_latency_clock_domain"] = (
            "emission times on one clock: hold DDS source time and the stopped sample's robot "
            "stamp, each mapped to the recorder wall clock by its median receipt offset "
            "(robust to recorder stalls; assumes each offset is constant over the window and "
            "the two median transport delays are similar)")
        out["stop_latency_uncertainty_basis"] = (
            "one odometry period + p95 deviation of the robot offset + p95 deviation of the "
            "payload receipt delay")
    else:
        out["stop_latency_s"] = _r(lat)
        out["stop_latency_uncertainty_s"] = out["stop_latency_receipt_uncertainty_s"]
        out["stop_latency_clock_domain"] = (
            "recorder monotonic clock: receipt of the hold -> receipt of the first odometry "
            "sample after which speed stays below threshold")
        out["stop_latency_uncertainty_basis"] = (
            "one odometry period + p95 odometry receipt jitter + largest odometry receipt "
            "stall up to the stop + the hold's excess receipt delay")
    if stopped["hdr"] is not None and after[0]["hdr"] is not None:
        out["robot_clock_stop_duration_s"] = _r(stopped["hdr"] - after[0]["hdr"])
    # stop distance: pose path must be continuous (no step implying > 1 m/s)
    seg = [s for s in after if s["t"] <= stopped["t"]]
    steps = [(math.hypot(b["x"] - a["x"], b["y"] - a["y"]), b["tb"] - a["tb"])
             for a, b in zip(seg, seg[1:])]
    jump = [d for d, dt in steps if dt > 0 and d / dt > 1.0]
    if jump:
        out["stop_distance_m"] = None
        out["stop_distance_reason"] = f"odometry pose jumped {max(jump):.3f} m between samples"
    else:
        out["stop_distance_m"] = _r(math.hypot(stopped["x"] - after[0]["x"],
                                               stopped["y"] - after[0]["y"]))
        out["stop_distance_basis"] = ("straight-line odometry pose displacement from the first "
                                      "sample after the hold to the stopped sample")
    out["moving_threshold_mps"] = moving
    return out


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


def _newer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """HELIX (epoch, seq) ordering: is hold message ``a`` newer than ``b``?

    Falls back to recorder order when the publisher's counters are missing.
    """
    ea, sa = a["data"].get("epoch"), a["data"].get("seq")
    eb, sb = b["data"].get("epoch"), b["data"].get("seq")
    if isinstance(sa, int) and isinstance(sb, int) and ea == eb:
        return sa > sb
    return a["seq"] > b["seq"]


def _release_after(holds: list[dict[str, Any]], edge: dict[str, Any]) -> dict[str, Any] | None:
    """First hold=false that is newer than the hold edge by the publisher's own
    (epoch, seq). A stale false delivered late is not a release (HELIX's
    arbiter drops those too)."""
    for r in holds:
        if r["seq"] > edge["seq"] and not r["data"].get("hold") and _newer(r, edge):
            return r
    return None


def _by_role(msgs: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [r for r in msgs if r.get("role") == role and r.get("data") is not None]


def _role_availability(role: str, topic_stats: dict[str, dict[str, Any]]) -> str:
    st = [s["availability"] for s in topic_stats.values() if s.get("role") == role]
    if not st:
        return "not_in_profile"
    for want in ("subscribed", "subscribed_no_messages"):
        if want in st:
            return want
    return st[0]


def _chain(msgs: list[dict[str, Any]], t0: int, clocks: _Clocks,
           topic_stats: dict[str, dict[str, Any]],
           horizon_s: float = 1.5) -> tuple[dict[str, Any], dict[str, Any]]:
    holds = _by_role(msgs, "helix_hold")
    edges = []
    prev = None
    for r in holds:
        h = bool(r["data"].get("hold"))
        if h and prev is not True:
            edges.append((r, prev is False))
        prev = h
    stops = [r for r in _by_role(msgs, "recovery_action")
             if r["data"].get("action") == "STOP_AND_HOLD" and r["data"].get("status") == "ACCEPTED"]
    found: dict[str, dict[str, Any] | None] = {k: None for k in CHAIN}
    notes: dict[str, Any] = {}
    fault_id = ""
    if edges:
        h, observed_edge = min(edges, key=lambda e: abs(e[0]["t_mono_ns"] - t0))
        found["helix_hold"] = h
        notes["helix_hold"] = {"observed_edge": observed_edge,
                               "asserted_stamp": h["data"].get("asserted_stamp"),
                               "reason": h["data"].get("reason")}
        fault_id = h["data"].get("fault_id", "")
    act_c = [r for r in stops if not fault_id or r["data"].get("fault_id") == fault_id]
    if found["helix_hold"] is not None:
        act_c = [r for r in act_c if clocks.not_before(found["helix_hold"], r)]
        if act_c:
            found["recovery_action"] = act_c[-1]
    elif act_c:
        found["recovery_action"] = min(act_c, key=lambda r: abs(r["t_mono_ns"] - t0))
        fault_id = found["recovery_action"]["data"].get("fault_id", "")
    anchor = found["recovery_action"] or found["helix_hold"]
    if anchor is None:
        return ({k: {"status": "not_observed"} for k in CHAIN},
                {"fault_id": None, "note": "no HELIX STOP_AND_HOLD or hold assertion in bundle"})
    hints = [r for r in _by_role(msgs, "recovery_hint")
             if r["data"].get("suggested_action") == "STOP_AND_HOLD"
             and (not fault_id or r["data"].get("fault_id") == fault_id)
             and clocks.not_before(anchor, r)]
    if hints:
        found["recovery_hint"] = found["diagnosis"] = hints[-1]
    ref = found["recovery_hint"] or anchor
    faults = [r for r in _by_role(msgs, "helix_fault")
              if (not fault_id or r["data"].get("node_name") == fault_id)
              and clocks.not_before(ref, r)]
    if faults:
        found["fault"] = faults[-1]
    ref = found["helix_hold"] or anchor

    # Downstream stages must follow the hold within the stop deadline and before
    # the hold is released; later matches belong to another event.
    horizon_ns = int(float(horizon_s) * _NS)
    release = None
    if found["helix_hold"] is not None:
        release = _release_after(holds, found["helix_hold"])
    info_release = release

    def first_after(role: str, ref: dict[str, Any], pred) -> dict[str, Any] | None:
        lim = (found["helix_hold"] or anchor)["t_mono_ns"] + horizon_ns
        for r in _by_role(msgs, role):
            if r["t_mono_ns"] > lim or (release is not None and r["seq"] > release["seq"]):
                break
            if clocks.not_before(r, ref) and pred(r["data"]):
                return r
        return None

    found["arbiter_forced_zero"] = first_after(
        "arbiter_status", ref,
        lambda d: bool(d.get("hold_active")) and all(
            _num(d.get(k)) == 0.0 for k in ("out_linear_x", "out_linear_y", "out_angular_z")))
    ref2 = found["arbiter_forced_zero"] or ref
    found["cmd_vel_zero"] = first_after("cmd_vel_out", ref2, lambda d: _twist_zero(d) is True)
    if found["cmd_vel_zero"] is None and \
            _role_availability("cmd_vel_out", topic_stats) != "subscribed":
        # Recorder not on /cmd_vel (HELIX runs: see profiles/go2_helix.yaml).
        # The sink's trace carries the command it received on /cmd_vel.
        def _zero_input(d: dict[str, Any]) -> bool:
            inp = d.get("input")
            return isinstance(inp, list) and len(inp) == 3 and all(_num(x) == 0.0 for x in inp)
        found["cmd_vel_zero"] = first_after("sink_trace", ref2, _zero_input)
        if found["cmd_vel_zero"] is not None:
            notes["cmd_vel_zero"] = {
                "source": "sink trace `input`: the zero command the GO2 sport sink received "
                          "on /cmd_vel (the recorder does not subscribe to /cmd_vel here)"}
    # The sink publishes the request, then writes its trace line, so the
    # request can precede the sink-trace record of the same decision. Order
    # both against the arbiter zero and pair them by request id.
    sink = first_after("sink_trace", ref2, lambda d: d.get("api_id") == STOP_API_ID)
    if sink is not None and sink["data"].get("request_id") is not None:
        rid = sink["data"]["request_id"]
        found["sport_stopmove_request"] = first_after(
            "sport_request", ref2, lambda d: _get(d, "header.identity.id") == rid)
    if found["sport_stopmove_request"] is None:
        found["sport_stopmove_request"] = first_after(
            "sport_request", ref2, lambda d: _get(d, "header.identity.api_id") == STOP_API_ID)
    if sink is not None:
        notes["sink_decision"] = {
            "api_id": sink["data"].get("api_id"), "reason": sink["data"].get("reason"),
            "request_id": sink["data"].get("request_id"),
            "sent_to_robot": sink["data"].get("sent_to_robot"),
            "mode": sink["data"].get("mode"), "evidence": _evidence(sink)}
    req_id = None
    if found["sport_stopmove_request"] is not None:
        req_id = _get(found["sport_stopmove_request"]["data"], "header.identity.id")
    elif sink is not None and sink["data"].get("sent_to_robot"):
        req_id = sink["data"].get("request_id")
    if req_id is not None:
        notes["stopmove_request_id"] = req_id
        for r in _by_role(msgs, "sport_response"):
            if _get(r["data"], "header.identity.id") == req_id:
                found["sport_response"] = r
                notes["sport_response"] = {
                    "code": _get(r["data"], "header.status.code"),
                    "api_id": _get(r["data"], "header.identity.api_id")}
                break
    stages: dict[str, Any] = {}
    for k in CHAIN:
        if k == "odometry_stopped":
            continue
        r = found[k]
        if r is not None:
            stages[k] = {"status": "observed", "evidence": _evidence(r),
                         "data": r.get("data")}
            if k == "diagnosis":
                stages[k]["note"] = ("HELIX diagnosis output is the RecoveryHint; "
                                     "same record as recovery_hint")
        else:
            avail = _role_availability(_STAGE_ROLE[k], topic_stats)
            stages[k] = {"status": "not_observed" if avail == "subscribed" else
                         f"topic_{avail}"}
        if k in notes:
            stages[k]["detail"] = notes[k]
    info = {"fault_id": fault_id, "anchor": "helix_hold" if found["helix_hold"] else
            "recovery_action", "found": found, "notes": notes, "release": info_release,
            "horizon_s": horizon_s}
    return stages, info


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _resources(sys_recs: list[dict[str, Any]], t0: int, pre_s: float) -> dict[str, Any]:
    if not sys_recs:
        return {"available": False, "reason": "no system samples in bundle"}
    phases = {"before": lambda t: t < t0, "during": lambda t: t0 <= t < t0 + DURING_SEC * _NS,
              "after": lambda t: t >= t0 + DURING_SEC * _NS}

    def series(path: str) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for name, f in phases.items():
            vals = [_num(_get(r, path)) for r in sys_recs if f(r["t_mono_ns"])]
            vals = [v for v in vals if v is not None]
            res[name] = ({"min": _r(min(vals), 2), "mean": _r(sum(vals) / len(vals), 2),
                          "max": _r(max(vals), 2), "n": len(vals)} if vals else None)
        return res

    out: dict[str, Any] = {"available": True, "samples": len(sys_recs),
                           "cpu_percent": series("cpu_percent"),
                           "mem_percent": series("mem_percent"),
                           "recorder_cpu_percent": series("recorder.cpu_percent"),
                           "recorder_rss_mb": series("recorder.rss_mb")}
    gpu = [r.get("gpu") for r in sys_recs]
    if any(g for g in gpu):
        out["gpu_load_percent"] = series("gpu.load_percent")
        out["gpu_temp_c"] = series("gpu.temp_c")
        out["gpu_backend"] = next(g.get("backend") for g in gpu if g)
    else:
        reasons = sorted({str(r.get("gpu_unavailable_reason")) for r in sys_recs
                          if r.get("gpu_unavailable_reason")})
        out["gpu"] = {"available": False, "reason": reasons or ["not reported"]}
    zones: dict[str, list[float]] = {}
    for r in sys_recs:
        for z, v in (r.get("thermal_c") or {}).items():
            if _num(v) is not None:
                zones.setdefault(z, []).append(float(v))
    out["thermal_max_c"] = {z: _r(max(v), 1) for z, v in sorted(zones.items())}
    return out


def analyze(manifest: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    recs = sorted(records, key=lambda r: r.get("seq", 0))
    prof = manifest.get("profile") or {}
    crit = prof.get("stop_criteria") or {}
    pre_s = float(prof.get("pre_trigger_sec") or 10.0)
    triggers = manifest.get("triggers") or [r for r in recs if r.get("kind") == "trigger"]
    msgs = [r for r in recs if r.get("kind") == "msg"]
    primary = triggers[0] if triggers else None
    t_end = recs[-1]["t_mono_ns"] if recs else 0
    t0 = primary["t_mono_ns"] if primary else (recs[0]["t_mono_ns"] if recs else 0)
    clocks = _Clocks(recs, set(prof.get("co_hosted_roles") or ()))

    specs = {t["name"]: t for t in prof.get("topics") or []}
    tstatus = manifest.get("topic_status") or {}
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in msgs:
        by_topic.setdefault(r["topic"], []).append(r)
    topics = {}
    for name in sorted(set(specs) | set(by_topic)):
        topics[name] = _topic_stats(name, by_topic.get(name, []), tstatus.get(name, {}),
                                    specs.get(name), t0, t_end, pre_s)

    stages, info = _chain(msgs, t0, clocks, topics,
                          horizon_s=float(crit.get("stop_deadline_sec", 1.5)))
    found = info.get("found") or {}
    odom_topics = [n for n, s in topics.items() if s.get("role") == "odometry"]
    odom_recs = by_topic.get(odom_topics[0], []) if odom_topics else []
    odom_status = topics[odom_topics[0]]["availability"] if odom_topics else "not_in_profile"
    stop = _stop_analysis(odom_recs, found.get("helix_hold"), found.get("fault"), crit,
                          odom_status, release=info.get("release"), payload_delay=clocks.delay)
    if stop.get("status") == "stopped":
        stages["odometry_stopped"] = {"status": "observed", "evidence": stop["stopped_sample"]}
    else:
        stages["odometry_stopped"] = {"status": "not_observed" if odom_status == "subscribed"
                                      else f"topic_{odom_status}",
                                      "reason": stop.get("reason")}

    latencies = []
    observed = [k for k in CHAIN if stages[k]["status"] == "observed" and k != "diagnosis"]
    for a, b in zip(observed, observed[1:]):
        if b == "odometry_stopped":
            continue
        iv = clocks.interval(found[a], found[b])
        latencies.append({"from": a, "to": b, **iv,
                          **({"flag": "negative interval: stamps do not support this order"}
                             if (iv["value_s"] or 0) < 0 else {})})
    spans = []
    for a, b in (("fault", "helix_hold"), ("helix_hold", "cmd_vel_zero"),
                 ("helix_hold", "sport_stopmove_request"),
                 ("sport_stopmove_request", "sport_response"), ("fault", "cmd_vel_zero")):
        if found.get(a) is not None and found.get(b) is not None:
            spans.append({"from": a, "to": b, **clocks.interval(found[a], found[b])})
    if stop.get("status") == "stopped":
        mapped = "stop_latency_mapped_s" in stop
        spans.append({"from": "helix_hold", "to": "odometry_stopped",
                      "value_s": stop["stop_latency_s"],
                      "basis": "emission (offset-mapped)" if mapped else "receipt",
                      "clock_domain": stop["stop_latency_clock_domain"],
                      "uncertainty_s": stop["stop_latency_uncertainty_s"],
                      "receipt_value_s": stop.get("stop_latency_receipt_s"),
                      "receipt_uncertainty_s": stop.get("stop_latency_receipt_uncertainty_s"),
                      "caveat": "hold and odometry are published on different computers "
                                "(payload vs GO2) with different clocks"})

    # motion summary
    held_from = found.get("helix_hold")
    src_before = [r for r in _by_role(msgs, "cmd_vel_source")
                  if held_from is None or r["seq"] <= held_from["seq"]]
    outs = _by_role(msgs, "cmd_vel_out")
    outputs_source = "/cmd_vel"
    if not outs:
        # ArbiterStatus carries the command the arbiter published on /cmd_vel.
        outs = [{**r, "data": {"linear": {"x": r["data"].get("out_linear_x"),
                                          "y": r["data"].get("out_linear_y"), "z": 0.0},
                               "angular": {"x": 0.0, "y": 0.0,
                                           "z": r["data"].get("out_angular_z")}}}
                for r in _by_role(msgs, "arbiter_status")]
        outputs_source = ("ArbiterStatus out_* fields (what the arbiter published on "
                          "/cmd_vel)" if outs else None)
    held_nonzero = moves_held = None
    release = None
    if held_from is not None:
        release = _release_after(_by_role(msgs, "helix_hold"), held_from)
        lo, hi = held_from["seq"], (release["seq"] if release else float("inf"))
        # an output already in flight when the hold lands is not "while held":
        # start counting at the arbiter's first forced zero if it was observed
        start = found.get("arbiter_forced_zero") or held_from
        held_nonzero = sum(1 for r in outs if start["seq"] < r["seq"] < hi
                           and _twist_zero(r["data"]) is False)
        reqs = _by_role(msgs, "sport_request")
        if _role_availability("sport_request", topics) == "subscribed":
            moves_held = sum(1 for r in reqs if lo < r["seq"] < hi
                             and _get(r["data"], "header.identity.api_id") == MOVE_API_ID)
    motion = {
        "input_twist": ({"topic": src_before[-1]["topic"], **_twist(src_before[-1]["data"])}
                        if src_before else None),
        "final_twist": _twist(outs[-1]["data"]) if outs else None,
        "outputs_source": outputs_source,
        "nonzero_outputs_while_held": held_nonzero,
        "move_requests_while_held": moves_held,
        "hold_released": None if held_from is None else (
            {"evidence": _evidence(release), "reason": release["data"].get("reason")}
            if release else False),
        "stopmove_request_id": info.get("notes", {}).get("stopmove_request_id"),
        "sport_response": info.get("notes", {}).get("sport_response"),
        "odometry": {k: v for k, v in stop.items() if k != "synthetic_warning" and v is not None},
        "chain_horizon_s": info.get("horizon_s"),
    }

    # data quality and clock warnings
    sys_recs = [r for r in recs if r.get("kind") == "sys"]
    warnings: list[str] = []
    jumps = [r for r in recs if r.get("kind") == "clock_jump"]
    if jumps:
        warnings.append(f"{len(jumps)} recorder wall-clock jump(s) in window; wall-clock "
                        "(DDS reception) intervals across a jump were replaced by monotonic ones")
    wall0 = recs[0]["t_wall_ns"] / _NS if recs else 0
    if recs and wall0 < 1_767_225_600:  # 2026-01-01
        warnings.append("recorder wall clock is before 2026-01-01; wall timestamps are not "
                        "trustworthy (payload without RTC?)")
    off = stop.get("robot_clock_offset_s")
    if off is not None and abs(off) > 0.5:
        warnings.append(f"robot clock differs from recorder receipt by {off:.3f} s (median); "
                        "robot stamps are never compared with payload or recorder clocks")
    skews = [(r["dds_rx_ns"] - r["dds_src_ns"]) / _NS for r in msgs
             if r.get("dds_rx_ns") and r.get("dds_src_ns") and r.get("role") in clocks.co_hosted]
    if skews:
        med = statistics.median(skews)
        if med < 0 or med > 0.5:
            warnings.append(f"median (DDS reception - DDS source) on co-hosted topics is "
                            f"{med:.3f} s: recorder and payload clocks disagree; emission "
                            "intervals remain valid (one clock), receipt vs emission never mixed")
    if (manifest.get("session") or {}).get("use_sim_time"):
        warnings.append("recorder ran with use_sim_time; t_ros_ns is simulation time")
    if not any(r.get("dds_src_ns") for r in msgs) and msgs:
        warnings.append("no DDS source timestamps captured (RMW did not provide them); "
                        "emission intervals rely on embedded stamps only")
    synthetic = bool((manifest.get("session") or {}).get("synthetic"))
    stats = manifest.get("recorder_stats") or {}
    writer = manifest.get("writer") or {}
    gaps = {n: s.get("gap_count", 0) for n, s in topics.items() if s.get("gap_count")}
    lost = {n: s["sequence"]["lost"] for n, s in topics.items()
            if s.get("sequence", {}).get("lost")}
    quality = {
        "synthetic": synthetic,
        "bundle_status": manifest.get("status"),
        "pre_window": manifest.get("pre_window"),
        "recorder_ring_evicted_in_window": stats.get("ring_evicted_by_cap_in_window", 0),
        "recorder_message_lost": sum(int((st or {}).get("message_lost") or 0)
                                     for st in tstatus.values()),
        "writer_errors": writer.get("write_errors", 0),
        "first_write_error": writer.get("first_write_error"),
        "topics_with_gaps": gaps,
        "publisher_sequence_lost": lost,
        "duplicates": {n: s["duplicates"] for n, s in topics.items() if s.get("duplicates")},
        "out_of_order_stamps": {n: s["out_of_order_stamps"] for n, s in topics.items()
                                if s.get("out_of_order_stamps")},
        "clock_jumps": [{"seq": r["seq"], "wall_step_s": _r(r["wall_step_ns"] / _NS)}
                        for r in jumps],
        "clock_warnings": warnings,
    }

    # nodes
    graphs = [r for r in recs if r.get("kind") == "graph"]
    nodes_gone = sorted({n for g in graphs for n in g.get("nodes_gone") or []})
    nodes_new = sorted({n for g in graphs for n in g.get("nodes_new") or []})
    pubs = sorted({p for s in topics.values() for p in (s.get("publishers") or [])})

    report = {
        "schema": REPORT_SCHEMA,
        "bundle_id": manifest.get("bundle_id"),
        "status": manifest.get("status"),
        "synthetic": synthetic,
        "provenance": manifest.get("session"),
        "profile": {"name": prof.get("name"), "sha256": prof.get("sha256"),
                    "pre_trigger_sec": prof.get("pre_trigger_sec"),
                    "post_trigger_sec": prof.get("post_trigger_sec")},
        "trigger": primary,
        "secondary_triggers": triggers[1:],
        "window": {"records": len(recs), "messages": len(msgs),
                   "first_t_mono_ns": recs[0]["t_mono_ns"] if recs else None,
                   "last_t_mono_ns": t_end if recs else None,
                   "seconds_before_trigger": _r((t0 - recs[0]["t_mono_ns"]) / _NS, 3)
                   if recs else None,
                   "seconds_after_trigger": _r((t_end - t0) / _NS, 3) if recs else None},
        "nodes": {"publishers_on_profiled_topics": pubs, "disappeared": nodes_gone,
                  "appeared": nodes_new},
        "topics": topics,
        "helix": {
            "fault_id": info.get("fault_id"),
            "fault": _get(stages, "fault.data"),
            "diagnosis": _get(stages, "recovery_hint.data"),
            "recovery_hint": _get(stages, "recovery_hint.data"),
            "recovery_action": _get(stages, "recovery_action.data"),
            "hold": _get(stages, "helix_hold.data"),
            "arbiter": _get(stages, "arbiter_forced_zero.data"),
            "note": info.get("note"),
        },
        "chain_order": list(CHAIN),
        "chain": stages,
        "stage_intervals": latencies,
        "key_spans": spans,
        "motion": motion,
        "resources": _resources(sys_recs, t0, pre_s),
        "data_quality": quality,
        "recorder_receipt_delay": clocks.delay or {
            "note": "not measurable: fewer than 20 co-hosted messages with DDS source stamps"},
        "timing_methodology": {
            "emission": "DDS source timestamps, or embedded publisher stamps, compared only "
                        "within one declared clock domain",
            "receipt": "recorder DDS reception time (wall) or recorder monotonic callback "
                       "time; used only when emission times are not comparable; labelled",
            "never": "receipt times are never presented as emission times; robot clock is "
                     "never compared with the payload or recorder clock",
        },
    }
    report["verdicts"] = _verdicts(report, stages, stop, crit)
    return report


def _verdicts(rep: dict[str, Any], stages: dict[str, Any], stop: dict[str, Any],
              crit: dict[str, Any]) -> list[dict[str, Any]]:
    """PASS/FAIL/INCOMPLETE only for criteria with an explicit source."""
    src = "HELIX docs/HW_MOTION_TEST.md stage E pass criteria"
    if (rep["chain"].get("helix_hold", {}).get("status") != "observed"
            and rep["chain"].get("recovery_action", {}).get("status") != "observed"):
        return []
    out = []

    def v(cid: str, text: str, result: str, evidence: Any) -> None:
        out.append({"id": cid, "criterion": text, "source": src, "result": result,
                    "evidence": evidence})

    helix_stages = ("fault", "recovery_hint", "recovery_action", "helix_hold",
                    "arbiter_forced_zero", "cmd_vel_zero")
    miss = [s for s in helix_stages if stages[s]["status"] != "observed"]
    unavail = [s for s in miss if stages[s]["status"].startswith("topic_")]
    v("full_chain", "fault, hint, action, hold, arbiter zero, output zero all observed",
      "PASS" if not miss else ("INCOMPLETE" if len(unavail) == len(miss) else "FAIL"),
      {"missing": miss})
    m = rep["motion"]
    n = m["nonzero_outputs_while_held"]
    v("zero_nonzero_while_held", "0 nonzero /cmd_vel outputs while held",
      "INCOMPLETE" if n is None else ("PASS" if n == 0 else "FAIL"), n)
    mv = m["move_requests_while_held"]
    v("no_move_while_held", "no Move (1008) request sent while held",
      "INCOMPLETE" if mv is None else ("PASS" if mv == 0 else "FAIL"), mv)
    resp = m.get("sport_response")
    st = stages["sport_response"]["status"]
    if resp is not None:
        res = "PASS" if resp.get("code") == 0 else "FAIL"
    elif stages["sport_stopmove_request"]["status"] == "observed" and st == "not_observed":
        res = "FAIL"
    else:
        res = "INCOMPLETE"
    v("stopmove_acknowledged", "StopMove reached the robot and was answered with code 0", res,
      {"request_id": m.get("stopmove_request_id"), "response": resp,
       "request_stage": stages["sport_stopmove_request"]["status"], "response_stage": st})
    sp = stop.get("speed_at_fault_mps")
    moving = crit.get("moving_speed_mps", 0.05)
    v("moving_at_fault", f"robot moving (> {moving} m/s) when the fault was raised",
      "INCOMPLETE" if sp is None else ("PASS" if sp > moving else "FAIL"),
      {"speed_at_fault_mps": sp, "odom_age_at_fault_s": stop.get("odom_age_at_fault_s")})
    dl = crit.get("stop_deadline_sec", 1.5)
    status = stop.get("status")
    if status == "stopped" and stop.get("already_stopped_at_hold"):
        res = "INCOMPLETE"  # nothing to stop: the criterion was not exercised
    elif status == "stopped":
        res = "PASS" if stop["stop_latency_s"] <= dl else "FAIL"
    elif status == "not_stopped":
        res = "FAIL"
    else:
        res = "INCOMPLETE"
    v("stopped_within_deadline",
      f"robot physically stopped (< {crit.get('stopped_speed_mps', 0.03)} m/s) within {dl} s "
      "of the hold", res,
      {"status": status, "reason": stop.get("reason") or (
          "robot already below the stopped threshold at the hold" if
          stop.get("already_stopped_at_hold") else None),
       "stop_latency_s": stop.get("stop_latency_s"),
       "uncertainty_s": stop.get("stop_latency_uncertainty_s")})
    if rep.get("synthetic"):
        for x in out:
            x["note"] = "SYNTHETIC bundle: verdict exercises the pipeline, not hardware"
    return out


def iter_problems(report: dict[str, Any]) -> Iterable[str]:
    """Short list of things a reader should look at first."""
    for v in report.get("verdicts", []):
        if v["result"] != "PASS":
            yield f"{v['id']}: {v['result']}"
    q = report.get("data_quality", {})
    for w in q.get("clock_warnings", []):
        yield w
    if q.get("writer_errors"):
        yield f"bundle writer errors: {q['writer_errors']} ({q.get('first_write_error')})"
    if q.get("recorder_ring_evicted_in_window"):
        yield f"recorder evicted {q['recorder_ring_evicted_in_window']} records inside the window"
