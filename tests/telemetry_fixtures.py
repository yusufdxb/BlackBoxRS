"""Real deterministic telemetry provenance fixtures shared by test suites.

The fixture follows the production path: it writes and finalizes an incident
bundle, writes a source bag and content-addressed evidence document, derives a
telemetry rule, persists and reloads it, then exercises runtime provenance
verification.  Central provenance checks are never mocked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.incident.builder import _promote_trigger
from blackboxrs.incident.bundle import BundleReader, BundleWriter
from blackboxrs.incident.fingerprint import compute as compute_incident_fingerprint
from blackboxrs.incident.models import (
    ConfigSignature,
    DetectorTrigger,
    Incident,
    LikelyCauseHypothesis,
    TimelineEvent,
    VersionSignature,
)
from blackboxrs.prevention.derivation import derive_telemetry_health_rule
from blackboxrs.prevention.bag_manifest import (
    build_bag_manifest,
    compute_manifest_sha256,
)
from blackboxrs.prevention.rules import PreventionRule, load_rule, save_rule
from blackboxrs.prevention.telemetry_health import (
    HealthyTelemetryStatistics,
    TelemetryHealthContract,
    TelemetryHealthEvidence,
    compute_evidence_fingerprint,
    contract_from_rule,
    derive_thresholds,
)


TOPIC = "/utlidar/robot_pose"
MESSAGE_TYPE = "geometry_msgs/msg/PoseStamped"
GRAPH_CONTEXT = "go2_utlidar_hardware_eval_20260406"
OFFERED_QOS = {
    "history": "keep_last",
    "depth": 1,
    "reliability": "reliable",
    "durability": "volatile",
}


@dataclass(frozen=True)
class TelemetryProvenanceFixture:
    """Paths and validated models produced by the real provenance path."""

    root: Path
    bundle_path: Path
    bag_path: Path
    evidence_path: Path
    rules_dir: Path
    rule_path: Path
    event: BlackBoxEvent
    trigger: DetectorTrigger
    incident: Incident
    rule: PreventionRule
    contract: TelemetryHealthContract


def healthy_statistics(
    *, message_count: int = 6177
) -> HealthyTelemetryStatistics:
    """Return deterministic healthy statistics selecting the 15 Hz rule."""
    return HealthyTelemetryStatistics(
        message_count=message_count,
        startup_delay_sec=0.158142077,
        observed_duration_sec=329.443225884,
        mean_rate_hz=18.746780976988813,
        median_rate_hz=18.756079021048965,
        inter_arrival_sec={
            "minimum": 0.045329218,
            "mean": 0.053342491237694294,
            "median": 0.0533160475,
            "p90": 0.0544107025,
            "p95": 0.0559826245,
            "p99": 0.05693549725,
            "p99_5": 0.057507992125,
            "p99_9": 0.06031294775,
            "max": 0.070847572,
        },
        rolling_rate_hz={"2s": {"minimum": 18.5}},
        header_nonprogressing_deltas=0,
        header_frozen_deltas=0,
        header_negative_deltas=0,
        payload_nonfinite_values=0,
        consecutive_exact_pose_repeats=0,
        unique_pose_vectors=6177,
    )


def write_evidence(
    root: Path,
    *,
    message_count: int = 6177,
    topic: str = TOPIC,
    message_type: str = MESSAGE_TYPE,
    declared_context_label: str = GRAPH_CONTEXT,
    offered_qos: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """Write a minimal deterministic source bag and evidence document."""
    root = Path(root)
    bag_path = root / "healthy_bag"
    bag_path.mkdir(parents=True, exist_ok=True)
    metadata = bag_path / "metadata.yaml"
    metadata.write_text(
        "rosbag2_bagfile_information:\n"
        "  version: 8\n"
        "  storage_identifier: sqlite3\n"
        "  relative_file_paths:\n"
        "    - healthy_0.db3\n",
        encoding="utf-8",
    )
    (bag_path / "healthy_0.db3").write_bytes(
        b"SQLite format 3\0deterministic telemetry provenance fixture\n"
    )

    stats = healthy_statistics(message_count=message_count)
    bag_manifest = build_bag_manifest(bag_path)
    evidence = TelemetryHealthEvidence(
        schema_version="telemetry-health-evidence-v2",
        evidence_id="evh_123456789abc",
        source_bag_path=str(bag_path.resolve()),
        source_bag_manifest_sha256=compute_manifest_sha256(bag_manifest),
        source_bag_manifest=bag_manifest,
        metadata_sha256=bag_manifest.metadata.sha256,
        source_bag_size_bytes=bag_manifest.total_size,
        source_bag_duration_sec=329.6,
        source_bag_message_count=94_325,
        topic=topic,
        message_type=message_type,
        offered_qos=dict(offered_qos or OFFERED_QOS),
        declared_context_label=declared_context_label,
        statistics=stats,
        thresholds=derive_thresholds(stats),
        derivation_method={
            "method": "dead_topic_telemetry_health_v2",
            "source": "deterministic_test_fixture",
        },
        confidence_bounds={"descriptive_only": True},
    )
    evidence = evidence.model_copy(
        update={"evidence_fingerprint": compute_evidence_fingerprint(evidence)}
    )
    evidence_path = root / "healthy_evidence.json"
    evidence_path.write_text(
        json.dumps(
            evidence.model_dump(mode="json", by_alias=True),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return evidence_path, bag_path


def write_finalized_incident(
    root: Path,
    *,
    topic: str = TOPIC,
) -> tuple[Path, BlackBoxEvent, DetectorTrigger, Incident]:
    """Write a valid finalized incident with a real source-event reference."""
    root = Path(root)
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    event = BlackBoxEvent(
        timestamp=now,
        source="anomaly_engine",
        event_type="anomaly.dead_topic",
        severity="error",
        data={
            "detector": "dead_topic",
            "topic": topic,
            "message": f"Topic {topic} stopped emitting messages.",
        },
        metadata={
            "detector_class": (
                "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector"
            ),
            "signature_fields": ["topic"],
            "target_subsystem": "ros",
            "session_id": "telemetry-provenance-test",
        },
    )
    trigger = _promote_trigger(event, 1)
    fingerprint = compute_incident_fingerprint([trigger])
    bundle_path = root / "incident"
    incident = Incident(
        incident_id="inc_2026-07-23T00-00-00_12345678",
        created_at=now,
        window_start=now,
        window_end=now,
        session_id="telemetry-provenance-test",
        host="fixture-host",
        severity="error",
        title="dead pose telemetry",
        summary=f"Topic {topic} stopped emitting messages.",
        bundle_path=str(bundle_path.resolve()),
        triggers=[trigger.trigger_id],
        fingerprint=fingerprint,
        likely_causes=[
            LikelyCauseHypothesis(
                cause=f"Topic {topic} stopped emitting messages.",
                confidence=0.98,
                evidence_refs=[
                    trigger.source_event_ref or "",
                    f"triggers.json#{trigger.trigger_id}",
                ],
            )
        ],
    )
    writer = BundleWriter(bundle_path)
    writer.write_events_jsonl([event])
    writer.write_triggers([trigger])
    writer.write_snapshots([])
    writer.write_signatures(
        ConfigSignature(t=now, hash="1" * 64, payload={"fixture": "config"}),
        VersionSignature(t=now, hash="2" * 64, payload={"fixture": "version"}),
    )
    writer.write_timeline(
        [
            TimelineEvent(
                t=now,
                kind="trigger",
                subsystem="ros",
                summary=trigger.message,
                confidence=1.0,
                evidence_ref=trigger.source_event_ref or "",
                data={"trigger_id": trigger.trigger_id},
            )
        ]
    )
    writer.write_fingerprint(fingerprint)
    writer.write_incident(incident)
    writer.write_report("# Deterministic telemetry provenance incident\n")
    writer.write_manifest(
        writer.build_manifest(incident_id=incident.incident_id, created_at=now)
    )
    assert writer.validate(require_finalized=True).ok
    return bundle_path, event, trigger, incident


def build_telemetry_provenance_fixture(
    root: Path,
    *,
    message_count: int = 6177,
) -> TelemetryProvenanceFixture:
    """Build and verify the complete adoption-to-runtime provenance path."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bundle_path, event, trigger, incident = write_finalized_incident(root)
    evidence_path, bag_path = write_evidence(root, message_count=message_count)
    reader = BundleReader(bundle_path)
    derivation = derive_telemetry_health_rule(reader, evidence_path)
    rules_dir = root / "rules"
    rule_path = save_rule(derivation.rule, rules_dir)
    rule = load_rule(rule_path)
    assert rule.rule_fingerprint is not None
    contract = contract_from_rule(
        rule, trusted_rule_fingerprint=rule.rule_fingerprint
    )
    return TelemetryProvenanceFixture(
        root=root,
        bundle_path=bundle_path,
        bag_path=bag_path,
        evidence_path=evidence_path,
        rules_dir=rules_dir,
        rule_path=rule_path,
        event=event,
        trigger=trigger,
        incident=incident,
        rule=rule,
        contract=contract,
    )
