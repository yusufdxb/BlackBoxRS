"""Adversarial tests for the framed rosbag2 manifest-v2 identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import blackboxrs.prevention.bag_manifest as bag_manifest_module
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.prevention.bag_manifest import (
    BAG_MANIFEST_SCHEMA,
    build_bag_manifest,
    canonical_manifest_bytes,
    compute_manifest_sha256,
)
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_telemetry_health_rule,
)
from blackboxrs.prevention.telemetry_health import (
    HistoricalTelemetryHealthEvidenceV1,
    TelemetryHealthEvidence,
    compute_evidence_fingerprint,
)
from tests.telemetry_fixtures import build_telemetry_provenance_fixture


def _write_bag(
    root: Path,
    payloads: dict[str, bytes],
    *,
    listed_paths: list[str] | None = None,
    storage_identifier: str = "sqlite3",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    paths = listed_paths if listed_paths is not None else list(payloads)
    metadata = (
        "rosbag2_bagfile_information:\n"
        "  version: 8\n"
        f"  storage_identifier: {storage_identifier}\n"
        "  relative_file_paths:\n"
        + "".join(f"    - {path}\n" for path in paths)
    )
    (root / "metadata.yaml").write_text(metadata, encoding="utf-8")
    for relative_path, content in payloads.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def _v1_ambiguous_hash(bag: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in bag.rglob("*") if path.is_file()),
        key=lambda path: path.name,
    )
    for path in files:
        digest.update(path.relative_to(bag).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_reviewer_split_file_collision_is_rejected(tmp_path):
    original = _write_bag(
        tmp_path / "original",
        {
            "healthy_0.db3": (
                b"SQLite format 3\0deterministic telemetry provenance fixture\n"
            )
        },
    )
    substituted = _write_bag(
        tmp_path / "substituted",
        {
            "healthy_0.db3": b"SQL",
            "ite format 3": b"deterministic telemetry provenance fixture\n",
        },
        listed_paths=["healthy_0.db3"],
    )

    assert _v1_ambiguous_hash(original) == _v1_ambiguous_hash(substituted)
    with pytest.raises(ValueError, match="unexpected files"):
        build_bag_manifest(substituted)


def test_file_merge_changes_manifest_identity(tmp_path):
    split = _write_bag(
        tmp_path / "split",
        {"a.db3": b"left", "b.db3": b"right"},
    )
    merged = _write_bag(
        tmp_path / "merged",
        {"a.db3": b"leftb.db3\0right"},
    )

    assert compute_manifest_sha256(build_bag_manifest(split)) != (
        compute_manifest_sha256(build_bag_manifest(merged))
    )


def test_payload_records_are_sorted_and_metadata_reorder_changes_identity(tmp_path):
    first = _write_bag(
        tmp_path / "first",
        {"a.db3": b"a", "b.db3": b"b"},
        listed_paths=["b.db3", "a.db3"],
    )
    second = _write_bag(
        tmp_path / "second",
        {"a.db3": b"a", "b.db3": b"b"},
        listed_paths=["a.db3", "b.db3"],
    )

    first_manifest = build_bag_manifest(first)
    second_manifest = build_bag_manifest(second)
    assert [record.path for record in first_manifest.payloads] == [
        "a.db3",
        "b.db3",
    ]
    assert compute_manifest_sha256(first_manifest) != compute_manifest_sha256(
        second_manifest
    )


def test_added_unexpected_file_is_rejected(tmp_path):
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"payload"})
    (bag / "extra.bin").write_bytes(b"unexpected")

    with pytest.raises(ValueError, match="unexpected files"):
        build_bag_manifest(bag)


def test_removed_payload_is_rejected(tmp_path):
    bag = _write_bag(tmp_path / "bag", {}, listed_paths=["bag_0.db3"])

    with pytest.raises(ValueError, match="missing payload"):
        build_bag_manifest(bag)


def test_renamed_payload_is_rejected(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {"renamed.db3": b"payload"},
        listed_paths=["bag_0.db3"],
    )

    with pytest.raises(ValueError, match="missing payload"):
        build_bag_manifest(bag)


def test_duplicate_normalized_metadata_path_is_rejected(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {"bag_0.db3": b"payload"},
        listed_paths=["bag_0.db3", "./bag_0.db3"],
    )

    with pytest.raises(ValueError, match="duplicate normalized paths"):
        build_bag_manifest(bag)


def test_symlink_payload_substitution_is_rejected(tmp_path):
    outside = tmp_path / "outside.db3"
    outside.write_bytes(b"outside")
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"payload"})
    (bag / "bag_0.db3").unlink()
    (bag / "bag_0.db3").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        build_bag_manifest(bag)


def test_root_symlink_substitution_while_hashing_is_rejected(
    tmp_path, monkeypatch
):
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"reviewed"})
    replacement = _write_bag(
        tmp_path / "replacement", {"bag_0.db3": b"substituted"}
    )
    moved = tmp_path / "reviewed-moved"
    original_hash_fd = bag_manifest_module._hash_fd
    swapped = False

    def swap_root_after_payload_read(file_fd: int, *, collect_bytes: bool):
        nonlocal swapped
        result = original_hash_fd(file_fd, collect_bytes=collect_bytes)
        if not collect_bytes and not swapped:
            bag.rename(moved)
            bag.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(
        bag_manifest_module, "_hash_fd", swap_root_after_payload_read
    )

    with pytest.raises(ValueError, match="root changed while hashing"):
        build_bag_manifest(bag)


def test_root_swap_between_lstat_and_open_is_rejected(tmp_path, monkeypatch):
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"reviewed"})
    replacement = _write_bag(
        tmp_path / "replacement", {"bag_0.db3": b"substituted"}
    )
    moved = tmp_path / "reviewed-moved"
    original_open_root = bag_manifest_module._open_bag_root

    def swap_before_open(path: Path, expected: os.stat_result):
        bag.rename(moved)
        bag.symlink_to(replacement, target_is_directory=True)
        return original_open_root(path, expected)

    monkeypatch.setattr(
        bag_manifest_module, "_open_bag_root", swap_before_open
    )

    with pytest.raises(ValueError, match="without following links|root changed"):
        build_bag_manifest(bag)


def test_metadata_naming_missing_payload_is_rejected(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {"bag_0.db3": b"payload"},
        listed_paths=["bag_0.db3", "bag_1.db3"],
    )

    with pytest.raises(ValueError, match="missing payload"):
        build_bag_manifest(bag)


def test_metadata_storage_payload_mismatch_is_rejected(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {"bag_0.mcap": b"payload"},
        storage_identifier="sqlite3",
    )

    with pytest.raises(ValueError, match="does not match storage identifier"):
        build_bag_manifest(bag)


def test_file_change_while_hashing_is_rejected(tmp_path, monkeypatch):
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"x" * 1024})
    payload = bag / "bag_0.db3"
    original_hash_fd = bag_manifest_module._hash_fd
    changed = False

    def mutate_after_read(file_fd: int, *, collect_bytes: bool):
        nonlocal changed
        result = original_hash_fd(file_fd, collect_bytes=collect_bytes)
        if not collect_bytes and not changed:
            with open(payload, "ab") as output:
                output.write(b"changed")
                output.flush()
                os.fsync(output.fileno())
            changed = True
        return result

    monkeypatch.setattr(bag_manifest_module, "_hash_fd", mutate_after_read)

    with pytest.raises(ValueError, match="changed while hashing"):
        build_bag_manifest(bag)


def test_genuine_style_sqlite_fixture_has_complete_manifest(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {
            "extended_5min_0.db3": b"SQLite format 3\0fixture",
            "extended_5min_1.db3": b"SQLite format 3\0fixture-two",
        },
    )

    manifest = build_bag_manifest(bag)

    assert manifest.manifest_schema == BAG_MANIFEST_SCHEMA
    assert manifest.storage_identifier == "sqlite3"
    assert manifest.total_size == sum(
        path.stat().st_size for path in bag.iterdir()
    )
    assert [record.role for record in manifest.payloads] == [
        "storage_payload",
        "storage_payload",
    ]
    assert all(record.sha256 for record in manifest.payloads)


def test_canonical_manifest_is_deterministic_across_repeated_runs(tmp_path):
    bag = _write_bag(
        tmp_path / "bag",
        {"b.db3": b"b", "a.db3": b"a"},
        listed_paths=["b.db3", "a.db3"],
    )

    first = build_bag_manifest(bag)
    second = build_bag_manifest(bag)

    assert canonical_manifest_bytes(first) == canonical_manifest_bytes(second)
    assert compute_manifest_sha256(first) == compute_manifest_sha256(second)


def test_v1_artifact_is_readable_but_refused_for_trusted_adoption(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path / "fixture")
    current = TelemetryHealthEvidence.model_validate_json(
        fixture.evidence_path.read_text(encoding="utf-8")
    )
    historical = HistoricalTelemetryHealthEvidenceV1(
        schema_version="telemetry-health-evidence-v1",
        evidence_id=current.evidence_id,
        source_bag_path=current.source_bag_path,
        source_bag_sha256="0" * 64,
        metadata_sha256=current.metadata_sha256,
        source_bag_size_bytes=current.source_bag_size_bytes,
        source_bag_duration_sec=current.source_bag_duration_sec,
        source_bag_message_count=current.source_bag_message_count,
        topic=current.topic,
        message_type=current.message_type,
        offered_qos=current.offered_qos,
        graph_context=current.declared_context_label,
        statistics=current.statistics,
        thresholds=current.thresholds,
        derivation_method=current.derivation_method,
        confidence_bounds=current.confidence_bounds,
    )
    historical = historical.model_copy(
        update={
            "evidence_fingerprint": compute_evidence_fingerprint(historical)
        }
    )
    historical_path = tmp_path / "historical-v1.json"
    historical_path.write_text(
        json.dumps(historical.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        PreventionDerivationError,
        match="readable but requires explicit migration",
    ):
        derive_telemetry_health_rule(
            BundleReader(fixture.bundle_path),
            historical_path,
        )


@pytest.mark.parametrize("payload_path", ["/absolute.db3", "../escape.db3"])
def test_absolute_and_parent_traversal_paths_are_rejected(
    tmp_path, payload_path
):
    bag = _write_bag(
        tmp_path / "bag",
        {},
        listed_paths=[payload_path],
    )

    with pytest.raises(ValueError, match="absolute|traversal"):
        build_bag_manifest(bag)


def test_fifo_is_rejected(tmp_path):
    bag = _write_bag(tmp_path / "bag", {"bag_0.db3": b"payload"})
    fifo = bag / "unexpected.fifo"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="non-regular"):
        build_bag_manifest(bag)
