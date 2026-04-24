"""Event logging pipeline for BlackBoxRS.

Subscribes to every event on the :class:`EventBus` and persists them
as structured JSONL through the :class:`RotatingJsonlWriter`.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from queue import Empty, Queue

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session

from .writer import RotatingJsonlWriter

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SEC = 0.25
_PROTECTED_QUEUE_FACTOR = 4
_PROTECTED_QUEUE_MIN = 4096


class LoggingPipeline:
    """Subscribes to all events on the bus and writes them to structured logs.

    The pipeline runs in a dedicated daemon thread.  Every event
    published on the bus -- regardless of source or channel -- is
    serialised to JSONL via a :class:`RotatingJsonlWriter`.

    Args:
        event_bus: The shared :class:`EventBus` instance.
        config: A :class:`BlackBoxConfig` providing ``log_dir``,
            ``log_rotation_mb``, and ``log_max_files``.
        session: The active :class:`Session` (used for enrichment of
            events that lack session metadata).
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: BlackBoxConfig,
        session: Session,
    ) -> None:
        self._event_bus = event_bus
        self._session = session
        self._writer = RotatingJsonlWriter(
            log_dir=Path(os.path.expanduser(config.log_dir)),
            max_file_mb=config.log_rotation_mb,
            max_files=config.log_max_files,
            max_age_hours=config.log_max_age_hours,
        )
        self._running = False
        self._queue: Queue[BlackBoxEvent] | None = None
        self._thread: threading.Thread | None = None

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the logging pipeline in a background daemon thread.

        Subscribes globally on the event bus and begins draining events.
        Calling :meth:`start` on an already-running pipeline is a no-op.
        """
        if self._running:
            logger.warning("LoggingPipeline.start() called but already running")
            return

        queue_size = max(
            _PROTECTED_QUEUE_MIN,
            self._event_bus.default_queue_maxsize * _PROTECTED_QUEUE_FACTOR,
        )
        self._queue = self._event_bus.subscribe(maxsize=queue_size)
        self._running = True
        self._thread = threading.Thread(
            target=self.run,
            name="logging-pipeline",
            daemon=True,
        )
        self._thread.start()
        logger.info("Logging pipeline started")

    def stop(self) -> None:
        """Signal the pipeline to stop and wait for the background thread.

        Flushes and closes the underlying writer.  Safe to call even if
        the pipeline is not running.
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

        self._writer.close()
        logger.info("Logging pipeline stopped")

    def run(self) -> None:
        """Blocking loop: drain events from the bus and write to log files.

        This method is intended to be executed in a dedicated thread
        (see :meth:`start`).  It loops until :attr:`_running` is set to
        ``False``, pulling events from the subscription queue with a
        short timeout so that the stop signal is checked regularly.

        Events that lack session metadata are enriched automatically.
        """
        if self._queue is None:
            logger.error("run() called without an active subscription")
            return

        logger.debug("Logging pipeline run-loop entered")
        session_meta = self._session.metadata()

        while self._running:
            try:
                event = self._queue.get(timeout=_DRAIN_TIMEOUT_SEC)
            except Empty:
                continue

            # Enrich with session metadata if not already present.
            if "session_id" not in event.metadata:
                event.metadata.update(session_meta)

            try:
                self._writer.write(event)
            except Exception:
                logger.exception("Failed to write event to log")

        logger.debug("Logging pipeline run-loop exited")
