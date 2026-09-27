"""Plain-text rendering of a replay result."""

from __future__ import annotations

from typing import Any

_LAYER = {"fault": "FAULT", "input": "INPUT", "detector": "DETECT", "decision": "DECIDE",
          "output": "OUTPUT", "invariant": "INVARIANT"}


def timeline_line(e: dict[str, Any]) -> str:
    cause = f"  <- {', '.join(e['caused_by'])}" if e.get("caused_by") else ""
    if e.get("related_faults"):
        cause += f"  [related fault: {', '.join(e['related_faults'])}]"
    ident = f"{e['id']} " if e.get("id") else ""
    t = e.get("t") or f"{e['t_ns'] / 1e9:+.3f}s"
    return f"{ident}{t:>9}  {_LAYER.get(e['layer'], e['layer']):<9} {e['text']}{cause}"


def render_text(res: dict[str, Any], *, timeline: bool = True, max_lines: int = 200) -> str:
    ev = res["evidence"]
    sut = res["config"]["sut"]
    v = res["verdict"]
    lines = [f"Replay Lab  run {res['run_id'][:12]}",
             f"evidence  {ev['source']}  ({ev['records']} records, digest {ev['digest'][:12]}"
             + (", SYNTHETIC" if ev["synthetic"] else "") + (", PARTIAL" if ev["partial"] else "")
             + ")"]
    if sut["mode"] == "reference":
        lines.append(f"sut       reference model: {sut['preset']}"
                     + (f" (freshness clock: {sut['freshness_clock']})"
                        if sut["freshness_clock"] != "receipt" else ""))
    else:
        lines.append(f"sut       observed output: {sut['output_source'] or 'NOT RECORDED'}")
    rp = res["replay"]
    lines.append(f"replay    {rp['events_delivered']} events, {rp['ticks']} ticks, "
                 f"{rp['window_start_ns'] / 1e9:g}..{rp['window_end_ns'] / 1e9:.3f} s")
    if rp["suppressed_recorded_outputs"]:
        n = sum(rp["suppressed_recorded_outputs"].values())
        lines.append(f"          {n} recorded arbiter-output records replaced by the model")
    for note in rp["notes"]:
        lines.append(f"note      {note}")
    for f in res["injections"]:
        p = ", ".join(f"{k}={val}" for k, val in f["params"].items() if val is not None)
        lines.append(f"fault     {f['id']} {f['kind']}({p}) -> {f['events_affected']} event(s)")
    lines.append("")
    lines.append("invariants")
    for name, inv in res["invariants"].items():
        extra = ""
        if inv["status"] == "FAIL":
            extra = (f"  first at {inv['first_violation_t_ns'] / 1e9:+.3f}s, "
                     f"{inv['violating_ticks']} violating tick(s)")
        elif inv["status"] == "INCOMPLETE":
            extra = f"  ({inv['incomplete_reason']})"
        lines.append(f"  {inv['status']:<13} {name}{extra}")
    lines.append("")
    lines.append("findings")
    shown = [f for f in res["findings"] if f["severity"] != "info"]
    infos = len(res["findings"]) - len(shown)
    if not shown:
        lines.append("  none above info")
    for f in shown:
        lines.append(f"  {f['id']} {f['t_s']:+.3f}s {f['severity']:<8} {f['kind']}: "
                     f"{f['message']}")
    if infos:
        lines.append(f"  (+{infos} info finding(s); see --json)")
    if timeline:
        lines.append("")
        lines.append("causal timeline")
        tl = res["timeline"]
        for e in tl[:max_lines]:
            lines.append("  " + timeline_line(e))
        if len(tl) > max_lines:
            lines.append(f"  ... {len(tl) - max_lines} more entries (see --json)")
    lines.append("")
    lines.append(f"VERDICT   {v['result']}: {'; '.join(v['reasons'])}")
    if v["invariants_not_exercised"]:
        lines.append(f"          not exercised: {', '.join(v['invariants_not_exercised'])}")
    return "\n".join(lines)
