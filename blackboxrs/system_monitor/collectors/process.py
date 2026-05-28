"""Per-process CPU/RSS signals producer for BlackBoxRS.

Samples all processes whose cmdline matches one of the configured glob
patterns (default: ROS 2 and controller-related patterns), emits one
``system.process_signals`` event per tick for the
:class:`ProcessSignalsDetector` to evaluate.

Pattern matching is against the full command-line string (joined with
spaces), not just the process name.  This means ``*ros2*`` catches both
``ros2 run foo bar`` and ``python3 -m ros2_pkg.node``.

In observer mode (``runtime.role="observer"``) this producer auto-disables
at startup with one INFO log line.  ``psutil`` reports the observer
workstation's processes, not the robot's.  A DDS bridge for remote process
signals is deferred to v2 — see ``docs/design/orphan_detector_producers.md``
§3.5 for the full discussion.

The collector is implemented as a standalone class with a synchronous
``tick()`` method so it can be driven by the ``SystemMonitor`` run-loop or
by test harnesses without spawning threads.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blackboxrs.core.config import ProcessSignalsCollectorConfig

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False
    logger.warning("psutil not installed — ProcessSignalsCollector will be a no-op")


# ---------------------------------------------------------------------------
# Pure-function helpers (testable without real processes)
# ---------------------------------------------------------------------------


def cmdline_matches_patterns(cmdline_parts: list[str], patterns: list[str]) -> bool:
    """Return True if the joined cmdline matches any of the glob patterns.

    Matching is case-sensitive and uses ``fnmatch``.  The pattern is
    tested against the full cmdline string (all argv parts joined by a
    space) so that ``*ros2*`` catches both ``ros2 run pkg node`` and
    ``python3 -m ros2_pkg.node``.

    Args:
        cmdline_parts: The ``proc.cmdline()`` list returned by psutil.
        patterns: A list of glob patterns (e.g. ``["*ros2*", "*nav2*"]``).

    Returns:
        ``True`` if the full cmdline matches at least one pattern.
    """
    if not cmdline_parts:
        return False
    full_cmd = " ".join(cmdline_parts)
    return any(fnmatch.fnmatch(full_cmd, pat) for pat in patterns)


def proc_to_dict(proc: object) -> dict | None:
    """Convert a ``psutil.Process`` to a snapshot dict.

    Collects ``pid``, ``name``, ``cpu_percent(interval=None)``, and
    ``memory_info().rss`` from ``proc``.  Returns ``None`` if any
    required attribute raises (``AccessDenied``, ``NoSuchProcess``,
    etc.) or if ``cpu_percent`` is 0.0 (first-sample warm-up artifact).

    The caller is responsible for seeding the cpu_percent baseline on
    startup; the first call per new pid always returns 0.0.  Those
    entries are silently dropped here per spec §3.7 to avoid flooding
    the detector with "everything is at 0%."

    Args:
        proc: A ``psutil.Process`` instance (typed as ``object`` to
            avoid a hard import at function-definition time in tests).

    Returns:
        ``{"pid": int, "name": str, "cpu_percent": float, "rss_mb": float}``
        or ``None`` if the entry should be dropped.
    """
    try:
        pid: int = proc.pid  # type: ignore[union-attr]
        name: str = proc.name()  # type: ignore[union-attr]
        cpu: float = proc.cpu_percent(interval=None)  # type: ignore[union-attr]
        rss_bytes: int = proc.memory_info().rss  # type: ignore[union-attr]
    except Exception:
        # AccessDenied, NoSuchProcess, ZombieProcess — swallow silently
        return None

    if cpu == 0.0:
        # First-call warm-up artifact; caller already seeded the baseline,
        # so this is either a brand-new pid or a stale reading.  Drop it.
        return None

    return {
        "pid": pid,
        "name": name,
        "cpu_percent": round(float(cpu), 2),
        "rss_mb": round(float(rss_bytes) / (1024 * 1024), 2),
    }


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class ProcessSignalsCollector:
    """Samples ROS 2 / controller process CPU and RSS and emits events.

    On each :meth:`tick`, iterates over running processes whose cmdline
    matches one of the configured glob patterns, collects CPU% (delta-based
    via ``psutil``) and RSS, and emits a single ``system.process_signals``
    event for the :class:`ProcessSignalsDetector`.

    First-call CPU% readings (always 0.0) are dropped; the collector seeds
    the baseline at startup by walking the matched process set without
    emitting.

    In observer mode (``runtime.role == "observer"``) the collector
    disables itself at construction time with one INFO log.  psutil reports
    the observer workstation's processes; remote process enumeration is out
    of scope for v1.

    Args:
        config: A :class:`ProcessSignalsCollectorConfig` instance.
        event_bus: The shared event bus; ``tick()`` publishes directly.
        session_meta: Flat dict of session-level metadata attached to every
            event (``session_id``, ``hostname``, ``role``, etc.).
        is_observer: When ``True`` the collector disables immediately.
    """

    def __init__(
        self,
        config: ProcessSignalsCollectorConfig,
        event_bus: object,
        session_meta: dict,
        *,
        is_observer: bool = False,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._session_meta = session_meta
        self._disabled = False

        if is_observer:
            logger.info(
                "ProcessSignalsCollector: disabled in observer mode "
                "(psutil reports the observer host, not the robot; "
                "see docs/design/orphan_detector_producers.md §3.5)."
            )
            self._disabled = True
            return

        if not _HAS_PSUTIL:  # pragma: no cover
            logger.warning(
                "ProcessSignalsCollector: psutil not available — disabled."
            )
            self._disabled = True
            return

        # Seed the per-process cpu_percent baseline so that the first real
        # tick gets non-zero readings instead of all-zeros warm-up noise.
        self._seed_baseline()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seed_baseline(self) -> None:
        """Walk the matched process set once to prime the cpu_percent delta.

        Calling ``proc.cpu_percent(interval=None)`` for the first time on
        a given ``Process`` object always returns 0.0; subsequent calls
        return the delta since the previous call.  We call it once here at
        startup so the first :meth:`tick` gets real readings.
        """
        try:
            matched = self._iter_matched_procs()
            for proc in matched:
                try:
                    proc.cpu_percent(interval=None)
                except Exception:
                    pass
            logger.debug(
                "ProcessSignalsCollector: baseline seeded (%d matched pids)",
                len(matched),
            )
        except Exception:
            logger.debug("ProcessSignalsCollector: baseline seed failed", exc_info=True)

    def _iter_matched_procs(self) -> list:
        """Return a list of psutil.Process objects whose cmdline matches.

        Caps the result at ``config.max_tracked`` entries.  Processes that
        raise on ``cmdline()`` access (``AccessDenied``) are skipped
        silently.

        Returns:
            A list of matched :class:`psutil.Process` instances.
        """
        patterns: list[str] = self._config.tracked_patterns
        max_tracked: int = self._config.max_tracked
        matched: list = []

        for proc in psutil.process_iter():
            if len(matched) >= max_tracked:
                break
            try:
                cmdline_parts = proc.cmdline()
            except Exception:
                # AccessDenied on Linux for other-user processes — skip
                continue
            if cmdline_matches_patterns(cmdline_parts, patterns):
                matched.append(proc)

        return matched

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def tick(self) -> dict | None:
        """Sample matched processes and emit one ``system.process_signals`` event.

        Drops first-sample (cpu_percent==0.0) entries, dead processes, and
        processes that raise on attribute access.  If the resulting snapshot
        is empty, no event is emitted.

        Returns:
            The ``data`` dict published to the bus (for testing convenience),
            or ``None`` if disabled or no matching processes were found.
        """
        if self._disabled:
            return None

        from blackboxrs.core.schemas import BlackBoxEvent

        import time

        t_start = time.monotonic()

        try:
            matched_procs = self._iter_matched_procs()
        except Exception:
            logger.debug("ProcessSignalsCollector: process_iter failed", exc_info=True)
            return None

        snapshot: list[dict] = []
        for proc in matched_procs:
            entry = proc_to_dict(proc)
            if entry is not None:
                snapshot.append(entry)

        if not snapshot:
            logger.debug(
                "ProcessSignalsCollector: matched set empty after filtering — "
                "skipping emission"
            )
            return None

        elapsed = time.monotonic() - t_start
        data: dict = {
            "sampling_interval_sec": round(elapsed, 4),
            "processes": snapshot,
        }

        event = BlackBoxEvent.system_event(
            event_type="system.process_signals",
            data=data,
            severity="info",
            **self._session_meta,
        )
        self._event_bus.publish(event)  # type: ignore[union-attr]
        logger.debug(
            "ProcessSignalsCollector: emitted snapshot with %d process(es)",
            len(snapshot),
        )
        return data

    def close(self) -> None:
        """No-op; retained for symmetry with other collectors."""
        pass
