"""On-disk evidence bundle layout: writer and reader.
The bundle directory is the source of truth. This module owns the layout
contract from ``ARCHITECTURE_PIVOT.md`` §1.2. Tools must not bypass it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from blackboxrs.core.schemas import BlackBoxEvent

from .models import (
    ConfigSignature,
    DetectorTrigger,
    FailureFingerprint,
    Incident,
    SystemSnapshot,
    TimelineEvent,
    VersionSignature,
)
from .integrity import (
    BundleManifest,
    BundleValidationResult,
    build_manifest,
    inspect_bundle_state,
    validate_bundle,
    write_manifest,
)


# Required files; absent → bundle is invalid.
_REQUIRED = (
    "incident.json",
    "report.md",
    "timeline.json",
    "evidence/events.jsonl",
    "signatures/config.json",
    "signatures/versions.json",
    "fingerprint.json",
)

_OPTIONAL = (
    "evidence/triggers.json",
    "evidence/snapshots.json",
    "signatures/diff.json",
)


def _dump_json(path: Path, obj: object) -> None:
    """Write *obj* as deterministic JSON.

    sort_keys ensures bit-stable output across runs; default=str handles
    datetimes via pydantic's ISO formatting when called via
    ``model_dump(mode='json')``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


class BundleWriter:
    """Builds an evidence bundle directory.

    Operates on a single incident directory. Multiple writes accumulate
    into the same directory. Idempotent: calling each write twice with
    the same inputs leaves the directory in the same state.
    """

    def __init__(self, bundle_dir: Path) -> None:
        self._dir = Path(bundle_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "evidence").mkdir(parents=True, exist_ok=True)
        (self._dir / "signatures").mkdir(parents=True, exist_ok=True)
        (self._dir / "attachments").mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._dir

    # -- writers ---------------------------------------------------------

    def write_incident(self, incident: Incident) -> None:
        _dump_json(self._dir / "incident.json", incident.model_dump(mode="json"))

    def write_events_jsonl(self, events: Iterable[BlackBoxEvent]) -> None:
        target = self._dir / "evidence" / "events.jsonl"
        with open(target, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(ev.to_jsonl())
                fh.write("\n")

    def write_triggers(self, triggers: list[DetectorTrigger]) -> None:
        _dump_json(
            self._dir / "evidence" / "triggers.json",
            [t.model_dump(mode="json") for t in triggers],
        )

    def write_snapshots(self, snapshots: list[SystemSnapshot]) -> None:
        _dump_json(
            self._dir / "evidence" / "snapshots.json",
            [s.model_dump(mode="json") for s in snapshots],
        )

    def write_signatures(
        self, config_sig: ConfigSignature, version_sig: VersionSignature
    ) -> None:
        _dump_json(
            self._dir / "signatures" / "config.json",
            config_sig.model_dump(mode="json"),
        )
        _dump_json(
            self._dir / "signatures" / "versions.json",
            version_sig.model_dump(mode="json"),
        )

    def write_diff(self, diff) -> None:
        """Persist an :class:`IncidentDiff` so the report is self-contained.

        Optional file: bundles produced before P5 do not have it, and
        the reader treats absence as "no baseline available."
        """
        _dump_json(
            self._dir / "signatures" / "diff.json",
            diff.model_dump(mode="json"),
        )

    def write_timeline(self, timeline: list[TimelineEvent]) -> None:
        _dump_json(
            self._dir / "timeline.json",
            [t.model_dump(mode="json") for t in timeline],
        )

    def write_fingerprint(self, fp: FailureFingerprint) -> None:
        _dump_json(self._dir / "fingerprint.json", fp.model_dump(mode="json"))

    def write_report(self, text: str) -> None:
        with open(self._dir / "report.md", "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")

    def build_manifest(self, *, incident_id: str, created_at) -> BundleManifest:
        """Build a manifest for the current on-disk bundle contents."""
        return build_manifest(
            self._dir,
            incident_id=incident_id,
            created_at=created_at,
            required_files=_REQUIRED,
        )

    def write_manifest(self, manifest: BundleManifest) -> Path:
        """Write manifest.json as the final marker."""
        return write_manifest(self._dir, manifest)

    def validate(self, *, require_finalized: bool = False) -> BundleValidationResult:
        """Validate this bundle's layout and integrity."""
        return validate_bundle(
            self._dir,
            required_files=_REQUIRED,
            require_finalized=require_finalized,
        )

    # -- introspection ---------------------------------------------------

    def required_files_present(self) -> list[str]:
        return [name for name in _REQUIRED if (self._dir / name).exists()]

    def required_files_missing(self) -> list[str]:
        return [name for name in _REQUIRED if not (self._dir / name).exists()]


class BundleReader:
    """Reads an existing evidence bundle.

    Refuses to load a bundle that is missing any required file (callers
    can suppress that with ``strict=False``).
    """

    def __init__(self, bundle_dir: Path, *, strict: bool = True) -> None:
        self._dir = Path(bundle_dir)
        if not self._dir.is_dir():
            raise FileNotFoundError(f"Bundle directory not found: {self._dir}")
        if strict:
            result = validate_bundle(self._dir, required_files=_REQUIRED)
            if result.errors:
                details = "; ".join(
                    f"{issue.code}:{issue.path or '-'}:{issue.message}"
                    for issue in result.errors
                )
                raise ValueError(f"Invalid bundle {self._dir}: {details}")

    @property
    def path(self) -> Path:
        return self._dir

    # -- readers ---------------------------------------------------------

    def load_incident(self) -> Incident:
        with open(self._dir / "incident.json", "r", encoding="utf-8") as fh:
            return Incident.model_validate_json(fh.read())

    def iter_events(self) -> Iterator[BlackBoxEvent]:
        target = self._dir / "evidence" / "events.jsonl"
        if not target.exists():
            return
        with open(target, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield BlackBoxEvent.from_jsonl(line)

    def load_triggers(self) -> list[DetectorTrigger]:
        target = self._dir / "evidence" / "triggers.json"
        if not target.exists():
            return []
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [DetectorTrigger.model_validate(item) for item in data]

    def load_snapshots(self) -> list[SystemSnapshot]:
        target = self._dir / "evidence" / "snapshots.json"
        if not target.exists():
            return []
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [SystemSnapshot.model_validate(item) for item in data]

    def load_timeline(self) -> list[TimelineEvent]:
        target = self._dir / "timeline.json"
        if not target.exists():
            return []
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [TimelineEvent.model_validate(item) for item in data]

    def load_signatures(self) -> tuple[ConfigSignature, VersionSignature]:
        with open(self._dir / "signatures" / "config.json", "r", encoding="utf-8") as fh:
            cfg = ConfigSignature.model_validate_json(fh.read())
        with open(self._dir / "signatures" / "versions.json", "r", encoding="utf-8") as fh:
            ver = VersionSignature.model_validate_json(fh.read())
        return cfg, ver

    def load_fingerprint(self) -> FailureFingerprint | None:
        target = self._dir / "fingerprint.json"
        if not target.exists():
            return None
        with open(target, "r", encoding="utf-8") as fh:
            return FailureFingerprint.model_validate_json(fh.read())

    def report_text(self) -> str:
        return (self._dir / "report.md").read_text(encoding="utf-8")

    def load_diff(self):
        """Return the persisted :class:`IncidentDiff`, or ``None`` if absent.

        Bundles built before P5 do not include ``signatures/diff.json``;
        the renderer treats ``None`` as "no baseline available."
        """
        target = self._dir / "signatures" / "diff.json"
        if not target.exists():
            return None
        # Lazy import to avoid pulling diff models when only loading
        # bundles for unrelated purposes.
        from .diff import IncidentDiff

        with open(target, "r", encoding="utf-8") as fh:
            return IncidentDiff.model_validate_json(fh.read())

    def validate(self, *, require_finalized: bool = False) -> BundleValidationResult:
        """Validate bundle integrity and completeness."""
        return validate_bundle(
            self._dir,
            required_files=_REQUIRED,
            require_finalized=require_finalized,
        )


def validate_bundle_path(
    bundle_dir: Path, *, require_finalized: bool = False
) -> BundleValidationResult:
    """Validate a bundle directory without constructing a reader."""
    return validate_bundle(
        Path(bundle_dir),
        required_files=_REQUIRED,
        require_finalized=require_finalized,
    )


def inspect_bundle_path(bundle_dir: Path) -> BundleValidationResult:
    """Return a cheap bundle state summary without deep integrity checks."""
    return inspect_bundle_state(Path(bundle_dir))


__all__ = [
    "BundleReader",
    "BundleWriter",
    "inspect_bundle_path",
    "validate_bundle_path",
]
