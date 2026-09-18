"""Human-readable summary (report.md) of a flight report."""

from __future__ import annotations

from typing import Any

from blackboxrs.flight.analysis import CHAIN, iter_problems


def _fmt_t(ev: dict[str, Any] | None) -> str:
    if not ev:
        return ""
    parts = [f"rx_mono={ev['t_mono_ns'] / 1e9:.6f}"]
    if ev.get("dds_src_ns"):
        parts.append(f"dds_src={ev['dds_src_ns'] / 1e9:.6f}")
    if ev.get("pub_stamp_s") is not None:
        parts.append(f"stamp={ev['pub_stamp_s']:.6f} ({ev.get('pub_stamp_domain')})")
    return ", ".join(parts)


def render_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = []
    prov = rep.get("provenance") or {}
    title = rep.get("bundle_id") or "incident"
    L.append(f"# BlackBoxRS flight incident `{title}`")
    L.append("")
    if rep.get("synthetic"):
        L.append("> **SYNTHETIC DATA.** This bundle was produced from generated traffic. "
                 "Nothing in it is hardware evidence.")
        L.append("")
    trig = rep.get("trigger") or {}
    L.append(f"- Status: **{rep.get('status')}**")
    L.append(f"- Trigger: `{trig.get('type')}` "
             + " ".join(f"{k}={trig[k]}" for k in ("topic", "node", "fault_id", "reason", "note")
                        if trig.get(k)))
    if rep.get("secondary_triggers"):
        L.append("- Also fired: " + ", ".join(
            f"`{t.get('type')}`" for t in rep["secondary_triggers"]))
    L.append(f"- Session: `{prov.get('session_id')}` experiment `{prov.get('experiment')}`")
    L.append(f"- Host: {prov.get('hostname')} ({prov.get('platform')}), ROS {prov.get('ros_distro')}, "
             f"RMW {prov.get('rmw_implementation')}")
    L.append(f"- BlackBoxRS {prov.get('blackboxrs_version')} @ `{prov.get('blackboxrs_git_sha')}`"
             f"{' (dirty)' if prov.get('blackboxrs_git_dirty') else ''}; profile "
             f"`{rep['profile']['name']}` sha256 `{(rep['profile']['sha256'] or '')[:12]}`")
    if prov.get("excluded_topics"):
        L.append(f"- Topics excluded for this run: {prov['excluded_topics']}")
    for repo in prov.get("experiment_repos") or []:
        L.append(f"- Experiment repo {repo.get('path')}: `{repo.get('sha')}`"
                 f"{' (dirty)' if repo.get('dirty') else ''}")
    w = rep["window"]
    L.append(f"- Window: {w['seconds_before_trigger']} s before, {w['seconds_after_trigger']} s "
             f"after the trigger; {w['messages']} messages")
    L.append("")
    probs = list(iter_problems(rep))
    L.append("## Look here first")
    L.append("")
    L.extend(f"- {p}" for p in probs) if probs else L.append("- nothing flagged")
    L.append("")
    if rep.get("verdicts"):
        L.append("## Verdicts (explicit criteria only)")
        L.append("")
        L.append("| Check | Result | Evidence |")
        L.append("|---|---|---|")
        for v in rep["verdicts"]:
            L.append(f"| {v['criterion']} | **{v['result']}** | `{v['evidence']}` |")
        L.append("")
    L.append("## HELIX stop chain")
    L.append("")
    L.append("| Stage | Status | Times |")
    L.append("|---|---|---|")
    for k in CHAIN:
        st = rep["chain"].get(k, {})
        L.append(f"| {k} | {st.get('status')} | {_fmt_t(st.get('evidence'))} |")
    L.append("")
    if rep.get("key_spans"):
        L.append("### Intervals")
        L.append("")
        L.append("| From | To | Seconds | Basis | Clock |")
        L.append("|---|---|---|---|---|")
        for s in rep["key_spans"]:
            unc = f" ± {s['uncertainty_s']}" if s.get("uncertainty_s") is not None else ""
            L.append(f"| {s['from']} | {s['to']} | {s['value_s']}{unc} | {s['basis']} | "
                     f"{s['clock_domain']} |")
        L.append("")
        L.append("`emission` intervals compare publisher stamps on one clock. `receipt` "
                 "intervals are when the recorder saw each message: they include transport and "
                 "are not causal latencies.")
        L.append("")
    m = rep["motion"]
    L.append("## Motion")
    L.append("")
    L.append(f"- Input twist before hold: `{m.get('input_twist')}`")
    L.append(f"- Final /cmd_vel: `{m.get('final_twist')}` (source: {m.get('outputs_source')})")
    L.append(f"- Nonzero outputs while held: {m.get('nonzero_outputs_while_held')}; Move requests "
             f"while held: {m.get('move_requests_while_held')}")
    L.append(f"- StopMove request id: {m.get('stopmove_request_id')}; response: "
             f"{m.get('sport_response')}")
    od = m.get("odometry") or {}
    L.append(f"- Odometry: status **{od.get('status')}**"
             + (f" ({od.get('reason')})" if od.get("reason") else ""))
    for k in ("speed_at_fault_mps", "speed_at_hold_mps", "stop_latency_s",
              "stop_latency_uncertainty_s", "robot_clock_stop_duration_s", "stop_distance_m",
              "stop_distance_reason", "robot_clock_offset_s", "speed_time_base"):
        if k in od:
            L.append(f"  - {k}: {od[k]}")
    L.append("")
    L.append("## Topics")
    L.append("")
    L.append("| Topic | Availability | Count | Hz before/during/after | Max gap s | Lost | Dup | OOO |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name, s in rep["topics"].items():
        r = s.get("rate_hz") or {}
        seq = s.get("sequence") or {}
        L.append(f"| {name} | {s['availability']} | {s['count']} | "
                 f"{r.get('before')}/{r.get('during')}/{r.get('after')} | {s.get('max_gap_s')} | "
                 f"{seq.get('lost', '')} | {s.get('duplicates', '')} | "
                 f"{s.get('out_of_order_stamps', '')} |")
    L.append("")
    res = rep.get("resources") or {}
    L.append("## Resources (max before/during/after)")
    L.append("")
    if res.get("available"):
        for key in ("cpu_percent", "mem_percent", "recorder_cpu_percent", "recorder_rss_mb",
                    "gpu_load_percent", "gpu_temp_c"):
            if key in res:
                vals = ["-" if res[key][p] is None else str(res[key][p]["max"])
                        for p in ("before", "during", "after")]
                L.append(f"- {key}: {' / '.join(vals)}")
        if "gpu" in res:
            L.append(f"- gpu: unavailable ({', '.join(res['gpu']['reason'])})")
        if res.get("thermal_max_c"):
            L.append(f"- thermal max C: {res['thermal_max_c']}")
    else:
        L.append(f"- unavailable: {res.get('reason')}")
    L.append("")
    n = rep.get("nodes") or {}
    L.append("## Nodes")
    L.append("")
    L.append(f"- Publishers on profiled topics: {', '.join(n.get('publishers_on_profiled_topics') or []) or 'none'}")
    L.append(f"- Disappeared: {', '.join(n.get('disappeared') or []) or 'none'}")
    L.append(f"- Appeared: {', '.join(n.get('appeared') or []) or 'none'}")
    L.append("")
    return "\n".join(L)
