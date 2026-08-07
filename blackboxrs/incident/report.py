"""Markdown report renderer for incident bundles.

Templates are inlined to avoid pulling jinja in. Determinism matters:
the same bundle should produce byte-identical (modulo timestamps not in
the inputs) output every time.
"""

from __future__ import annotations

from datetime import datetime

from .bundle import BundleReader
from .models import (
    DetectorTrigger,
    FailureFingerprint,
    Incident,
    LikelyCauseHypothesis,
    TimelineEvent,
)
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_rule_from_incident,
)


_TIMELINE_ROW_LIMIT = 50


def _fmt_t(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.%fZ")


def _section(title: str) -> str:
    return f"\n## {title}\n\n"


def _header(incident: Incident) -> str:
    parts = [
        f"# Incident `{incident.incident_id}`",
        "",
        f"- **Severity**: {incident.severity}",
        f"- **Created**: {_fmt_t(incident.created_at)}",
        (
            f"- **Window**: {_fmt_t(incident.window_start)} → "
            f"{_fmt_t(incident.window_end)}"
        ),
        f"- **Session**: `{incident.session_id}`",
    ]
    if incident.observed_host or incident.observer_host:
        # Remote-observer bundles: name who captured vs. who the bundle
        # is about, so the observer's hostname isn't mistaken for the
        # robot's.
        observer = incident.observer_host or incident.host or "<unknown>"
        observed = incident.observed_host or "<unspecified>"
        parts.append(f"- **Observer**: `{observer}`")
        parts.append(f"- **Observed**: `{observed}`")
    elif incident.host:
        parts.append(f"- **Host**: `{incident.host}`")
    if incident.tags:
        parts.append(f"- **Tags**: {', '.join(incident.tags)}")
    parts.append(f"- **Schema**: v{incident.schema_version}")
    parts.append("")
    return "\n".join(parts)


def _summary(incident: Incident) -> str:
    if incident.summary:
        return _section("Summary") + incident.summary.rstrip() + "\n"
    return _section("Summary") + "_No generated summary._\n"


def _timeline_section(timeline: list[TimelineEvent]) -> str:
    if not timeline:
        return _section("Timeline") + "_Timeline is empty._\n"
    out = [_section("Timeline").rstrip(), ""]
    out.append("| t | subsystem | kind | summary | conf. | evidence |")
    out.append("|---|---|---|---|---|---|")
    rows = timeline[:_TIMELINE_ROW_LIMIT]
    for ev in rows:
        summary = ev.summary.replace("|", "\\|")
        out.append(
            f"| {_fmt_t(ev.t)} | {ev.subsystem} | {ev.kind} | "
            f"{summary} | {ev.confidence:.2f} | `{ev.evidence_ref}` |"
        )
    if len(timeline) > _TIMELINE_ROW_LIMIT:
        out.append("")
        out.append(
            f"_Truncated: {len(timeline) - _TIMELINE_ROW_LIMIT} additional "
            f"rows in `timeline.json`._"
        )
    return "\n".join(out) + "\n"


def _triggers_section(triggers: list[DetectorTrigger]) -> str:
    if not triggers:
        return _section("Triggers") + "_No detector triggers in this window._\n"
    out = [_section("Triggers").rstrip(), ""]
    for t in triggers:
        out.append(f"### `{t.trigger_id}`: {t.detector}")
        out.append("")
        out.append(f"- **t**: {_fmt_t(t.t)}")
        out.append(f"- **subject**: `{t.subject}`")
        out.append(f"- **severity**: {t.severity}")
        out.append(f"- **message**: {t.message}")
        if t.data:
            data_lines = [f"  - `{k}`: `{v!r}`" for k, v in sorted(t.data.items())]
            out.append("- **data**:")
            out.extend(data_lines)
        if t.source_event_ref:
            out.append(f"- **source**: `{t.source_event_ref}`")
        out.append("")
    return "\n".join(out)


def _causes_section(causes: list[LikelyCauseHypothesis]) -> str:
    if not causes:
        return (
            _section("Likely causes")
            + "_No hypotheses generated. See triggers + timeline above._\n"
        )
    out = [_section("Likely causes").rstrip(), ""]
    for i, h in enumerate(causes, start=1):
        head = f"{i}. **{h.cause}** _(confidence {h.confidence:.2f}"
        if h.score is not None:
            head += f", score {h.score:.2f}"
        if h.subsystem:
            head += f", subsystem `{h.subsystem}`"
        head += ")_"
        out.append(head)

        if h.precursor_chain:
            out.append("   - **precursor chain**:")
            # Render earliest-first so the reader sees "X happened, then
            # Y, then the trigger fired."
            for p in h.precursor_chain:
                out.append(
                    f"     - `{_fmt_t(p.t)}` ({p.gap_sec:.1f}s before "
                    f"trigger, subsystem `{p.subsystem}`, relevance "
                    f"{p.relevance:.2f}): {p.summary} "
                    f"[`{p.evidence_ref}`]"
                )

        if h.recurrence is not None:
            r = h.recurrence
            last = _fmt_t(r.last_seen_at) if r.last_seen_at else "(unknown)"
            ids = ", ".join(f"`{x}`" for x in r.prior_incident_ids)
            out.append(
                f"   - **recurrence**: this fingerprint matched "
                f"{r.prior_count} prior bundle(s); last seen {last}."
            )
            if ids:
                out.append(f"     - prior incidents: {ids}")

        if h.reasoning:
            out.append("   - **reasoning**:")
            for line in h.reasoning:
                out.append(f"     - {line}")

        if h.evidence_refs:
            refs = ", ".join(f"`{r}`" for r in h.evidence_refs)
            out.append(f"   - **evidence**: {refs}")
        if h.caveat:
            out.append(f"   - **caveat**: {h.caveat}")
    out.append("")
    return "\n".join(out)


def _signatures_section(reader: BundleReader) -> str:
    try:
        cfg, ver = reader.load_signatures()
    except FileNotFoundError:
        return _section("Signatures") + "_No signatures captured._\n"
    out = [_section("Signatures").rstrip(), ""]
    out.append("**Config signature**")
    out.append("")
    out.append(f"- hash: `{cfg.hash}`")
    out.append(
        f"- ros_distro: `{cfg.payload.get('ros_distro')}`  "
        f"rmw: `{cfg.payload.get('rmw_implementation')}`  "
        f"domain_id: `{cfg.payload.get('ros_domain_id')}`"
    )
    attached = cfg.payload.get("attached_files") or []
    if attached:
        out.append(f"- attached files: {len(attached)}")
    out.append("")
    out.append("**Version signature**")
    out.append("")
    out.append(f"- hash: `{ver.hash}`")
    os_blob = ver.payload.get("os") or {}
    py_blob = ver.payload.get("python") or {}
    out.append(
        f"- os: `{os_blob.get('name')} {os_blob.get('version')}` "
        f"(kernel `{os_blob.get('kernel')}`)"
    )
    out.append(f"- python: `{py_blob.get('version')}`")
    out.append(f"- blackboxrs: `{ver.payload.get('blackboxrs_version')}`")
    out.append(f"- nvidia driver: `{ver.payload.get('nvidia_driver')}`")
    out.append("")
    return "\n".join(out)


_DIFF_ROW_LIMIT = 20


def _signature_diff_section(reader: BundleReader) -> str:
    """Render the config/version diff against the most recent prior bundle.

    Returns an empty string when there is no diff (no baseline, or the
    builder did not persist one). When both signatures are identical,
    surfaces that fact explicitly: same-config recurrence is itself
    diagnostic.
    """
    diff = reader.load_diff()
    if diff is None:
        return ""

    cfg = diff.config
    ver = diff.versions

    out = [_section("Config / version diff vs prior session").rstrip(), ""]

    if cfg.baseline_hash is None and ver.baseline_hash is None:
        out.append(
            "_No prior bundle found on this host. Diff is the full "
            "current signature payload (omitted for brevity; see "
            "`signatures/diff.json`)._"
        )
        out.append("")
        return "\n".join(out)

    if cfg.identical and ver.identical:
        out.append(
            "_Config and version signatures are byte-identical to the "
            "prior session (`{}` / `{}`). The bug is not driven by a "
            "config change._".format(cfg.baseline_hash, ver.baseline_hash)
        )
        out.append("")
        return "\n".join(out)

    out.append("**Config**")
    out.append("")
    out.append(f"- baseline: `{cfg.baseline_hash}`")
    out.append(f"- current:  `{cfg.current_hash}`")
    if cfg.identical:
        out.append("- _identical_")
    else:
        _render_changes(out, cfg)
    out.append("")

    out.append("**Versions**")
    out.append("")
    out.append(f"- baseline: `{ver.baseline_hash}`")
    out.append(f"- current:  `{ver.current_hash}`")
    if ver.identical:
        out.append("- _identical_")
    else:
        _render_changes(out, ver)
    out.append("")
    out.append(
        "_The diff shows what changed between sessions. It does NOT "
        "claim causation: equality is not safety, and a difference is "
        "not a smoking gun without supporting evidence._"
    )
    out.append("")
    return "\n".join(out)


def _render_changes(out: list[str], side) -> None:
    """Append bulleted change rows for one SignatureDiff side."""
    truncated = False
    rows: list[str] = []
    for ch in side.changed:
        rows.append(f"- changed `{ch.key}`: `{ch.before!r}` -> `{ch.after!r}`")
    for ch in side.only_in_current:
        rows.append(f"- added   `{ch.key}`: `{ch.after!r}`")
    for ch in side.only_in_baseline:
        rows.append(f"- removed `{ch.key}`: was `{ch.before!r}`")

    if len(rows) > _DIFF_ROW_LIMIT:
        rows = rows[:_DIFF_ROW_LIMIT]
        truncated = True
    out.extend(rows)
    if truncated:
        out.append("  _(truncated; full diff in `signatures/diff.json`)_")


def _fingerprint_section(fp: FailureFingerprint | None) -> str:
    if fp is None:
        return _section("Fingerprint") + "_No fingerprint computed._\n"
    payload = fp.payload or {}
    out = [_section("Fingerprint").rstrip(), ""]
    out.append(f"- **id**: `{fp.fingerprint_id}`")
    out.append(f"- **algorithm**: {fp.algorithm_version}")
    classes = payload.get("detector_classes") or []
    out.append(
        f"- **detectors**: {', '.join(classes) if classes else '(none)'}"
    )
    subs = payload.get("subsystems") or []
    out.append(f"- **subsystems**: {', '.join(subs) if subs else '(none)'}")
    topo = payload.get("topology_signature")
    if topo:
        out.append(f"- **topology**: `{topo}`")
    out.append("")
    out.append("_This id will collide on a recurrence with the same surface._")
    out.append("")
    return "\n".join(out)


def _recommended_rule_section(
    causes: list[LikelyCauseHypothesis], triggers: list[DetectorTrigger]
) -> str:
    """Render a recommended preflight YAML when a credible rule is derivable.

    v0.4 covers the QoS mismatch and dead-topic cases: the two with
    well-defined preflight equivalents. Below threshold confidence we
    leave a TODO line instead of a YAML block so the user knows what's
    missing without seeing a recommendation that wasn't earned.
    """
    if not causes:
        return ""
    top = causes[0]
    if top.confidence < 0.7:
        return (
            _section("Recommended preflight rule")
            + "_Top hypothesis confidence below threshold (0.70). "
            "No recommendation emitted; review manually._\n"
        )

    # Derive a rule body from the highest-severity trigger that matches
    # the top hypothesis class.
    yaml_block = _yaml_for_top_cause(top, triggers)
    if yaml_block is None:
        return ""
    return (
        _section("Recommended preflight rule")
        + "Adopt with `robot-blackbox prevention adopt --from-incident <bundle-dir>`.\n\n"
        "```yaml\n"
        + yaml_block.rstrip()
        + "\n```\n"
    )


def _yaml_for_top_cause(
    top: LikelyCauseHypothesis, triggers: list[DetectorTrigger]
) -> str | None:
    """Map a top hypothesis to a concrete preflight YAML when possible."""
    incident = _minimal_incident_for_rule_render(top)
    try:
        derivation = derive_rule_from_incident(incident, triggers)
    except PreventionDerivationError:
        return None
    check = derivation.rule.check
    lines = [
        f"check: {check.kind}",
        "params:",
    ]
    for key, value in check.params.items():
        lines.append(f"  {key}: {value!r}")
    lines.extend([
        f"severity_on_fail: {check.severity_on_fail}",
        "rationale: |",
        f"  Generated from incident evidence: {top.cause}",
    ])
    return "\n".join(lines) + "\n"
    return None


def _minimal_incident_for_rule_render(top: LikelyCauseHypothesis):
    """Build the small Incident surface derivation needs for report YAML."""
    from datetime import datetime, timezone

    from .models import Incident

    now = datetime.now(timezone.utc)
    return Incident(
        incident_id="inc_report_preview",
        created_at=now,
        window_start=now,
        window_end=now,
        session_id="report_preview",
        title="report preview",
        bundle_path=".",
        likely_causes=[top],
    )


def _attachments_section(reader: BundleReader) -> str:
    attach = reader.path / "attachments"
    if not attach.exists():
        return ""
    items = sorted(attach.iterdir())
    if not items:
        return ""
    out = [_section("Attachments").rstrip(), ""]
    for entry in items:
        size = "?"
        try:
            size = f"{entry.stat().st_size} bytes"
        except OSError:
            pass
        out.append(f"- `{entry.name}`: {size}")
    out.append("")
    return "\n".join(out)


def render(reader: BundleReader) -> str:
    """Render a bundle to markdown."""
    incident = reader.load_incident()
    triggers = reader.load_triggers()
    timeline = reader.load_timeline()
    fp = reader.load_fingerprint()
    causes = incident.likely_causes

    chunks: list[str] = [
        _header(incident),
        _summary(incident),
        _timeline_section(timeline),
        _triggers_section(triggers),
        _causes_section(causes),
        _signatures_section(reader),
        _signature_diff_section(reader),
        _fingerprint_section(fp),
        _recommended_rule_section(causes, triggers),
        _attachments_section(reader),
    ]
    return "".join(chunk for chunk in chunks if chunk)


__all__ = ["render"]
