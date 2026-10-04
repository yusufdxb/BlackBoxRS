"""Derive traceable prevention rules from incident evidence."""

from __future__ import annotations

from dataclasses import dataclass

from blackboxrs.incident.bundle import BundleReader
from blackboxrs.incident.models import DetectorTrigger, Incident

from .rules import PreflightCheck, PreventionRule, make_rule


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
