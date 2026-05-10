"""Incident intelligence and prevention for BlackBoxRS.

This package owns the post-capture pipeline: turning raw events from the
``logging`` subsystem into reproducible *incident bundles* (timelines,
evidence, config/version signatures, fingerprints, and human-readable
reports).

Public API:

- :class:`Incident`, :class:`EvidenceBundle`, :class:`TimelineEvent`,
  :class:`DetectorTrigger`, :class:`SystemSnapshot`,
  :class:`ConfigSignature`, :class:`VersionSignature`,
  :class:`FailureFingerprint`, :class:`LikelyCauseHypothesis`: domain
  models.
- :func:`build_incident`: top-level builder entry point.
- :func:`load_bundle`: read a bundle from disk.
- :func:`render_report`: render a bundle's ``report.md``.
"""

from __future__ import annotations

from .models import (
    ConfigSignature,
    DetectorTrigger,
    EvidenceBundle,
    FailureFingerprint,
    GPUSnapshot,
    Incident,
    LikelyCauseHypothesis,
    PrecursorRef,
    ProcessSnapshot,
    RecurrenceContext,
    SystemSnapshot,
    TimelineEvent,
    TopicSnapshot,
    VersionSignature,
)
from .api import build_incident, load_bundle, render_report

__all__ = [
    "ConfigSignature",
    "DetectorTrigger",
    "EvidenceBundle",
    "FailureFingerprint",
    "GPUSnapshot",
    "Incident",
    "LikelyCauseHypothesis",
    "PrecursorRef",
    "ProcessSnapshot",
    "RecurrenceContext",
    "SystemSnapshot",
    "TimelineEvent",
    "TopicSnapshot",
    "VersionSignature",
    "build_incident",
    "load_bundle",
    "render_report",
]
