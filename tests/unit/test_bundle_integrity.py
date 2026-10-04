"""Integrity manifest, finalization, and validation tests."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from blackboxrs.cli.incident_cmd import incident_group, prevention_group
from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident, render_report
from blackboxrs.incident.bundle import BundleWriter, validate_bundle_path
from blackboxrs.incident.builder import IncidentBuilder
from blackboxrs.incident.integrity import MANIFEST_PATH, ValidationIssue
from blackboxrs.incident.models import Incident


_START = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _ev(t, source, event_type, data, severity="info", metadata=None):
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "sess_integrity"},
    }


def _seed_dead_topic_log(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    events = [
        _ev(_START, "ros_monitor", "ros.frequency", {"topic": "/scan"}),
        _ev(
            _START + timedelta(seconds=5),
            "anomaly_engine",
            "anomaly.dead_topic",
            {
                "detector": "DeadTopicDetector",
                "topic": "/scan",
                "metric": "/scan",
                "value": 5.0,
                "threshold": 3.0,
                "message": "Topic /scan stopped emitting messages.",
            },
            severity="error",
            metadata={
                "session_id": "sess_integrity",
                "detector_class": (
                    "blackboxrs.anomaly_engine.detectors.dead_topic."
                    "DeadTopicDetector"
                ),
                "signature_fields": ["topic"],
            },
        ),
    ]
    target = log_dir / "blackboxrs_20260507_120000_000000.jsonl"
    with open(target, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")


def _build_bundle(tmp_path: Path) -> Path:
    log_dir = tmp_path / "logs"
    _seed_dead_topic_log(log_dir)
    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)
    return build_incident(
        _START,
        _START + timedelta(seconds=10),
        config=cfg,
        incidents_dir=tmp_path / "incidents",
    )


def _load_manifest(bundle: Path) -> dict:
    return json.loads((bundle / MANIFEST_PATH).read_text(encoding="utf-8"))


def _write_manifest(bundle: Path, data: dict) -> None:
    (bundle / MANIFEST_PATH).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_valid_finalized_bundle_verifies(tmp_path: Path):
    bundle = _build_bundle(tmp_path)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "valid_finalized"
    assert result.errors == []
    assert (bundle / MANIFEST_PATH).exists()


def test_incident_verify_cli_reports_valid_and_json(tmp_path: Path):
    bundle = _build_bundle(tmp_path)

    human = CliRunner().invoke(incident_group, ["verify", str(bundle)])
    machine = CliRunner().invoke(incident_group, ["verify", str(bundle), "--json"])

    assert human.exit_code == 0
    assert "valid_finalized" in human.output
    assert machine.exit_code == 0
    assert json.loads(machine.output)["state"] == "valid_finalized"


def test_modified_file_causes_checksum_failure(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "timeline.json").write_text("[]\n", encoding="utf-8")

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "checksum_mismatch" for issue in result.errors)


def test_missing_required_file_fails(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "fingerprint.json").unlink()

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "incomplete"
    assert any(issue.path == "fingerprint.json" for issue in result.errors)


def test_truncated_manifest_fails(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / MANIFEST_PATH).write_text("{", encoding="utf-8")

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "malformed_manifest" for issue in result.errors)


def test_path_traversal_entry_is_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    manifest = _load_manifest(bundle)
    manifest["files"].append(
        {
            "path": "../outside.json",
            "size_bytes": 1,
            "sha256": "0" * 64,
            "required": False,
        }
    )
    _write_manifest(bundle, manifest)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "unsafe_manifest_path" for issue in result.errors)


def test_duplicate_manifest_entry_is_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    manifest = _load_manifest(bundle)
    manifest["files"].append(dict(manifest["files"][0]))
    _write_manifest(bundle, manifest)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "duplicate_manifest_entry" for issue in result.errors)


def test_duplicate_canonical_manifest_entry_is_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    manifest = _load_manifest(bundle)
    original = next(
        entry for entry in manifest["files"] if entry["path"] == "evidence/events.jsonl"
    )
    duplicate = dict(original)
    duplicate["path"] = "evidence//events.jsonl"
    manifest["files"].append(duplicate)
    _write_manifest(bundle, manifest)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "unsafe_manifest_path" for issue in result.errors)


def test_empty_required_evidence_stream_is_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "evidence" / "events.jsonl").write_text("", encoding="utf-8")
    manifest = _load_manifest(bundle)
    for entry in manifest["files"]:
        if entry["path"] == "evidence/events.jsonl":
            entry["size_bytes"] = 0
            entry["sha256"] = hashlib.sha256(b"").hexdigest()
    _write_manifest(bundle, manifest)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "corrupted"
    assert any(issue.code == "empty_required_file" for issue in result.errors)


def test_unsupported_manifest_version_is_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    manifest = _load_manifest(bundle)
    manifest["bundle_format_version"] = "999.0"
    _write_manifest(bundle, manifest)

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "unsupported_version"
    assert any(issue.code == "unsupported_bundle_format" for issue in result.errors)


def test_unexpected_extra_file_warns_but_does_not_invalidate(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "notes.tmp").write_text("operator scratch\n", encoding="utf-8")

    result = validate_bundle_path(bundle, require_finalized=True)

    assert result.state == "valid_finalized"
    assert not result.errors
    assert any(issue.code == "unexpected_file" for issue in result.warnings)


def test_finalized_bundle_refuses_late_attachment(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    attachment = tmp_path / "operator_note.txt"
    attachment.write_text("late note\n", encoding="utf-8")

    result = CliRunner().invoke(incident_group, ["attach", str(bundle), str(attachment)])

    assert result.exit_code == 1
    assert "Cannot attach to a finalized bundle" in result.output
    assert not (bundle / "attachments" / attachment.name).exists()


def test_interrupted_staging_bundle_is_not_finalized(tmp_path: Path):
    staging = tmp_path / "incidents" / ".inc_x.staging.test"
    staging.mkdir(parents=True)
    (staging / "incident.json").write_text("{}", encoding="utf-8")

    result = validate_bundle_path(staging, require_finalized=True)

    assert result.state == "incomplete"
    assert any(issue.code == "missing_manifest" for issue in result.errors)


def test_corrupted_bundle_cannot_be_used_for_prevention_adoption(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "evidence" / "triggers.json").write_text("[]\n", encoding="utf-8")

    result = CliRunner().invoke(
        prevention_group,
        ["adopt", "--from-incident", str(bundle), "--rules-dir", str(tmp_path / "rules")],
    )

    assert result.exit_code == 1
    assert "integrity validation" in result.output


def test_corrupted_bundle_cannot_be_replayed(tmp_path: Path):
    bundle = _build_bundle(tmp_path)
    (bundle / "evidence" / "events.jsonl").write_text("not-json\n", encoding="utf-8")

    result = CliRunner().invoke(incident_group, ["replay", str(bundle)])

    assert result.exit_code == 1
    assert "checksum_mismatch" in result.output


def test_legacy_bundle_behavior_is_explicit_and_readable(tmp_path: Path):
    bundle = tmp_path / "legacy"
    writer = BundleWriter(bundle)
    now = _START
    writer.write_incident(
        Incident(
            incident_id="inc_2026-05-07T12-00-00_0badc0de",
            created_at=now,
            window_start=now,
            window_end=now,
            session_id="s",
            title="legacy",
            bundle_path=str(bundle),
        )
    )
    writer.write_events_jsonl([])
    writer.write_triggers([])
    writer.write_snapshots([])
    writer.write_signatures(
        _minimal_signature("config"),
        _minimal_signature("version"),
    )
    writer.write_timeline([])
    from blackboxrs.incident.models import FailureFingerprint

    writer.write_fingerprint(FailureFingerprint(fingerprint_id="fpr_" + "a" * 16))
    writer.write_report("# legacy\n")

    result = validate_bundle_path(bundle)

    assert result.state == "legacy"
    assert not result.errors
    assert any(issue.code == "missing_manifest" for issue in result.warnings)
    assert render_report(bundle).startswith("> **Integrity warning:**")

    cli = CliRunner().invoke(incident_group, ["verify", str(bundle)])
    assert cli.exit_code == 2
    assert "legacy" in cli.output


def test_failed_finalization_preserves_staging_and_hides_final_dir(
    tmp_path: Path, monkeypatch
):
    log_dir = tmp_path / "logs"
    _seed_dead_topic_log(log_dir)
    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)
    builder = IncidentBuilder(config=cfg, incidents_dir=tmp_path / "incidents")

    def _bad_validate(self, *, require_finalized=False):
        return SimpleNamespace(
            errors=[ValidationIssue(code="forced", message="forced failure")]
        )

    monkeypatch.setattr(BundleWriter, "validate", _bad_validate)

    try:
        builder.build(_START, _START + timedelta(seconds=10))
    except RuntimeError as exc:
        assert "staging preserved" in str(exc)
    else:
        raise AssertionError("build should fail")

    final_dirs = [p for p in (tmp_path / "incidents").iterdir() if not p.name.startswith(".")]
    staging_dirs = [p for p in (tmp_path / "incidents").iterdir() if ".staging." in p.name]
    assert final_dirs == []
    assert len(staging_dirs) == 1
    assert (staging_dirs[0] / MANIFEST_PATH).exists()


def _minimal_signature(kind: str):
    from blackboxrs.incident.models import ConfigSignature, VersionSignature

    cls = ConfigSignature if kind == "config" else VersionSignature
    return cls(t=_START, hash="0" * 64, payload={})
