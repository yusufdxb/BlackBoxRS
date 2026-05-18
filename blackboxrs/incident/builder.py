"""IncidentBuilder: turns log slices into an evidence bundle.

This is the orchestrator. Stages from ``ARCHITECTURE_PIVOT.md`` §2.2 are
each delegated to a small module so they can be tested in isolation.
"""

from __future__ import annotations

import hashlib
import logging
import os
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

from . import cause as cause_mod
from . import diff as diff_mod
from . import fingerprint as fingerprint_mod
from . import recurrence as recurrence_mod
from . import report as report_mod
from . import timeline as timeline_mod
from .bundle import BundleReader, BundleWriter
from .models import (
    DetectorTrigger,
    Incident,
    SystemSnapshot,
)


logger = logging.getLogger(__name__)


_DEFAULT_INCIDENTS_DIR = Path("~/.blackboxrs/incidents").expanduser()


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


_VALID_SUBSYSTEMS = {
    "ros", "system", "gpu", "anomaly", "recorder", "config", "external"
}


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
        ev.data.get("topic")
        or ev.data.get("metric")
        or ev.data.get("subject")
        or ev.event_type
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
        severity=ev.severity if ev.severity in ("info", "warning", "error", "critical") else "warning",
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
        self._incidents_dir = Path(
            incidents_dir or os.path.expanduser(str(_DEFAULT_INCIDENTS_DIR))
        )
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
        writer = BundleWriter(bundle_dir)

        # Diff against the most recent prior bundle on this host (if any).
        # We compute this BEFORE ranking causes so the precursor-aware
        # ranker can use config/version drift as a (coincident) signal.
        prior_cfg, prior_ver = self._load_prior_signatures(
            host=host, before=window_start, exclude_id=incident_id,
        )
        incident_diff = diff_mod.compute(
            prior_cfg, prior_ver, config_sig, version_sig
        )

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

        # Render report from the disk view to keep it consistent with what
        # `incident show` will load later. Strict=False because report.md
        # itself does not exist yet at this point in the build pipeline.
        report_text = report_mod.render(BundleReader(bundle_dir, strict=False))
        writer.write_report(report_text)

        logger.info("Built incident %s at %s", incident_id, bundle_dir)
        return bundle_dir

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
                p for p in self._incidents_dir.iterdir()
                if p.is_dir() and p.name != exclude_id
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
