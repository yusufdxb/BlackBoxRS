"""Canonical, versioned integrity manifests for rosbag2 directories."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


BAG_MANIFEST_SCHEMA = "blackboxrs-bag-manifest-v2"
_METADATA_PATH = "metadata.yaml"
_CHUNK_SIZE = 1024 * 1024


class BagFileRecord(BaseModel):
    """One regular file named by a canonical bag manifest."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size: int = Field(..., ge=0)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    role: Literal["metadata", "storage_payload"]
    storage_identifier: str
    metadata_relationship: str


class BagManifest(BaseModel):
    """Portable bag identity built from framed per-file records."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    manifest_schema: Literal["blackboxrs-bag-manifest-v2"] = Field(
        default=BAG_MANIFEST_SCHEMA,
        alias="schema",
        serialization_alias="schema",
    )
    storage_identifier: str
    metadata: BagFileRecord
    payloads: list[BagFileRecord]
    total_size: int = Field(..., gt=0)

    @model_validator(mode="after")
    def _validate_manifest(self) -> "BagManifest":
        if self.metadata.path != _METADATA_PATH or self.metadata.role != "metadata":
            raise ValueError("bag manifest metadata record is invalid")
        payload_paths = [record.path for record in self.payloads]
        if payload_paths != sorted(payload_paths):
            raise ValueError("bag manifest payloads are not canonically ordered")
        all_paths = [self.metadata.path, *payload_paths]
        if len(all_paths) != len(set(all_paths)):
            raise ValueError("bag manifest contains duplicate normalized paths")
        if any(record.role != "storage_payload" for record in self.payloads):
            raise ValueError("bag manifest contains a non-payload payload record")
        if any(
            record.storage_identifier != self.storage_identifier
            for record in [self.metadata, *self.payloads]
        ):
            raise ValueError("bag manifest storage identifiers are inconsistent")
        expected_total = sum(
            record.size for record in [self.metadata, *self.payloads]
        )
        if self.total_size != expected_total:
            raise ValueError("bag manifest total size is inconsistent")
        return self


def canonical_manifest_bytes(manifest: BagManifest) -> bytes:
    """Serialize a manifest using the one supported canonical JSON form."""
    return json.dumps(
        manifest.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def compute_manifest_sha256(manifest: BagManifest) -> str:
    """Return the digest of the canonical manifest, not concatenated bag bytes."""
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def build_bag_manifest(bag_path: Path) -> BagManifest:
    """Build a v2 manifest while rejecting ambiguous or unstable bag layouts."""
    bag = Path(bag_path)
    try:
        root_stat = bag.lstat()
    except OSError as exc:
        raise ValueError(f"Telemetry source bag is unavailable: {bag}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Telemetry source bag must be a non-symlink directory")

    initial_inventory = _inventory(bag)
    if _METADATA_PATH not in initial_inventory:
        raise ValueError("Telemetry source bag metadata.yaml is unavailable")

    metadata_bytes, metadata_size, metadata_hash = _stable_file_digest(
        bag, _METADATA_PATH, collect_bytes=True
    )
    assert metadata_bytes is not None
    storage_identifier, listed_payloads = _parse_metadata(metadata_bytes)

    expected_paths = {_METADATA_PATH, *listed_payloads}
    actual_paths = set(initial_inventory)
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)
    if missing:
        raise ValueError(
            "Telemetry metadata names missing payload files: " + ", ".join(missing)
        )
    if unexpected:
        raise ValueError(
            "Telemetry source bag contains unexpected files: "
            + ", ".join(unexpected)
        )

    payload_records: list[BagFileRecord] = []
    for relative_path in sorted(listed_payloads):
        _validate_payload_extension(relative_path, storage_identifier)
        _bytes, size, digest = _stable_file_digest(
            bag, relative_path, collect_bytes=False
        )
        payload_records.append(
            BagFileRecord(
                path=relative_path,
                size=size,
                sha256=digest,
                role="storage_payload",
                storage_identifier=storage_identifier,
                metadata_relationship=(
                    "rosbag2_bagfile_information.relative_file_paths"
                ),
            )
        )

    final_inventory = _inventory(bag)
    if _inventory_identity(initial_inventory) != _inventory_identity(final_inventory):
        raise ValueError("Telemetry source bag layout changed while hashing")

    metadata_record = BagFileRecord(
        path=_METADATA_PATH,
        size=metadata_size,
        sha256=metadata_hash,
        role="metadata",
        storage_identifier=storage_identifier,
        metadata_relationship="rosbag2_bagfile_information",
    )
    return BagManifest(
        storage_identifier=storage_identifier,
        metadata=metadata_record,
        payloads=payload_records,
        total_size=metadata_size + sum(record.size for record in payload_records),
    )


def verify_bag_manifest(bag_path: Path, expected: BagManifest) -> None:
    """Rebuild and compare every canonical manifest field."""
    actual = build_bag_manifest(bag_path)
    if actual != expected:
        raise ValueError("Telemetry source bag manifest mismatch")


def _normalise_relative_path(raw_path: object) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Telemetry metadata contains an invalid payload path")
    if "\\" in raw_path:
        raise ValueError("Telemetry payload paths must use POSIX separators")
    if PurePosixPath(raw_path).is_absolute():
        raise ValueError("Telemetry metadata contains an absolute payload path")
    raw_parts = raw_path.split("/")
    if any(part == ".." for part in raw_parts):
        raise ValueError("Telemetry metadata contains parent traversal")
    normalised = unicodedata.normalize("NFC", posixpath.normpath(raw_path))
    if normalised in {"", "."} or normalised.startswith("../"):
        raise ValueError("Telemetry metadata contains an invalid payload path")
    if PurePosixPath(normalised).is_absolute():
        raise ValueError("Telemetry metadata contains an absolute payload path")
    return normalised


def _parse_metadata(metadata_bytes: bytes) -> tuple[str, list[str]]:
    try:
        document = yaml.safe_load(metadata_bytes)
        info = document["rosbag2_bagfile_information"]
        storage_identifier = info["storage_identifier"]
        raw_paths = info["relative_file_paths"]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise ValueError("Telemetry metadata.yaml is malformed") from exc
    if not isinstance(storage_identifier, str) or not storage_identifier:
        raise ValueError("Telemetry metadata has no storage identifier")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("Telemetry metadata has no payload file list")
    paths = [_normalise_relative_path(raw_path) for raw_path in raw_paths]
    if len(paths) != len(set(paths)):
        raise ValueError("Telemetry metadata contains duplicate normalized paths")
    if _METADATA_PATH in paths:
        raise ValueError("Telemetry metadata lists metadata.yaml as a payload")
    return storage_identifier, paths


def _validate_payload_extension(relative_path: str, storage_identifier: str) -> None:
    expected_extensions = {
        "sqlite3": ".db3",
        "mcap": ".mcap",
    }
    expected = expected_extensions.get(storage_identifier)
    if expected is not None and Path(relative_path).suffix.lower() != expected:
        raise ValueError(
            f"Telemetry payload {relative_path!r} does not match "
            f"storage identifier {storage_identifier!r}"
        )


def _inventory(bag: Path) -> dict[str, os.stat_result]:
    inventory: dict[str, os.stat_result] = {}
    for directory, directory_names, file_names in os.walk(
        bag, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in list(directory_names):
            child = directory_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"Telemetry source bag contains symlink: {child}")
            if not stat.S_ISDIR(child_stat.st_mode):
                raise ValueError(
                    f"Telemetry source bag contains non-directory entry: {child}"
                )
        for name in file_names:
            child = directory_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"Telemetry source bag contains symlink: {child}")
            if not stat.S_ISREG(child_stat.st_mode):
                raise ValueError(
                    f"Telemetry source bag contains non-regular file: {child}"
                )
            relative = _normalise_relative_path(
                child.relative_to(bag).as_posix()
            )
            if relative in inventory:
                raise ValueError(
                    "Telemetry source bag contains duplicate normalized paths"
                )
            inventory[relative] = child_stat
    return inventory


def _inventory_identity(
    inventory: dict[str, os.stat_result],
) -> dict[str, tuple[int, int, int, int, int, int]]:
    return {
        path: _stat_identity(file_stat)
        for path, file_stat in inventory.items()
    }


def _stat_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _open_regular_file(bag: Path, relative_path: str) -> tuple[int, list[int]]:
    parts = PurePosixPath(relative_path).parts
    directory_fds: list[int] = []
    current_fd = os.open(
        bag,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    directory_fds.append(current_fd)
    try:
        for part in parts[:-1]:
            current_fd = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current_fd,
        )
        return file_fd, directory_fds
    except BaseException:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        raise


def _hash_fd(file_fd: int, *, collect_bytes: bool) -> tuple[bytes | None, str]:
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if collect_bytes else None
    while True:
        chunk = os.read(file_fd, _CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return (b"".join(chunks) if chunks is not None else None), digest.hexdigest()


def _stable_file_digest(
    bag: Path, relative_path: str, *, collect_bytes: bool
) -> tuple[bytes | None, int, str]:
    file_fd, directory_fds = _open_regular_file(bag, relative_path)
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                f"Telemetry source bag entry is not a regular file: {relative_path}"
            )
        content, digest = _hash_fd(file_fd, collect_bytes=collect_bytes)
        after = os.fstat(file_fd)
        if _stat_identity(before) != _stat_identity(after):
            raise ValueError(
                f"Telemetry source bag file changed while hashing: {relative_path}"
            )
        return content, before.st_size, digest
    except OSError as exc:
        raise ValueError(
            f"Telemetry source bag file could not be hashed safely: {relative_path}"
        ) from exc
    finally:
        os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
