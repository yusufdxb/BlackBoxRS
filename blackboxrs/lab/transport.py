"""Transport-level data quality of the replayed stream, by the flight analyzer.

Duplicate, reordering and sequence-loss detection already exist
in :func:`blackboxrs.flight.analysis.analyze`. The replay runs that function
on the stream as it was delivered (after fault injection, in delivery order,
recorded outputs suppressed by the reference mode left out) and turns its
data-quality counters into findings. Nothing here counts duplicates or
sequence gaps itself.

``analyze`` reports counts per topic, not the first offending message, so
these findings are placed at the end of the replay and say they come from a
whole-stream pass.

The flight report's stop-chain verdicts (stopped within 1.5 s, and so on)
are deliberately not used: they judge recorded odometry, which a software
replay cannot change, so they would report the recorded physical response
as if it followed the injected faults.
"""

from __future__ import annotations

from typing import Any

from blackboxrs.flight.analysis import analyze
from blackboxrs.lab.events import ReplayEvent, to_record
from blackboxrs.lab.monitors import Finding


def analyze_delivered(manifest: dict[str, Any], delivered: list[ReplayEvent], t0_mono_ns: int,
                      t_end_ns: int) -> tuple[list[Finding], dict[str, Any]]:
    records = [to_record(e, t0_mono_ns, i + 1) for i, e in enumerate(delivered)]
    rep = analyze(manifest, records)
    q = rep["data_quality"]
    out: list[Finding] = []

    def add(kind: str, subject: str, n: int, what: str) -> None:
        out.append(Finding(t_end_ns, "transport", kind, "warning", subject,
                           f"{subject}: {n} {what} (flight analyzer, whole-stream pass)",
                           data={"count": n}))

    dups: dict[str, int] = dict(q.get("duplicates") or {})
    for name, st in sorted(rep["topics"].items()):
        seq = st.get("sequence") or {}
        if seq.get("duplicates"):
            dups[name] = max(dups.get(name, 0), seq["duplicates"])
        if seq.get("reordered"):
            add("sequence_reordered", name, seq["reordered"],
                "message(s) arrived with an older publisher sequence number")
    for name, n in sorted(dups.items()):
        add("duplicate_messages", name, n, "duplicate message(s)")
    for name, n in sorted((q.get("out_of_order_stamps") or {}).items()):
        add("out_of_order_stamps", name, n, "message(s) stamped earlier than a previous one")
    for name, n in sorted((q.get("publisher_sequence_lost") or {}).items()):
        add("sequence_loss", name, n, "message(s) missing from the publisher sequence")
    summary = {k: q.get(k) for k in ("duplicates", "out_of_order_stamps",
                                      "publisher_sequence_lost")}
    summary["sequence"] = {n: st["sequence"] for n, st in sorted(rep["topics"].items())
                           if st.get("sequence")}
    return out, summary
