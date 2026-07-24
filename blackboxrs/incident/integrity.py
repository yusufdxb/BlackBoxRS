"""Integrity manifest and validation for incident bundles."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blackboxrs import __version__
from blackboxrs.core.schemas import BlackBoxEvent

from .models import (
    ConfigSignature,
    FailureFingerprint,
    Incident,
    TimelineEvent,
    VersionSignature,
)


MANIFEST_PATH = "manifest.json"
MANIFEST_SCHEMA_VERSION = "1.0"
BUNDLE_FORMAT_VERSION = "1.0"
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {MANIFEST_SCHEMA_VERSION}
SUPPORTED_BUNDLE_FORMAT_VERSIONS = {BUNDLE_FORMAT_VERSION}

BundleState = Literal[
    "legacy",
    "valid_finalized",
    "incomplete",
    "corrupted",
    "unsupported_version",
]


class ManifestFile(BaseModel):
    """One bundle file recorded by the integrity manifest."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int = Field(..., ge=0)
    sha256: str
    required: bool = False


class BundleManifest(BaseModel):
    """Root `manifest.json` for a finalized incident bundle."""

    model_config = ConfigDict(extra="forbid")

    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION
    bundle_format_version: str = BUNDLE_FORMAT_VERSION
    incident_id: str
    created_at: datetime
    finalized_at: datetime
    finalized: bool = True
    generator: dict[str, str] = Field(default_factory=dict)
    files: list[ManifestFile]


class ValidationIssue(BaseModel):
    """Structured validation issue."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    path: str | None = None


class BundleValidationResult(BaseModel):
    """Structured result returned by bundle validation."""

    model_config = ConfigDict(extra="forbid")

    bundle_path: str
    state: BundleState
    manifest_schema_version: str | None = None
    bundle_format_version: str | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in {"valid_finalized", "legacy"} and not self.errors

    @property
    def finalized(self) -> bool:
        return self.state == "valid_finalized"


def build_manifest(
    bundle_dir: Path,
    *,
    incident_id: str,
    created_at: datetime,
    required_files: tuple[str, ...],
) -> BundleManifest:
    """Build an integrity manifest from the current bundle directory."""
    files: list[ManifestFile] = []
    required_set = set(required_files)
    for path in _iter_regular_files(bundle_dir):
        rel = _relative_posix(bundle_dir, path)
        if rel == MANIFEST_PATH:
            continue
        files.append(
            ManifestFile(
                path=rel,
                size_bytes=path.stat().st_size,
                sha256=_sha256_file(path),
                required=rel in required_set,
            )
        )
    files.sort(key=lambda item: item.path)
    return BundleManifest(
        incident_id=incident_id,
        created_at=created_at,
        finalized_at=datetime.now(timezone.utc),
        generator={"name": "blackboxrs", "version": __version__},
        files=files,
    )


def write_manifest(bundle_dir: Path, manifest: BundleManifest) -> Path:
    """Write manifest deterministically as the bundle's final marker."""
    path = Path(bundle_dir) / MANIFEST_PATH
    tmp = path.with_name(f".{MANIFEST_PATH}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest.model_dump(mode="json"), fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return path


def validate_bundle(
    bundle_dir: Path,
    *,
    required_files: tuple[str, ...],
    require_finalized: bool = False,
) -> BundleValidationResult:
    """Validate bundle completeness and integrity.

    Legacy bundles without a manifest remain readable unless
    ``require_finalized`` is true.
    """
    bundle_dir = Path(bundle_dir)
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    if not bundle_dir.is_dir():
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state="incomplete",
            errors=[_issue("not_directory", "Bundle directory not found.")],
        )

    manifest_path = bundle_dir / MANIFEST_PATH
    if not manifest_path.exists():
        state: BundleState = "incomplete" if _looks_like_staging(bundle_dir) else "legacy"
        _validate_required_presence(bundle_dir, required_files, errors)
        if state == "legacy":
            warnings.append(
                _issue(
                    "missing_manifest",
                    "Legacy bundle has no manifest; integrity is unverified.",
                    MANIFEST_PATH,
                )
            )
        else:
            errors.append(
                _issue(
                    "missing_manifest",
                    "Staging or incomplete bundle has no final manifest.",
                    MANIFEST_PATH,
                )
            )
        if require_finalized and state == "legacy":
            errors.append(
                _issue(
                    "legacy_not_allowed",
                    "Operation requires a finalized bundle with manifest.json.",
                    MANIFEST_PATH,
                )
            )
        if errors:
            state = "incomplete"
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state=state,
            errors=errors,
            warnings=warnings,
        )

    manifest, manifest_errors = _load_manifest(manifest_path)
    if manifest is None:
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state="corrupted",
            errors=manifest_errors,
        )

    if manifest.manifest_schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        errors.append(
            _issue(
                "unsupported_manifest_version",
                f"Unsupported manifest schema version {manifest.manifest_schema_version!r}.",
                MANIFEST_PATH,
            )
        )
    if manifest.bundle_format_version not in SUPPORTED_BUNDLE_FORMAT_VERSIONS:
        errors.append(
            _issue(
                "unsupported_bundle_format",
                f"Unsupported bundle format version {manifest.bundle_format_version!r}.",
                MANIFEST_PATH,
            )
        )
    if errors:
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state="unsupported_version",
            manifest_schema_version=manifest.manifest_schema_version,
            bundle_format_version=manifest.bundle_format_version,
            errors=errors,
        )

    _validate_manifest_files(bundle_dir, manifest, required_files, errors)
    if not errors:
        _validate_schema_files(bundle_dir, errors)
    _validate_unexpected_files(bundle_dir, manifest, warnings)

    state = "valid_finalized" if not errors else _error_state(errors)
    return BundleValidationResult(
        bundle_path=str(bundle_dir),
        state=state,
        manifest_schema_version=manifest.manifest_schema_version,
        bundle_format_version=manifest.bundle_format_version,
        errors=errors,
        warnings=warnings,
    )


def inspect_bundle_state(bundle_dir: Path) -> BundleValidationResult:
    """Return a cheap bundle state summary without hashing payload files."""
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state="incomplete",
            errors=[_issue("not_directory", "Bundle directory not found.")],
        )
    manifest_path = bundle_dir / MANIFEST_PATH
    if not manifest_path.exists():
        state: BundleState = "incomplete" if _looks_like_staging(bundle_dir) else "legacy"
        issues = [
            _issue(
                "missing_manifest",
                "Bundle has no manifest; integrity is unverified.",
                MANIFEST_PATH,
            )
        ]
        if state == "legacy":
            return BundleValidationResult(
                bundle_path=str(bundle_dir),
                state=state,
                warnings=issues,
            )
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state=state,
            errors=issues,
        )
    manifest, manifest_errors = _load_manifest(manifest_path)
    if manifest is None:
        return BundleValidationResult(
            bundle_path=str(bundle_dir),
            state="corrupted",
            errors=manifest_errors,
        )
    errors: list[ValidationIssue] = []
    if manifest.manifest_schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        errors.append(
            _issue(
                "unsupported_manifest_version",
                f"Unsupported manifest schema version {manifest.manifest_schema_version!r}.",
                MANIFEST_PATH,
            )
        )
    if manifest.bundle_format_version not in SUPPORTED_BUNDLE_FORMAT_VERSIONS:
        errors.append(
            _issue(
                "unsupported_bundle_format",
                f"Unsupported bundle format version {manifest.bundle_format_version!r}.",
                MANIFEST_PATH,
            )
        )
    return BundleValidationResult(
        bundle_path=str(bundle_dir),
        state="unsupported_version" if errors else "valid_finalized",
        manifest_schema_version=manifest.manifest_schema_version,
        bundle_format_version=manifest.bundle_format_version,
        errors=errors,
    )


def format_validation_result(result: BundleValidationResult) -> str:
    """Render validation result for CLI output."""
    lines = [f"Bundle: {result.bundle_path}", f"State: {result.state}"]
    for issue in result.errors:
        suffix = f" ({issue.path})" if issue.path else ""
        lines.append(f"ERROR {issue.code}{suffix}: {issue.message}")
    for issue in result.warnings:
        suffix = f" ({issue.path})" if issue.path else ""
        lines.append(f"WARN  {issue.code}{suffix}: {issue.message}")
    if not result.errors and result.state == "valid_finalized":
        lines.append("OK: finalized bundle integrity verified.")
    if not result.errors and result.state == "legacy":
        lines.append("OK: legacy bundle structure is readable; integrity is unverified.")
    return "\n".join(lines)


def _load_manifest(path: Path) -> tuple[BundleManifest | None, list[ValidationIssue]]:
    try:
        raw = path.read_text(encoding="utf-8")
        manifest = BundleManifest.model_validate_json(raw)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        return None, [
            _issue(
                "malformed_manifest",
                f"Could not parse manifest.json: {type(exc).__name__}: {exc}",
                MANIFEST_PATH,
            )
        ]
    return manifest, []


def _validate_manifest_files(
    bundle_dir: Path,
    manifest: BundleManifest,
    required_files: tuple[str, ...],
    errors: list[ValidationIssue],
) -> None:
    required_set = set(required_files)
    seen: set[str] = set()
    for entry in manifest.files:
        safe = _validate_manifest_path(entry.path, errors)
        if safe is None:
            continue
        if safe in seen:
            errors.append(
                _issue("duplicate_manifest_entry", "Duplicate manifest file entry.", entry.path)
            )
            continue
        seen.add(safe)
        target = bundle_dir / safe
        if not target.exists():
            errors.append(_issue("missing_manifest_file", "Manifest file is missing.", entry.path))
            continue
        if target.is_symlink():
            errors.append(
                _issue(
                    "symlink_manifest_file",
                    "Manifest path is a symlink; integrity checks do not follow symlinks.",
                    entry.path,
                )
            )
            continue
        if not target.is_file():
            errors.append(_issue("not_regular_file", "Manifest path is not a regular file.", entry.path))
            continue
        stat = target.stat()
        if stat.st_size != entry.size_bytes:
            errors.append(
                _issue(
                    "size_mismatch",
                    f"Expected {entry.size_bytes} byte(s), found {stat.st_size}.",
                    entry.path,
                )
            )
        actual_hash = _sha256_file(target)
        if actual_hash != entry.sha256:
            errors.append(
                _issue(
                    "checksum_mismatch",
                    f"Expected sha256 {entry.sha256}, found {actual_hash}.",
                    entry.path,
                )
            )

    for required in sorted(required_set):
        if required not in seen:
            errors.append(
                _issue(
                    "missing_required_manifest_entry",
                    "Required file is not listed in manifest.",
                    required,
                )
            )
        elif not (bundle_dir / required).exists():
            errors.append(_issue("missing_required_file", "Required file is missing.", required))


def _validate_manifest_path(path: str, errors: list[ValidationIssue]) -> str | None:
    pure = PurePosixPath(path)
    if path == MANIFEST_PATH:
        errors.append(_issue("manifest_self_reference", "Manifest must not list itself.", path))
        return None
    if not path or pure.is_absolute() or "\\" in path:
        errors.append(_issue("unsafe_manifest_path", "Manifest path must be relative POSIX.", path))
        return None
    if any(part in {".", ".."} for part in pure.parts):
        errors.append(_issue("unsafe_manifest_path", "Manifest path escapes bundle.", path))
        return None
    canonical = str(pure)
    if canonical != path:
        errors.append(
            _issue(
                "unsafe_manifest_path",
                "Manifest path must be canonical POSIX with no redundant separators.",
                path,
            )
        )
        return None
    return canonical


def _validate_required_presence(
    bundle_dir: Path,
    required_files: tuple[str, ...],
    errors: list[ValidationIssue],
) -> None:
    for rel in required_files:
        if not (bundle_dir / rel).exists():
            errors.append(_issue("missing_required_file", "Required file is missing.", rel))


def _validate_schema_files(bundle_dir: Path, errors: list[ValidationIssue]) -> None:
    validators: list[tuple[str, Any]] = [
        ("incident.json", lambda text: Incident.model_validate_json(text)),
        (
            "timeline.json",
            lambda text: [TimelineEvent.model_validate(item) for item in json.loads(text)],
        ),
        ("signatures/config.json", lambda text: ConfigSignature.model_validate_json(text)),
        ("signatures/versions.json", lambda text: VersionSignature.model_validate_json(text)),
        ("fingerprint.json", lambda text: FailureFingerprint.model_validate_json(text)),
    ]
    for rel, validator in validators:
        try:
            validator((bundle_dir / rel).read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(
                _issue(
                    "schema_invalid",
                    f"Could not parse required file: {type(exc).__name__}: {exc}",
                    rel,
                )
            )
    events = bundle_dir / "evidence" / "events.jsonl"
    try:
        event_count = 0
        with open(events, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                BlackBoxEvent.from_jsonl(stripped)
                event_count += 1
        if event_count == 0:
            errors.append(
                _issue(
                    "empty_required_file",
                    "Required evidence stream must contain at least one event.",
                    "evidence/events.jsonl",
                )
            )
    except Exception as exc:
        errors.append(
            _issue(
                "schema_invalid",
                f"Could not parse evidence/events.jsonl: {type(exc).__name__}: {exc}",
                "evidence/events.jsonl",
            )
        )


def _validate_unexpected_files(
    bundle_dir: Path,
    manifest: BundleManifest,
    warnings: list[ValidationIssue],
) -> None:
    expected = {entry.path for entry in manifest.files}
    expected.add(MANIFEST_PATH)
    for path in _iter_regular_files(bundle_dir):
        rel = _relative_posix(bundle_dir, path)
        if rel not in expected:
            warnings.append(
                _issue(
                    "unexpected_file",
                    "File is not listed in manifest and is ignored by integrity checks.",
                    rel,
                )
            )
    for path in Path(bundle_dir).rglob("*"):
        if path.is_symlink():
            rel = _relative_posix(bundle_dir, path)
            warnings.append(
                _issue(
                    "unexpected_symlink",
                    "Symlink is not followed by integrity checks.",
                    rel,
                )
            )


def _iter_regular_files(bundle_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in Path(bundle_dir).rglob("*"):
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return sorted(paths, key=lambda p: _relative_posix(bundle_dir, p))


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _issue(code: str, message: str, path: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path)


def _looks_like_staging(path: Path) -> bool:
    return ".staging." in path.name


def _error_state(errors: list[ValidationIssue]) -> BundleState:
    if any(e.code.startswith("unsupported") for e in errors):
        return "unsupported_version"
    if any(
        e.code in {
            "missing_required_file",
            "missing_required_manifest_entry",
            "missing_manifest_file",
        }
        for e in errors
    ):
        return "incomplete"
    return "corrupted"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

