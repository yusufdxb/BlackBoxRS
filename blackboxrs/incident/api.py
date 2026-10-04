"""High-level functional API for non-CLI consumers.

Provides three convenience entry points so other tools (or a future
web UI) can integrate without depending on the CLI layer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from blackboxrs.core.config import BlackBoxConfig

from .builder import IncidentBuilder
from .bundle import BundleReader, validate_bundle_path
from .report import render


def build_incident(
    window_start: datetime,
    window_end: datetime,
    *,
    config: BlackBoxConfig | None = None,
    incidents_dir: Path | None = None,
    title: str | None = None,
    notes: str | None = None,
    tags: Iterable[str] = (),
) -> Path:
    """Build a bundle for the given window and return its directory."""
    builder = IncidentBuilder(config=config, incidents_dir=incidents_dir)
    return builder.build(
        window_start, window_end, title=title, notes=notes, tags=tags
    )


def load_bundle(bundle_dir: Path | str) -> BundleReader:
    """Open an existing bundle for reading."""
    return BundleReader(Path(bundle_dir))


def render_report(bundle_dir: Path | str) -> str:
    """Render an incident bundle's ``report.md`` content."""
    path = Path(bundle_dir)
    result = validate_bundle_path(path)
    if result.errors:
        details = "; ".join(
            f"{issue.code}:{issue.path or '-'}:{issue.message}"
            for issue in result.errors
        )
        raise ValueError(f"Invalid incident bundle {path}: {details}")
    text = render(BundleReader(path))
    if result.state == "legacy":
        warning = (
            "> **Integrity warning:** this legacy bundle has no `manifest.json`; "
            "file checksums were not verified.\n\n"
        )
        return warning + text
    return text
