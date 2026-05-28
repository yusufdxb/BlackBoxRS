"""System monitor for BlackBoxRS.

Orchestrates metric collection across CPU, memory, GPU, disk, and thermal
collectors and publishes results as ``BlackBoxEvent`` instances on the
project's event bus.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from blackboxrs.core.clock import Clock
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session

from blackboxrs.system_monitor.collectors.clock import ClockSkewCollector
from blackboxrs.system_monitor.collectors.cpu import CpuCollector
from blackboxrs.system_monitor.collectors.disk import DiskCollector
from blackboxrs.system_monitor.collectors.gpu import GpuCollector
from blackboxrs.system_monitor.collectors.memory import MemoryCollector
from blackboxrs.system_monitor.collectors.thermal import ThermalCollector

logger = logging.getLogger(__name__)


class SystemMonitor:
    """Collects system metrics at configurable intervals and publishes them as BlackBoxEvents.

    Usage::

        monitor = SystemMonitor(event_bus, config.system_monitor, session)
        monitor.start()   # spawns a daemon thread
        ...
        monitor.stop()    # signals the thread to exit
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: Any,
        session: Session,
    ) -> None:
        """Initialise the system monitor.

        Args:
            event_bus: The event bus to publish metric events onto.
            config: A ``BlackBoxConfig.system_monitor`` object with at least
                    ``enabled``, ``interval_sec``, and ``gpu_backend`` attrs.
            session: The active session, used to attach metadata to events.
        """
        self._event_bus = event_bus
        self._config = config
        self._session = session
        self._collectors: list[tuple[str, Any]] = self._init_collectors()
        self._clock_collector: ClockSkewCollector | None = self._init_clock_collector()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Collector initialisation
    # ------------------------------------------------------------------

    def _init_collectors(self) -> list[tuple[str, Any]]:
        """Initialize all available collectors.

        Returns an empty list when the monitor is disabled (typical in
        observer mode, where the collectors would describe the
        observer host instead of the robot) so we don't import psutil
        or probe GPU backends only to throw the results away.
        """
        if not getattr(self._config, "enabled", True):
            return []

        collectors: list[tuple[str, Any]] = [
            ("cpu", CpuCollector()),
            ("memory", MemoryCollector()),
            ("disk", DiskCollector()),
            ("thermal", ThermalCollector()),
        ]

        gpu_backend = getattr(self._config, "gpu_backend", "auto")
        gpu = GpuCollector(backend=gpu_backend)
        if gpu.available:
            collectors.append(("gpu", gpu))
        else:
            logger.info("No GPU backend available — GPU metrics disabled")

        return collectors

    def _init_clock_collector(self) -> ClockSkewCollector | None:
        """Initialise the clock-skew producer if configured.

        The clock-skew producer is kept separate from the regular
        ``(name, collector)`` list because it publishes directly to the
        event bus (its event shape is detector-specific) rather than
        returning a plain dict for the generic ``system.<name>`` wrapper.

        The producer runs in both onboard and observer mode.  In observer
        mode the ``system`` and ``ntp:*`` sources describe the observer
        workstation's clock; ``ros:/clock`` describes the robot's sim
        clock.  See ``docs/design/orphan_detector_producers.md`` §2.5.
        """
        clock_cfg = getattr(self._config, "clock", None)
        if clock_cfg is None or not getattr(clock_cfg, "enabled", True):
            logger.info("ClockSkewCollector: disabled by configuration")
            return None

        # Derive the read-window budget from the anomaly detector config.
        # We import lazily to avoid a heavy dependency chain at module load.
        try:
            from blackboxrs.core.config import ClockSkewConfig

            max_skew_sec = ClockSkewConfig().max_skew_sec
        except Exception:
            max_skew_sec = 0.1  # safe fallback
        budget_sec = max_skew_sec / 2.0

        collector = ClockSkewCollector(
            config=clock_cfg,
            event_bus=self._event_bus,
            session_meta=self._session.metadata(),
        )
        collector.set_window_budget(budget_sec)
        logger.info("ClockSkewCollector: initialised (budget=%.1f ms)", budget_sec * 1000)
        return collector

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin collecting metrics in a background daemon thread.

        Does nothing if the monitor is already running or if system
        monitoring is disabled in the configuration.
        """
        if not getattr(self._config, "enabled", True):
            logger.info("System monitor is disabled by configuration")
            return

        if self._running:
            logger.warning("SystemMonitor.start() called but already running")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run,
            name="blackbox-system-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "System monitor started (interval=%ss)", self._config.interval_sec
        )

    def stop(self) -> None:
        """Stop the collection loop and wait for the thread to exit.

        Safe to call multiple times.
        """
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=self._config.interval_sec + 2)
            self._thread = None

        if self._clock_collector is not None:
            self._clock_collector.close()

        logger.info("System monitor stopped")

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _collect_once(self) -> list[BlackBoxEvent]:
        """Run all collectors and return a list of BlackBoxEvents.

        Each collector produces one event.  Collectors that fail or
        return empty data are skipped silently (the error is logged).
        Collectors that return a list (e.g. the thermal-zone collector)
        are wrapped under a single ``"items"`` key so the resulting
        ``data`` payload always satisfies the ``dict[str, Any]`` schema
        contract on :class:`BlackBoxEvent`.

        Returns:
            A list of ``BlackBoxEvent`` instances ready for publishing.
        """
        events: list[BlackBoxEvent] = []
        session_meta = self._session.metadata()

        for name, collector in self._collectors:
            try:
                raw = collector.collect()
            except Exception:
                logger.exception("Collector %r raised an exception", name)
                continue

            if raw is None:
                continue
            if isinstance(raw, dict):
                if not raw:
                    continue
                data: dict[str, Any] = raw
            elif isinstance(raw, list):
                if not raw:
                    continue
                data = {"items": raw}
            else:
                logger.warning(
                    "Collector %r returned unsupported type %s; skipping",
                    name,
                    type(raw).__name__,
                )
                continue

            event = BlackBoxEvent.system_event(
                event_type=f"system.{name}",
                data=data,
                severity="info",
                **session_meta,
            )
            events.append(event)

        return events

    def run(self) -> None:
        """Blocking run loop intended to be called from a dedicated thread.

        Collects metrics at the configured interval and publishes each
        resulting event to the event bus.  Exits when :meth:`stop` is called.
        """
        interval: float = getattr(self._config, "interval_sec", 5.0)
        logger.debug("Monitor run loop starting (interval=%.1fs)", interval)

        while not self._stop_event.is_set():
            start = time.monotonic()

            try:
                events = self._collect_once()
                for event in events:
                    self._event_bus.publish(event)
                if self._clock_collector is not None:
                    self._clock_collector.tick()
            except Exception:
                logger.exception("Unhandled error in monitor run loop")

            elapsed = time.monotonic() - start
            remaining = max(0.0, interval - elapsed)
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    # ------------------------------------------------------------------
    # Snapshot (for CLI / status queries)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return the current system state as a flat dictionary.

        This runs every collector synchronously and merges the results into
        a single dict, suitable for display in a CLI status command.

        Returns:
            A merged dictionary of all collector outputs.  Keys are prefixed
            by collector name where collisions would occur (e.g. GPU metrics
            already carry a ``gpu_`` prefix).
        """
        merged: dict[str, Any] = {}

        for name, collector in self._collectors:
            try:
                data = collector.collect()
            except Exception:
                logger.exception("Snapshot: collector %r failed", name)
                continue

            if data is None:
                continue

            if isinstance(data, dict):
                merged.update(data)
            elif isinstance(data, list):
                merged[name] = data

        merged["timestamp"] = Clock.format_iso(Clock.now())
        merged.update(self._session.metadata())
        return merged
