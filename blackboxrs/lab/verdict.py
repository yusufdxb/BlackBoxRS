"""Replay verdict, plain-language summary, and expectation checks.

Verdict, in order of precedence:

``FAIL``        a safety invariant was violated
``INCOMPLETE``  an invariant could not be evaluated (for example the
                evidence records no arbitration output) or the evidence was
                partial; a replay that cannot see the output never passes
``DETECTED``    every invariant held, and a detector reported a warning or
                critical finding (a fault was present and was caught)
``PASS``        every invariant held and nothing above info was reported

``NOT_EXERCISED`` invariants (for example ``stop_dominance`` when no hold
was ever asserted) do not block PASS; they are listed so a reader can see
what the replay did not test.
"""

from __future__ import annotations

from typing import Any

VERDICTS = ("PASS", "DETECTED", "FAIL", "INCOMPLETE")
EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3, "DETECTED": 4}
EXIT_ERROR = 5   # malformed evidence, case or fault definition (click usage errors are 2)


def detection_kinds(result: dict[str, Any]) -> list[str]:
    return sorted({f["kind"] for f in result["findings"]
                   if f["severity"] in ("warning", "critical") and not f["invariant"]})


def compute_verdict(result: dict[str, Any]) -> dict[str, Any]:
    inv = result["invariants"]
    failed = sorted(n for n, v in inv.items() if v["status"] == "FAIL")
    incomplete = sorted(n for n, v in inv.items() if v["status"] == "INCOMPLETE")
    not_exercised = sorted(n for n, v in inv.items() if v["status"] == "NOT_EXERCISED")
    kinds = detection_kinds(result)
    reasons = []
    if failed:
        verdict = "FAIL"
        reasons.append(f"invariant(s) violated: {', '.join(failed)}")
    elif incomplete or result["evidence"]["partial"]:
        verdict = "INCOMPLETE"
        for n in incomplete:
            reasons.append(f"{n}: {inv[n]['incomplete_reason']}")
        if result["evidence"]["partial"]:
            reasons.append("evidence is partial: " + "; ".join(result["evidence"]["problems"]))
    elif kinds:
        verdict = "DETECTED"
        reasons.append(f"invariants held; detections: {', '.join(kinds)}")
    else:
        verdict = "PASS"
        reasons.append("invariants held; no warning or critical finding")
    for note in result["replay"].get("notes") or []:
        reasons.append(note)
    return {"result": verdict, "reasons": reasons, "invariants_failed": failed,
            "invariants_incomplete": incomplete, "invariants_not_exercised": not_exercised,
            "detections": kinds}


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Direct answers to: what happened, which detector, which decision, what reached the robot."""
    tl = result["timeline"]
    finds = result["findings"]
    first_fault = next((e for e in tl if e["layer"] == "fault"), None)
    first_det = next((f for f in finds if f["severity"] != "info" and not f["invariant"]), None)
    first_viol = next((f for f in finds if f["invariant"]), None)
    decisions = [e for e in tl if e["layer"] == "decision"]
    outputs = [e for e in tl if e["layer"] == "output"]
    after = first_fault["t_ns"] if first_fault else None
    return {
        "faults": [f"{f['id']} {f['kind']}" for f in result["injections"]],
        "first_fault": None if not first_fault else {"t_s": first_fault["t_s"],
                                                     "text": first_fault["text"]},
        "first_detection": None if not first_det else {
            "t_s": first_det["t_s"], "kind": first_det["kind"], "subject": first_det["subject"]},
        "first_decision_after_fault": next(
            ({"t_s": e["t_s"], "text": e["text"]} for e in decisions
             if after is not None and e["t_ns"] >= after), None),
        "final_robot_command": outputs[-1]["text"] if outputs else None,
        "first_invariant_violation": None if not first_viol else {
            "t_s": first_viol["t_s"], "invariant": first_viol["invariant"],
            "message": first_viol["message"]},
        "invariants": {n: v["status"] for n, v in result["invariants"].items()},
    }


def check_expectations(result: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Mismatches between a result and a case's ``expect`` block ([] = match).

    ``detections`` is compared as an exact set of warning/critical,
    non-invariant finding kinds, so an unexpected extra detection (a false
    positive) is a mismatch too.
    """
    bad = sorted(set(expect) - {"verdict", "invariants", "detections", "info"})
    if bad:
        return [f"unknown expect keys {bad}"]
    out = []
    v = result["verdict"]["result"]
    if "verdict" in expect and expect["verdict"] != v:
        out.append(f"verdict: expected {expect['verdict']}, got {v}")
    for name, want in sorted((expect.get("invariants") or {}).items()):
        got = (result["invariants"].get(name) or {}).get("status")
        if got != want:
            out.append(f"invariant {name}: expected {want}, got {got}")
    if "detections" in expect:
        want = sorted(set(expect["detections"]))
        got = detection_kinds(result)
        if want != got:
            missing = sorted(set(want) - set(got))
            extra = sorted(set(got) - set(want))
            out.append(f"detections: missing {missing}, unexpected {extra}")
    for kind in expect.get("info") or []:
        if not any(f["kind"] == kind for f in result["findings"]):
            out.append(f"info finding {kind} not reported")
    return out
