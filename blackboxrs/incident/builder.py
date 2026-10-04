"""IncidentBuilder: turns log slices into an evidence bundle.

This is the orchestrator. Stages from ``ARCHITECTURE_PIVOT.md`` §2.2 are
each delegated to a small module so they can be tested in isolation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.signatures import (
    ConfigSignatureCollector,
    VersionSignatureCollector,
)
from blackboxrs.core.snapshots import SystemSnapshotter
from blackboxrs.logging.reader import LogReader
from blackboxrs.recording.native import NativeCaptureReader, resolve_current_native_session

from . import cause as cause_mod
from . import diff as diff_mod
from . import fingerprint as fingerprint_mod
from . import recurrence as recurrence_mod
from . import report as report_mod
from . import timeline as timeline_mod
from .bundle import BundleReader, BundleWriter
from .models import (
    CaptureQuality,
    DetectorTrigger,
    Incident,
    LikelyCauseHypothesis,
    SystemSnapshot,
)


logger = logging.getLogger(__name__)


_DEFAULT_INCIDENTS_DIR = Path("~/.blackboxrs/incidents").expanduser()

#: Marker reason recorded when the Python capture backend supplies the evidence.
#: It has no drop, queue, or delivery accounting, so it can never substantiate a
#: completeness claim; the report discloses this instead of staying silent.
PYTHON_BACKEND_NO_ACCOUNTING = "python_backend_has_no_delivery_accounting"


def _make_incident_id(window_start: datetime, session_id: str, host: str) -> str:
    """Deterministic id from (window_start, session_id, host).

    Format: ``inc_YYYY-MM-DDTHH-MM-SS_<sha8>``. Stable across reruns of
    the same inputs so re-building from the same logs returns to the
    same directory.
    """
    payload = f"{window_start.isoformat()}|{session_id}|{host}".encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:8]
    stamp = window_start.strftime("%Y-%m-%dT%H-%M-%S")
    return f"inc_{stamp}_{suffix}"


def _severity_from(triggers: list[DetectorTrigger], events: list[BlackBoxEvent]) -> str:
    """Pick severity = highest of all triggers + raw events."""
    rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    best = 0
    for t in triggers:
        best = max(best, rank.get(t.severity, 0))
    for ev in events:
        best = max(best, rank.get(ev.severity, 0))
    inverse = {v: k for k, v in rank.items()}
    return inverse[best] if best > 0 else "warning"


_VALID_SUBSYSTEMS = {"ros", "system", "gpu", "anomaly", "recorder", "config", "external"}


def _promote_trigger(ev: BlackBoxEvent, line_index: int) -> DetectorTrigger:
    """Turn an ``anomaly_engine`` event into a typed :class:`DetectorTrigger`.

    Keeps a backreference into ``events.jsonl`` via ``source_event_ref``
    so the renderer and timeline can hyperlink claims. The trigger's
    ``subsystem`` honours ``metadata.target_subsystem`` when present and
    valid; otherwise falls back to ``"anomaly"`` (legacy v0.3 shape).
    """
    detector_name = ev.data.get("detector", "unknown")
    detector_class = ev.metadata.get("detector_class", detector_name) or detector_name
    subject = (
        ev.data.get("topic") or ev.data.get("metric") or ev.data.get("subject") or ev.event_type
    )
    sig_fields = ev.metadata.get("signature_fields") or []
    target_sub = ev.metadata.get("target_subsystem") or "anomaly"
    if target_sub not in _VALID_SUBSYSTEMS:
        target_sub = "anomaly"

    return DetectorTrigger(
        trigger_id=DetectorTrigger.make_id(detector_name, ev.timestamp, subject),
        detector=detector_name,
        detector_class=detector_class,
        t=ev.timestamp,
        subsystem=target_sub,  # type: ignore[arg-type]
        subject=str(subject),
        severity=ev.severity
        if ev.severity in ("info", "warning", "error", "critical")
        else "warning",
        message=str(ev.data.get("message", "")),
        data={k: v for k, v in ev.data.items()},
        signature_fields=list(sig_fields),
        source_event_ref=f"events.jsonl#L{line_index}",
    )


def _summary_line(top_cause, triggers: list[DetectorTrigger]) -> str:
    """Build a one-paragraph summary; promote top hypothesis when confident."""
    if top_cause is not None and top_cause.confidence >= 0.7:
        return top_cause.cause
    if triggers:
        return (
            f"{len(triggers)} detector trigger(s) fired during the window; "
            f"top detector: `{triggers[0].detector_class.rsplit('.', 1)[-1]}` "
            f"on `{triggers[0].subject}`. Hypothesis confidence is below "
            f"threshold; review supporting evidence in the timeline."
        )
    return (
        "No detector triggers in this window. Bundle was built manually; "
        "the timeline shows raw events only."
    )


def _apply_capture_quality_limit(
    causes: list[LikelyCauseHypothesis],
    quality: CaptureQuality | None,
) -> list[LikelyCauseHypothesis]:
    """Keep incomplete native evidence from supporting confident claims."""
    if quality is None or quality.completeness == "complete":
        return causes
    # The Python backend never measures delivery, so its "unknown" completeness is
    # a standing property of the backend rather than evidence that this particular
    # window lost data. The report discloses it either way, so the claim is not
    # silent; capping every Python-backend incident here would suppress the
    # detector-grounded confidence the rest of the product is built on. Only
    # measured degradation caps confidence.
    if quality.incomplete_reasons == [PYTHON_BACKEND_NO_ACCOUNTING]:
        return causes

    reason_text = ", ".join(quality.incomplete_reasons[:4]) or quality.completeness
    caveat = (
        "Native capture evidence is incomplete "
        f"({reason_text}); missing evidence may change this ranking."
    )
    limited: list[LikelyCauseHypothesis] = []
    for cause in causes:
        existing = f" {cause.caveat}" if cause.caveat else ""
        reasoning = list(cause.reasoning)
        reasoning.append("capture-quality limit applied because native evidence is not complete.")
        limited.append(
            cause.model_copy(
                update={
                    "confidence": min(cause.confidence, 0.69),
                    "caveat": caveat + existing,
                    "reasoning": reasoning,
                }
            )
        )
    return limited


class IncidentBuilder:
    """Orchestrator that produces a bundle from a log slice.

    Args:
        config: BlackBoxRS config (used for log_dir defaults).
        incidents_dir: Output root. Defaults to ``~/.blackboxrs/incidents``.
        log_reader: Inject a custom :class:`LogReader` for tests; otherwise
            constructed from ``config.log_dir``.
    """

    def __init__(
        self,
        config: BlackBoxConfig | None = None,
        incidents_dir: Path | None = None,
        log_reader: LogReader | None = None,
    ) -> None:
        self._config = config or BlackBoxConfig.default()
        self._incidents_dir = Path(incidents_dir or os.path.expanduser(str(_DEFAULT_INCIDENTS_DIR)))
        self._incidents_dir.mkdir(parents=True, exist_ok=True)

        log_dir = Path(os.path.expanduser(self._config.log_dir))
        self._log_reader = log_reader or LogReader(log_dir)

    # -- public --------------------------------------------------------

    def build(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        session_id: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        tags: Iterable[str] = (),
    ) -> Path:
        """Build a bundle and return its directory.

        Args:
            window_start: Inclusive start of the incident window (UTC).
            window_end: Inclusive end of the incident window (UTC).
            session_id: Override session id. When None, the value is
                taken from the first event in the window's metadata, or
                ``"unknown"`` if no events are present.
            title: Optional human-readable title.
            notes: Optional free-form notes from the user.
            tags: Optional list of tags.

        Returns:
            Path to the bundle directory.
        """
        events = list(self._log_reader.read_all(start=window_start, end=window_end))
        # The Python backend has no drop, queue, or delivery accounting, so it can
        # never substantiate a completeness claim. Say so explicitly instead of
        # leaving the quality object None, which would omit the capture-quality
        # report section and bypass the confidence cap entirely.
        capture_quality: CaptureQuality | None = CaptureQuality(
            backend="python",
            completeness="unknown",
            incomplete_reasons=[PYTHON_BACKEND_NO_ACCOUNTING],
        )
        native_files: tuple[tuple[Path, Path], ...] = ()
        if self._config.capture.backend == "cpp":
            native_path = self._config.capture.native_session_path
            if not native_path:
                current = resolve_current_native_session(self._config.capture.native_output_dir)
                native_path = str(current) if current is not None else None
            if native_path:
                native_reader = NativeCaptureReader(Path(native_path).expanduser())
                native_events = list(
                    native_reader.iter_blackbox_events(start=window_start, end=window_end)
                )
                for event in native_events:
                    native_ref = event.metadata.get("native_evidence_ref")
                    if isinstance(native_ref, str) and native_ref.startswith("native_capture/"):
                        event.metadata["native_evidence_ref"] = "attachments/" + native_ref
                    evidence_ref = event.data.get("evidence_ref")
                    if isinstance(evidence_ref, str) and evidence_ref.startswith("native_capture/"):
                        event.data["evidence_ref"] = "attachments/" + evidence_ref
                events.extend(native_events)
                native_files = native_reader.portable_files()
                capture_quality = native_reader.quality
            else:
                capture_quality = CaptureQuality(
                    backend="cpp",
                    completeness="unknown",
                    clean=None,
                    incomplete_reasons=["native_current_session_unavailable"],
                )
            events.sort(
                key=lambda event: (
                    event.timestamp,
                    int(event.metadata.get("monotonic_ns", -1)),
                    int(event.metadata.get("sequence", -1)),
                )
            )

        host = socket.gethostname()
        sid = session_id or _session_from_events(events) or "unknown"

        # Promote anomaly_engine events into typed triggers and keep a
        # source line ref pointing into events.jsonl (1-indexed, matches
        # what BundleWriter writes).
        triggers: list[DetectorTrigger] = []
        for line_index, ev in enumerate(events, start=1):
            if ev.source == "anomaly_engine":
                triggers.append(_promote_trigger(ev, line_index))

        # Snapshots: project the system + ROS event stream into a typed
        # series at fixed cadence. Used by the graph-delta and
        # resource-excursion derivers and by the fingerprint topology
        # signature.
        snapshots: list[SystemSnapshot] = SystemSnapshotter(
            fallback_host=host,
        ).project(events, window_start, window_end)

        # Signatures: capture *now* (best-effort). M4 will read them from
        # the per-session cache the daemon writes at startup.
        config_sig = ConfigSignatureCollector().collect()
        version_sig = VersionSignatureCollector().collect()

        # Timeline. Raw + trigger rows ordered by timestamp, plus derived
        # silence-interval / graph-delta / resource-excursion rows folded
        # in. Thresholds and dead-topic timeout come from the daemon
        # config so the derivers stay consistent with what the live
        # detectors used.
        timeline = timeline_mod.reconstruct(
            events,
            triggers,
            snapshots=snapshots,
            thresholds=self._config.anomaly_engine.thresholds,
            dead_topic_timeout_sec=self._config.anomaly_engine.dead_topic.timeout_sec,
        )

        # Fingerprint.
        fp = fingerprint_mod.compute(triggers, snapshots)

        severity = _severity_from(triggers, events)
        incident_id = _make_incident_id(window_start, sid, host)
        bundle_dir = self._incidents_dir / incident_id
        staging_dir = self._staging_dir_for(incident_id)
        writer = BundleWriter(staging_dir)

        # Diff against the most recent prior bundle on this host (if any).
        # We compute this BEFORE ranking causes so the precursor-aware
        # ranker can use config/version drift as a (coincident) signal.
        prior_cfg, prior_ver = self._load_prior_signatures(
            host=host,
            before=window_start,
            exclude_id=incident_id,
        )
        incident_diff = diff_mod.compute(prior_cfg, prior_ver, config_sig, version_sig)

        # Recurrence lookup: a callable the ranker can use to ask "have
        # we seen this fingerprint before on this host?" The lookup
        # walks bundle directories lazily; failure is silent.
        recurrence_lookup = recurrence_mod.build_recurrence_lookup(
            self._incidents_dir,
            host=host,
            before=window_start,
            exclude_id=incident_id,
        )

        # Likely causes. Now sees the full evidence set: timeline
        # (with derived events), the diff, and the recurrence lookup.
        causes = cause_mod.rank(
            triggers,
            timeline,
            incident_diff=incident_diff,
            recurrence_lookup=recurrence_lookup,
            fingerprint_id=fp.fingerprint_id,
        )
        causes = _apply_capture_quality_limit(causes, capture_quality)
        top = causes[0] if causes else None

        # Observer mode plumbing: when the daemon was observing a remote
        # robot, `host` (= socket.gethostname()) is the observer, not
        # the robot. We carry both labels on the bundle so the report
        # and any tooling can avoid mislabeling the source.
        runtime = self._config.runtime
        if runtime.is_observer:
            observer_host: str | None = host
            observed_host: str | None = runtime.observed_host
        else:
            observer_host = None
            observed_host = None

        incident = Incident(
            incident_id=incident_id,
            created_at=datetime.now(timezone.utc),
            window_start=window_start,
            window_end=window_end,
            session_id=sid,
            host=host,
            observer_host=observer_host,
            observed_host=observed_host,
            severity=severity,  # type: ignore[arg-type]
            title=title or _default_title(triggers),
            summary=_summary_line(top, triggers),
            bundle_path=str(bundle_dir),
            tags=list(tags),
            triggers=[t.trigger_id for t in triggers],
            fingerprint=fp,
            likely_causes=causes,
            capture_quality=capture_quality,
            notes=notes,
        )

        # Write evidence first; report renders from the on-disk state.
        writer.write_events_jsonl(events)
        writer.write_triggers(triggers)
        writer.write_snapshots(snapshots)
        writer.write_signatures(config_sig, version_sig)
        writer.write_diff(incident_diff)
        writer.write_timeline(timeline)
        writer.write_fingerprint(fp)
        writer.write_incident(incident)
        for source, relative in native_files:
            destination = staging_dir / "attachments" / "native_capture" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        # Render report from the disk view to keep it consistent with what
        # `incident show` will load later. Strict=False because report.md
        # itself does not exist yet at this point in the build pipeline.
        report_text = report_mod.render(BundleReader(staging_dir, strict=False))
        writer.write_report(report_text)

        manifest = writer.build_manifest(
            incident_id=incident.incident_id,
            created_at=incident.created_at,
        )
        writer.write_manifest(manifest)
        result = writer.validate(require_finalized=True)
        if result.errors:
            details = "; ".join(
                f"{issue.code}:{issue.path or '-'}:{issue.message}" for issue in result.errors
            )
            raise RuntimeError(
                f"Incident bundle finalization failed; staging preserved at "
                f"{staging_dir}: {details}"
            )

        self._publish_staging(staging_dir, bundle_dir)
        logger.info("Built incident %s at %s", incident_id, bundle_dir)
        return bundle_dir

    def _staging_dir_for(self, incident_id: str) -> Path:
        suffix = hashlib.sha256(
            f"{incident_id}|{datetime.now(timezone.utc).isoformat()}|{os.getpid()}".encode("utf-8")
        ).hexdigest()[:8]
        return self._incidents_dir / f".{incident_id}.staging.{suffix}"

    def _publish_staging(self, staging_dir: Path, bundle_dir: Path) -> None:
        """Publish a validated staging directory.

        First publication uses ``os.replace`` on the same parent directory.
        Rebuilding an existing deterministic incident id falls back to
        moving the old directory aside before publishing the new one. This is
        not an atomic non-empty directory swap on POSIX, but the old completed
        bundle is restored if the final publish step fails.
        """
        if not bundle_dir.exists():
            os.replace(staging_dir, bundle_dir)
            return

        backup_dir = bundle_dir.with_name(f".{bundle_dir.name}.replacing.{os.getpid()}")
        while backup_dir.exists():
            backup_dir = backup_dir.with_name(f"{backup_dir.name}.retry")
        os.replace(bundle_dir, backup_dir)
        try:
            os.replace(staging_dir, bundle_dir)
        except Exception:
            if not bundle_dir.exists() and backup_dir.exists():
                os.replace(backup_dir, bundle_dir)
            raise
        try:
            shutil.rmtree(backup_dir)
        except OSError:
            logger.warning("Could not remove replaced incident backup %s", backup_dir)

    # -- diff baseline lookup --------------------------------------------

    def _load_prior_signatures(
        self,
        *,
        host: str,
        before: datetime,
        exclude_id: str,
    ) -> tuple[Any, Any]:
        """Find the most recent prior bundle on *host* and return its sigs.

        "Prior" means: same host, ``window_end < before``, and not the
        bundle currently being built (``exclude_id``). Returns
        ``(None, None)`` when no candidate exists, or when the candidate
        is unreadable. Builder failure must never block a new bundle on
        a missing baseline.
        """
        try:
            entries = sorted(
                p for p in self._incidents_dir.iterdir() if p.is_dir() and p.name != exclude_id
            )
        except (OSError, FileNotFoundError):
            return None, None

        best: tuple[datetime, Path] | None = None
        for path in entries:
            try:
                reader = BundleReader(path, strict=False)
                inc = reader.load_incident()
            except Exception:
                continue
            if inc.host and inc.host != host:
                continue
            if inc.window_end >= before:
                continue
            if best is None or inc.window_end > best[0]:
                best = (inc.window_end, path)

        if best is None:
            return None, None
        try:
            cfg, ver = BundleReader(best[1], strict=False).load_signatures()
            return cfg, ver
        except Exception:
            return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_title(triggers: list[DetectorTrigger]) -> str:
    if not triggers:
        return "Manual incident (no detector triggers)"
    head = triggers[0]
    short = head.detector_class.rsplit(".", 1)[-1]
    return f"{short} on {head.subject}"


def _session_from_events(events: list[BlackBoxEvent]) -> str | None:
    for ev in events:
        sid = ev.metadata.get("session_id")
        if sid:
            return str(sid)
    return None
