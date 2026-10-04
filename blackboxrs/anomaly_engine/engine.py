"""Anomaly detection engine for BlackBoxRS.

Consumes the full event stream from the :class:`EventBus`, runs each
event through the registered detectors, and publishes any resulting
anomaly events back onto the bus.
"""

from __future__ import annotations

import logging
import threading
from queue import Empty, Queue

from blackboxrs.core.config import AnomalyEngineConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session

from .detectors import (
    BaseDetector,
    ClockSkewDetector,
    DeadTopicDetector,
    FrequencyDetector,
    ProcessSignalsDetector,
    QoSMismatchDetector,
    ThresholdDetector,
    TfTopologyDetector,
)

# ProcessSignalsDetector is now wired in (commit that lands
# ProcessSignalsCollector in system_monitor/collectors/process.py).
# The producer auto-disables in observer mode (it would otherwise sample
# the observer workstation's processes, not the robot's). The detector
# itself is NOT gated behind observer_mode: it simply receives no events
# when the producer is off, which is the correct v1 behaviour.
# See docs/design/orphan_detector_producers.md §3.5.
#
# ClockSkewDetector was re-wired now that ClockSkewCollector
# (blackboxrs/system_monitor/collectors/clock.py) ships as the live
# producer of `system.clock_skew` events. It is partially DDS-bound
# (ros:/clock source) and observer-mode-compatible (see §2.5).
#
# TfTopologyDetector was re-wired in commit 1dcd224 now that
# TfSnapshotter (commits 0ec7a41, 1481949) ships as the live producer
# of `ros.tf` events. It is DDS-bound and observer-mode-compatible;
# see §1.5.
#
# All three previously-orphaned detectors are now wired. The orphan set
# is empty for the first time since the observer-mode pivot.

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SEC = 0.25
_PROTECTED_QUEUE_FACTOR = 4
_PROTECTED_QUEUE_MIN = 4096


class AnomalyEngine:
    """Consumes events from the bus, runs them through detectors,
    and publishes anomaly events back to the bus.

    The engine subscribes to *all* channels on the event bus.  For
    every incoming event each registered :class:`BaseDetector` is
    invoked; if a detector returns a new event that event is published
    to the bus so that the logging pipeline (and any other subscribers)
    can capture it.

    Args:
        event_bus: The shared :class:`EventBus` instance.
        config: An :class:`AnomalyEngineConfig` with detector settings.
        session: The active :class:`Session` whose metadata is attached
            to emitted anomaly events.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: AnomalyEngineConfig,
        session: Session,
    ) -> None:
        self._event_bus = event_bus
        self._config = config
        self._session = session
        self._detectors: list[BaseDetector] = self._init_detectors()
        self._running = False
        self._queue: Queue[BlackBoxEvent] | None = None
        self._thread: threading.Thread | None = None

    # -- Setup ----------------------------------------------------------------

    def _init_detectors(self) -> list[BaseDetector]:
        """Instantiate all detectors from the current configuration.

        Returns:
            A list of ready-to-use detector instances.
        """
        # Only detectors with live producers in this codebase are wired
        # in. See the comment at the top of the file for the two that
        # are intentionally absent until their producers ship.
        detectors: list[BaseDetector] = [
            ThresholdDetector(self._config.thresholds),
            FrequencyDetector(self._config.frequency),
            QoSMismatchDetector(),
            DeadTopicDetector(self._config.dead_topic),
            # TfTopologyDetector is DDS-bound (consumes `ros.tf` events
            # emitted by TfSnapshotter over the shared EventBus) and is
            # therefore observer-mode-compatible: no host-bound
            # resources are read. It runs in both onboard and observer
            # mode with the same detector instance.
            TfTopologyDetector(self._config.tf_topology),
            # ClockSkewDetector consumes `system.clock_skew` events
            # emitted by ClockSkewCollector.  In observer mode the
            # `system` and `ntp:*` sources are observer-relative; the
            # `ros:/clock` source is robot-relative over DDS.  The
            # detector is DDS-compatible and runs in both modes.
            ClockSkewDetector(self._config.clock_skew),
            # ProcessSignalsDetector is host-bound in v1: the producer
            # (ProcessSignalsCollector) auto-disables in observer mode so
            # no `system.process_signals` events reach the bus. The
            # detector is kept wired in both modes so it fires correctly
            # when running onboard. In observer mode it is effectively
            # dormant (no events to consume).
            ProcessSignalsDetector(self._config.process_signals),
        ]
        if getattr(self._config, "observer_mode", False):
            # ProcessSignalsDetector is wired but dormant: its producer
            # auto-disables in observer mode (psutil would report the
            # observer workstation's processes, not the robot's).
            # ThresholdDetector checks host CPU/memory: those numbers
            # describe the observer, not the robot, but it is left
            # wired until a separate host-bound gating pass is added.
            # TfTopologyDetector and ClockSkewDetector are DDS-bound
            # and remain active in both modes.
            logger.info(
                "anomaly_engine: observer_mode active "
                "(process_signals producer auto-disabled; "
                "detector wired but dormant; "
                "tf_topology and clock_skew are DDS-compatible and active)."
            )
        if self._config.custom_detectors:
            from .detectors.loader import load_custom_detectors

            custom = load_custom_detectors(self._config.custom_detectors)
            detectors.extend(custom)
        logger.info(
            "Initialised %d anomaly detectors: %s",
            len(detectors),
            ", ".join(d.name for d in detectors),
        )
        return detectors

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the engine in a background daemon thread.

        Subscribes to the event bus and begins draining events.  Calling
        :meth:`start` on an already-running engine is a no-op.
        """
        if self._running:
            logger.warning("AnomalyEngine.start() called but already running")
            return

        queue_size = max(
            _PROTECTED_QUEUE_MIN,
            self._event_bus.default_queue_maxsize * _PROTECTED_QUEUE_FACTOR,
        )
        self._queue = self._event_bus.subscribe(maxsize=queue_size)
        self._running = True
        self._thread = threading.Thread(
            target=self.run,
            name="anomaly-engine",
            daemon=True,
        )
        self._thread.start()
        logger.info("Anomaly engine started")

    def stop(self) -> None:
        """Signal the engine to stop and wait for the background thread.

        Safe to call even if the engine is not running.
        """
        if not self._running:
            return

        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._queue is not None:
            self._event_bus.unsubscribe(self._queue)
            self._queue = None

        logger.info("Anomaly engine stopped")

    def run(self) -> None:
        """Blocking loop: drain events from the subscription and run detectors.

        This method is intended to be executed in a dedicated thread
        (see :meth:`start`).  It loops until :attr:`_running` is set to
        ``False``, pulling events from the subscription queue with a
        short timeout so that the stop signal is checked regularly.
        """
        if self._queue is None:
            logger.error("run() called without an active subscription")
            return

        logger.debug("Anomaly engine run-loop entered")

        while self._running:
            try:
                event = self._queue.get(timeout=_DRAIN_TIMEOUT_SEC)
            except Empty:
                continue

            # Skip anomaly events to prevent feedback loops.
            if event.source == "anomaly_engine":
                continue

            for detector in self._detectors:
                try:
                    anomaly = detector.check(event)
                except Exception:
                    logger.exception(
                        "Detector %s raised an unexpected error",
                        detector.name,
                    )
                    continue

                if anomaly is not None:
                    # Enrich with session metadata.
                    anomaly.metadata.update(self._session.metadata())
                    self._event_bus.publish(anomaly)

        logger.debug("Anomaly engine run-loop exited")
