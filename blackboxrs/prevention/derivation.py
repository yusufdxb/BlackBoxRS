"""Derive traceable prevention rules from incident evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib

from blackboxrs.incident.bundle import BundleReader
from blackboxrs.incident.models import DetectorTrigger, Incident

from .rules import PreflightCheck, PreventionRule, make_rule
from .telemetry_health import (
    TelemetryHealthContract,
    derive_thresholds,
    load_telemetry_evidence,
    verify_evidence_sources,
    verify_source_event_binding,
)


class PreventionDerivationError(ValueError):
    """Raised when an incident cannot safely produce an automatic rule."""


@dataclass(frozen=True)
class RuleDerivation:
    """A derived rule plus the evidence trigger that produced it."""

    rule: PreventionRule
    source_trigger: DetectorTrigger
    reason: str


_SUPPORTED_TRIGGER_CLASSES = {
    "DeadTopicDetector": "topic_present",
    "QoSMismatchDetector": "qos_match",
}


def derive_rule_from_bundle(
    reader: BundleReader, *, min_confidence: float = 0.70
) -> RuleDerivation:
    """Derive one prevention rule from a loaded incident bundle.

    The mapping is intentionally narrow. Only detector classes with a
    concrete preflight equivalent are auto-derived, and the rule records
    the incident, fingerprint, and exact trigger id that justified it.
    """
    result = reader.validate(require_finalized=True)
    if result.errors:
        details = "; ".join(
            f"{issue.code}:{issue.path or '-'}:{issue.message}"
            for issue in result.errors
        )
        raise PreventionDerivationError(
            f"Incident bundle failed integrity validation: {details}"
        )
    return derive_rule_from_incident(
        reader.load_incident(),
        reader.load_triggers(),
        min_confidence=min_confidence,
    )


def derive_rule_from_incident(
    incident: Incident,
    triggers: list[DetectorTrigger],
    *,
    min_confidence: float = 0.70,
) -> RuleDerivation:
    """Derive one prevention rule from incident metadata and triggers."""
    if not incident.likely_causes:
        raise PreventionDerivationError("No likely causes; nothing to adopt.")

    top = incident.likely_causes[0]
    if top.confidence < min_confidence:
        raise PreventionDerivationError(
            f"Top hypothesis confidence {top.confidence:.2f} below threshold "
            f"{min_confidence:.2f}; refuse to auto-adopt."
        )

    trigger = _select_supported_trigger(top.evidence_refs, triggers, top.cause)
    if trigger is None:
        supported = ", ".join(sorted(_SUPPORTED_TRIGGER_CLASSES))
        raise PreventionDerivationError(
            f"No supported trigger found for automatic prevention rule "
            f"(supported: {supported})."
        )

    check = _check_for_trigger(trigger)
    rationale = f"{top.cause} Evidence trigger: {trigger.trigger_id}."
    rule = make_rule(
        check,
        rationale=rationale,
        source_incident_id=incident.incident_id,
        source_fingerprint_id=(
            incident.fingerprint.fingerprint_id if incident.fingerprint else None
        ),
        source_trigger_ids=[trigger.trigger_id],
        derivation={
            "strategy": "detector_preflight_mapping_v1",
            "source_detector_class": trigger.detector_class,
            "source_trigger_id": trigger.trigger_id,
            "source_event_ref": trigger.source_event_ref,
            "hypothesis_confidence": top.confidence,
        },
    )
    return RuleDerivation(
        rule=rule,
        source_trigger=trigger,
        reason=f"{_short_detector_class(trigger)} -> {check.kind}",
    )


def derive_telemetry_health_rule(
    reader: BundleReader,
    evidence_path: Path,
    *,
    min_confidence: float = 0.70,
) -> RuleDerivation:
    """Strengthen one dead-topic incident into a bounded runtime contract.

    The incident must be finalized and must explicitly reference a
    DeadTopicDetector trigger. The healthy evidence is independently
    content-addressed and its thresholds must exactly match the fixed v1
    derivation method.
    """
    result = reader.validate(require_finalized=True)
    if result.errors:
        details = "; ".join(
            f"{issue.code}:{issue.path or '-'}:{issue.message}"
            for issue in result.errors
        )
        raise PreventionDerivationError(
            f"Incident bundle failed integrity validation: {details}"
        )

    incident = reader.load_incident()
    triggers = reader.load_triggers()
    if not incident.likely_causes:
        raise PreventionDerivationError("No likely causes; nothing to adopt.")
    top = incident.likely_causes[0]
    if top.confidence < min_confidence:
        raise PreventionDerivationError(
            f"Top hypothesis confidence {top.confidence:.2f} below threshold "
            f"{min_confidence:.2f}; refuse to auto-adopt."
        )
    trigger = _select_supported_trigger(top.evidence_refs, triggers, top.cause)
    if trigger is None or _short_detector_class(trigger) != "DeadTopicDetector":
        raise PreventionDerivationError(
            "Telemetry-health derivation requires an evidence-linked "
            "DeadTopicDetector trigger."
        )
    if not trigger.source_event_ref:
        raise PreventionDerivationError("Source trigger has no event evidence reference.")
    if incident.fingerprint is None:
        raise PreventionDerivationError(
            "Telemetry-health derivation requires a source incident fingerprint."
        )
    if trigger.trigger_id not in incident.triggers:
        raise PreventionDerivationError(
            "Source incident does not cross-reference the selected trigger."
        )
    bundle_fingerprint = reader.load_fingerprint()
    if bundle_fingerprint != incident.fingerprint:
        raise PreventionDerivationError(
            "Source incident fingerprint files are inconsistent."
        )

    try:
        evidence = load_telemetry_evidence(Path(evidence_path))
    except (OSError, ValueError) as exc:
        raise PreventionDerivationError(str(exc)) from exc
    try:
        verify_evidence_sources(evidence)
    except ValueError as exc:
        raise PreventionDerivationError(str(exc)) from exc
    try:
        verify_source_event_binding(reader.path, trigger)
    except ValueError as exc:
        raise PreventionDerivationError(str(exc)) from exc
    required_refs = {
        trigger.source_event_ref,
        f"triggers.json#{trigger.trigger_id}",
    }
    if None in required_refs or not required_refs.issubset(set(top.evidence_refs)):
        raise PreventionDerivationError(
            "Source hypothesis is missing trigger or event cross-reference."
        )
    if evidence.topic != trigger.subject:
        raise PreventionDerivationError(
            f"Healthy evidence topic {evidence.topic!r} does not match "
            f"incident topic {trigger.subject!r}."
        )
    if evidence.statistics.message_count < 1000:
        raise PreventionDerivationError(
            "Healthy telemetry evidence has fewer than 1000 messages."
        )
    expected_thresholds = derive_thresholds(evidence.statistics)
    if evidence.thresholds != expected_thresholds:
        raise PreventionDerivationError(
            "Selected thresholds do not match dead_topic_telemetry_health_v1."
        )

    thresholds = evidence.thresholds
    contract = TelemetryHealthContract(
        topic=evidence.topic,
        expected_type=evidence.message_type,
        expected_qos=evidence.offered_qos,
        graph_context=evidence.graph_context,
        startup_grace_sec=thresholds.startup_grace_sec,
        stale_timeout_sec=thresholds.stale_timeout_sec,
        minimum_rate_hz=thresholds.minimum_rate_hz,
        rate_window_sec=thresholds.rate_window_sec,
        header_progress_timeout_sec=thresholds.header_progress_timeout_sec,
        require_header_progress=True,
        lifecycle_stages=["startup", "runtime"],
    )
    check = PreflightCheck(
        name=f"runtime telemetry healthy: {trigger.subject}",
        kind="telemetry_health",
        params=contract.model_dump(mode="json"),
        severity_on_fail="block",
        applies_to=[evidence.graph_context],
    )
    rationale = (
        f"{top.cause} Require evidence-derived startup and sustained "
        f"telemetry health for {trigger.subject}."
    )
    rule = make_rule(
        check,
        rationale=rationale,
        source_incident_id=incident.incident_id,
        source_fingerprint_id=(
            incident.fingerprint.fingerprint_id if incident.fingerprint else None
        ),
        source_trigger_ids=[trigger.trigger_id],
        derivation={
            "strategy": "dead_topic_telemetry_health_v1",
            "source_detector_class": trigger.detector_class,
            "source_trigger_id": trigger.trigger_id,
            "source_event_ref": trigger.source_event_ref,
            "source_topic": trigger.subject,
            "hypothesis_confidence": top.confidence,
            "healthy_evidence_ref": f"{Path(evidence_path).resolve()}#statistics",
            "healthy_evidence_fingerprint": evidence.evidence_fingerprint,
            "source_bag_sha256": evidence.source_bag_sha256,
            "source_incident_ref": str(reader.path.resolve()),
            "source_incident_manifest_sha256": hashlib.sha256((reader.path / "manifest.json").read_bytes()).hexdigest(),
            "healthy_statistics": {
                "message_count": evidence.statistics.message_count,
                "mean_rate_hz": evidence.statistics.mean_rate_hz,
                "median_rate_hz": evidence.statistics.median_rate_hz,
                "p95_inter_arrival_sec": (
                    evidence.statistics.inter_arrival_sec["p95"]
                ),
                "p99_inter_arrival_sec": (
                    evidence.statistics.inter_arrival_sec["p99"]
                ),
                "max_healthy_gap_sec": (
                    evidence.statistics.inter_arrival_sec["max"]
                ),
            },
            "selected_thresholds": thresholds.model_dump(mode="json"),
            "threshold_derivation": evidence.derivation_method,
            "confidence_bounds": evidence.confidence_bounds,
        },
    )
    return RuleDerivation(
        rule=rule,
        source_trigger=trigger,
        reason="DeadTopicDetector + genuine healthy telemetry -> telemetry_health",
    )


def _select_supported_trigger(
    evidence_refs: list[str],
    triggers: list[DetectorTrigger],
    cause_text: str,
) -> DetectorTrigger | None:
    supported = [
        trigger for trigger in triggers
        if _short_detector_class(trigger) in _SUPPORTED_TRIGGER_CLASSES
    ]
    if not supported:
        return None

    expected_class = _expected_class_from_cause(cause_text)

    # Prefer the trigger explicitly referenced by the top hypothesis.
    refs = set(evidence_refs)
    for trigger in supported:
        short_class = _short_detector_class(trigger)
        if f"triggers.json#{trigger.trigger_id}" in refs:
            if expected_class is not None and short_class != expected_class:
                continue
            return trigger
        if trigger.source_event_ref and trigger.source_event_ref in refs:
            if expected_class is not None and short_class != expected_class:
                continue
            return trigger
    if refs:
        return None

    # Legacy bundles may lack evidence refs. Keep a narrow compatibility path
    # only when the top cause text names a supported failure family and exactly
    # one matching trigger exists. Otherwise refuse to guess.
    if expected_class is None:
        return None
    matching = [
        trigger for trigger in supported
        if _short_detector_class(trigger) == expected_class
    ]
    if len(matching) != 1:
        return None
    return matching[0]


def _check_for_trigger(trigger: DetectorTrigger) -> PreflightCheck:
    cls = _short_detector_class(trigger)
    if cls == "DeadTopicDetector":
        return PreflightCheck(
            name=f"topic present: {trigger.subject}",
            kind="topic_present",
            params={
                "topic": trigger.subject,
                "min_publishers": 1,
                "source_trigger_id": trigger.trigger_id,
            },
            severity_on_fail="block",
        )
    if cls == "QoSMismatchDetector":
        return PreflightCheck(
            name=f"qos match on {trigger.subject}",
            kind="qos_match",
            params={
                "topic": trigger.subject,
                "source_trigger_id": trigger.trigger_id,
            },
            severity_on_fail="block",
        )
    raise PreventionDerivationError(
        f"No automatic mapping for detector class {trigger.detector_class!r}."
    )


def _short_detector_class(trigger: DetectorTrigger) -> str:
    return trigger.detector_class.rsplit(".", 1)[-1]


def _expected_class_from_cause(cause_text: str) -> str | None:
    cause_lower = cause_text.lower()
    if "qos mismatch" in cause_lower:
        return "QoSMismatchDetector"
    if "stopped emitting" in cause_lower:
        return "DeadTopicDetector"
    return None
