"""Adversarial provenance verification for telemetry-health rules.

The cases are deliberately separated into traceability, integrity, and trusted
local approval.  The approval model is an exact operator-controlled fingerprint
pin.  It is not a signature scheme and these tests do not claim cryptographic
authenticity or model approval identities.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackboxrs.incident.bundle import BundleReader, BundleWriter
from blackboxrs.prevention.rules import (
    PreventionRule,
    compute_rule_fingerprint,
)
from blackboxrs.prevention.telemetry_health import (
    TelemetryHealthEvidence,
    compute_evidence_fingerprint,
    contract_from_rule,
)
from tests.telemetry_fixtures import (
    GRAPH_CONTEXT,
    TOPIC,
    TelemetryProvenanceFixture,
    build_telemetry_provenance_fixture,
)


def _approve(rule: PreventionRule, **updates: object) -> PreventionRule:
    changed = rule.model_copy(update=updates)
    return changed.model_copy(
        update={"rule_fingerprint": compute_rule_fingerprint(changed)}
    )


def _rule_with_params(
    rule: PreventionRule, **params: object
) -> PreventionRule:
    check = rule.check.model_copy(update={"params": {**rule.check.params, **params}})
    return _approve(rule, check=check)


def _rule_with_derivation(
    rule: PreventionRule, **derivation: object
) -> PreventionRule:
    return _approve(
        rule, derivation={**rule.derivation, **derivation}
    )


def _assert_refused(
    rule: PreventionRule,
    *,
    trusted_fingerprint: str | None = None,
    match: str | None = None,
) -> None:
    trusted = trusted_fingerprint
    if trusted is None:
        trusted = rule.rule_fingerprint
    with pytest.raises((OSError, ValueError), match=match):
        contract_from_rule(rule, trusted_rule_fingerprint=trusted)


def _read_evidence(path: Path) -> TelemetryHealthEvidence:
    return TelemetryHealthEvidence.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _write_evidence(path: Path, evidence: TelemetryHealthEvidence) -> None:
    path.write_text(
        json.dumps(
            evidence.model_dump(mode="json", by_alias=True),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _rehash_evidence(
    evidence: TelemetryHealthEvidence, **updates: object
) -> TelemetryHealthEvidence:
    changed = evidence.model_copy(update=updates)
    return changed.model_copy(
        update={"evidence_fingerprint": compute_evidence_fingerprint(changed)}
    )


def _rewrite_manifest(
    fixture: TelemetryProvenanceFixture,
    *,
    incident=None,
) -> str:
    writer = BundleWriter(fixture.bundle_path)
    current_incident = incident or BundleReader(fixture.bundle_path).load_incident()
    if incident is not None:
        writer.write_incident(incident)
    writer.write_manifest(
        writer.build_manifest(
            incident_id=current_incident.incident_id,
            created_at=current_incident.created_at,
        )
    )
    result = writer.validate(require_finalized=True)
    assert result.ok, result
    return hashlib.sha256(
        (fixture.bundle_path / "manifest.json").read_bytes()
    ).hexdigest()


def test_valid_control_adoption_and_runtime_provenance_passes(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)

    contract = contract_from_rule(
        fixture.rule,
        trusted_rule_fingerprint=fixture.rule.rule_fingerprint,
    )

    assert contract == fixture.contract
    assert contract.topic == TOPIC
    assert contract.declared_context_label == GRAPH_CONTEXT


# Traceability: incident -> trigger -> source event -> topic identity.


def test_traceability_05_wrong_incident_id_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _approve(
        fixture.rule,
        source_incident_id="inc_2026-07-23T00-00-00_deadbeef",
    )

    _assert_refused(rule, match="incident ID")


def test_traceability_06_wrong_trigger_id_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    derivation = {
        **fixture.rule.derivation,
        "source_trigger_id": "trg_deadbeef",
    }
    rule = _approve(
        fixture.rule,
        source_trigger_ids=["trg_deadbeef"],
        derivation=derivation,
    )

    _assert_refused(rule, match="trigger")


def test_traceability_07_wrong_detector_class_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _rule_with_derivation(
        fixture.rule,
        source_detector_class="attacker.DeadTopicDetector",
    )

    _assert_refused(rule, match="detector class")


def test_traceability_08_wrong_event_reference_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _rule_with_derivation(
        fixture.rule, source_event_ref="events.jsonl#L2"
    )

    _assert_refused(rule, match="event reference")


def test_traceability_09_event_reference_to_different_topic_fails_closed(
    tmp_path,
):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    different = fixture.event.model_copy(
        update={
            "data": {
                **fixture.event.data,
                "topic": "/different/topic",
            }
        }
    )
    trigger = fixture.trigger.model_copy(
        update={"source_event_ref": "events.jsonl#L2"}
    )
    reader = BundleReader(fixture.bundle_path)
    incident = reader.load_incident()
    top = incident.likely_causes[0].model_copy(
        update={
            "evidence_refs": [
                "events.jsonl#L2",
                f"triggers.json#{trigger.trigger_id}",
            ]
        }
    )
    incident = incident.model_copy(update={"likely_causes": [top]})
    writer = BundleWriter(fixture.bundle_path)
    writer.write_events_jsonl([fixture.event, different])
    writer.write_triggers([trigger])
    manifest_hash = _rewrite_manifest(fixture, incident=incident)
    rule = _rule_with_derivation(
        fixture.rule,
        source_event_ref="events.jsonl#L2",
        source_incident_manifest_sha256=manifest_hash,
    )

    _assert_refused(rule, match="event topic differs")


def test_traceability_22_rule_topic_differs_from_incident_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    check = fixture.rule.check.model_copy(
        update={
            "params": {
                **fixture.rule.check.params,
                "topic": "/different/topic",
            }
        }
    )
    rule = _approve(
        fixture.rule,
        check=check,
        derivation={
            **fixture.rule.derivation,
            "source_topic": "/different/topic",
        },
    )

    _assert_refused(rule, match="topic differs from incident evidence")


def test_traceability_24_valid_hash_missing_source_cross_reference_fails_closed(
    tmp_path,
):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    trigger = fixture.trigger.model_copy(update={"source_event_ref": None})
    reader = BundleReader(fixture.bundle_path)
    incident = reader.load_incident()
    top = incident.likely_causes[0].model_copy(
        update={"evidence_refs": [f"triggers.json#{trigger.trigger_id}"]}
    )
    incident = incident.model_copy(update={"likely_causes": [top]})
    writer = BundleWriter(fixture.bundle_path)
    writer.write_triggers([trigger])
    manifest_hash = _rewrite_manifest(fixture, incident=incident)
    rule = _rule_with_derivation(
        fixture.rule,
        source_incident_manifest_sha256=manifest_hash,
    )

    _assert_refused(rule, match="event reference differs")


# Integrity: content hashes plus exact evidence-to-rule semantics.


def test_integrity_01_missing_evidence_file_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    fixture.evidence_path.unlink()

    _assert_refused(fixture.rule)


def test_integrity_02_missing_source_bag_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    for path in fixture.bag_path.iterdir():
        path.unlink()
    fixture.bag_path.rmdir()

    _assert_refused(fixture.rule, match="source bag is unavailable")


def test_integrity_03_changed_payload_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    payload = fixture.bag_path / "healthy_0.db3"
    payload.write_bytes(
        payload.read_bytes() + b"changed after trusted adoption"
    )

    _assert_refused(fixture.rule, match="source bag manifest mismatch")


def test_integrity_04_changed_metadata_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    metadata = fixture.bag_path / "metadata.yaml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8") + "# changed\n",
        encoding="utf-8",
    )

    _assert_refused(fixture.rule, match="source bag manifest mismatch")


def test_integrity_10_modified_event_file_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    event_path = fixture.bundle_path / "evidence" / "events.jsonl"
    event_path.write_text(
        event_path.read_text(encoding="utf-8") + " \n",
        encoding="utf-8",
    )

    _assert_refused(fixture.rule, match="incident failed integrity")


def test_integrity_11_modified_evidence_statistics_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    original = _read_evidence(fixture.evidence_path)
    statistics = original.statistics.model_copy(
        update={"mean_rate_hz": original.statistics.mean_rate_hz + 1.0}
    )
    evidence = _rehash_evidence(original, statistics=statistics)
    _write_evidence(fixture.evidence_path, evidence)
    rule = _rule_with_derivation(
        fixture.rule,
        healthy_evidence_fingerprint=evidence.evidence_fingerprint,
    )

    _assert_refused(rule, match="healthy statistics")


@pytest.mark.parametrize(
    ("case_id", "field", "value"),
    [
        ("12_minimum_rate", "minimum_rate_hz", 14.0),
        ("13_freshness_timeout", "stale_timeout_sec", 0.20),
        ("14_startup_grace", "startup_grace_sec", 0.75),
    ],
)
def test_integrity_modified_threshold_fails_closed(
    tmp_path, case_id, field, value
):
    fixture = build_telemetry_provenance_fixture(tmp_path / case_id)
    rule = _rule_with_params(fixture.rule, **{field: value})

    _assert_refused(rule, match="contract thresholds")


def test_integrity_15_modified_qos_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _rule_with_params(
        fixture.rule,
        expected_qos={
            **fixture.contract.expected_qos,
            "reliability": "best_effort",
        },
    )

    _assert_refused(rule, match="QoS")


def test_integrity_16_modified_type_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _rule_with_params(
        fixture.rule, expected_type="std_msgs/msg/String"
    )

    _assert_refused(rule, match="identity")


def test_integrity_17_modified_declared_context_label_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    rule = _rule_with_params(
        fixture.rule, declared_context_label="different_runtime_context"
    )

    _assert_refused(rule, match="context")


def test_integrity_21_selected_thresholds_differ_from_rule_fails_closed(
    tmp_path,
):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    selected = {
        **fixture.rule.derivation["selected_thresholds"],
        "minimum_rate_hz": 14.0,
    }
    rule = _rule_with_derivation(
        fixture.rule, selected_thresholds=selected
    )

    _assert_refused(rule, match="selected provenance")


def test_integrity_23_evidence_regenerated_after_approval_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    original = _read_evidence(fixture.evidence_path)
    regenerated = _rehash_evidence(
        original,
        evidence_id="evh_regenerated1",
        confidence_bounds={
            **original.confidence_bounds,
            "regenerated": True,
        },
    )
    _write_evidence(fixture.evidence_path, regenerated)

    _assert_refused(fixture.rule, match="fingerprint differs from rule")


# Trusted local approval: exact pinning, not a signature or identity model.


def test_trusted_approval_18_recomputed_untrusted_fingerprint_fails_closed(
    tmp_path,
):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    attacker_rule = _rule_with_params(fixture.rule, minimum_rate_hz=1.0)

    _assert_refused(
        attacker_rule,
        trusted_fingerprint=fixture.rule.rule_fingerprint,
        match="trusted local allowlist",
    )


def test_trusted_approval_19_untrusted_approver_cannot_replace_local_pin(
    tmp_path,
):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    untrusted_artifact = _approve(
        fixture.rule,
        rationale="untrusted party claims this artifact is approved",
    )

    _assert_refused(
        untrusted_artifact,
        trusted_fingerprint=fixture.rule.rule_fingerprint,
        match="trusted local allowlist",
    )


def test_trusted_approval_20_pin_for_different_rule_fails_closed(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path)
    different_rule = _approve(
        fixture.rule,
        rationale="different locally constructed rule",
    )

    _assert_refused(
        fixture.rule,
        trusted_fingerprint=different_rule.rule_fingerprint,
        match="trusted local allowlist",
    )
