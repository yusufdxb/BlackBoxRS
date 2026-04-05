"""BlackBoxRS daemon — orchestrates all monitoring components.

The :class:`BlackBoxDaemon` wires together the event bus, monitors, anomaly
engine, and logging pipeline, running each in its own thread.  It manages
PID-file lifecycle and signal handling for clean startup/shutdown.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from threading import Event, Thread
from typing import Any, Protocol

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight protocol so we can treat all components uniformly
# ---------------------------------------------------------------------------


class _Component(Protocol):
    """Minimal interface that every BlackBoxRS component exposes."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def run(self) -> None: ...


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class BlackBoxDaemon:
    """Main daemon that orchestrates all BlackBoxRS components.

    Each enabled component is started in its own daemon thread.  The
    daemon writes a PID file to ``~/.blackboxrs/blackboxrs.pid`` so that
    the CLI can discover and manage a running instance.

    Args:
        config: Fully resolved BlackBoxRS configuration.
    """

    _PID_DIR = Path("~/.blackboxrs").expanduser()
    _PID_FILE = _PID_DIR / "blackboxrs.pid"

    def __init__(self, config: BlackBoxConfig) -> None:
        self._config = config
        self._session = Session()
        self._event_bus = EventBus()
        self._components: list[_Component] = []
        self._threads: list[Thread] = []
        self._running = False
        self._stop_event = Event()

    # -- Properties ---------------------------------------------------------

    @property
    def session(self) -> Session:
        """The active session for this daemon run."""
        return self._session

    @property
    def config(self) -> BlackBoxConfig:
        """The daemon's active configuration."""
        return self._config

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start all enabled components in their own daemon threads.

        The logging pipeline is always started.  ROS monitor, system
        monitor, and anomaly engine are started only when their
        respective ``enabled`` flag is ``True`` in the configuration.

        Raises:
            RuntimeError: If the daemon is already running.
        """
        if self._running:
            raise RuntimeError("BlackBoxDaemon is already running")

        self._running = True
        self._stop_event.clear()

        # --- Logging pipeline (always enabled) ----------------------------
        from blackboxrs.logging import LoggingPipeline  # noqa: E402

        log_pipeline = LoggingPipeline(self._event_bus, self._config, self._session)
        self._register(log_pipeline, name="logging_pipeline")

        # --- ROS monitor --------------------------------------------------
        if self._config.ros_monitor.enabled:
            from blackboxrs.ros_monitor import RosMonitor  # noqa: E402

            ros_mon = RosMonitor(
                self._event_bus, self._config.ros_monitor, self._session
            )
            self._register(ros_mon, name="ros_monitor")

        # --- System monitor -----------------------------------------------
        if self._config.system_monitor.enabled:
            from blackboxrs.system_monitor import SystemMonitor  # noqa: E402

            sys_mon = SystemMonitor(
                self._event_bus, self._config.system_monitor, self._session
            )
            self._register(sys_mon, name="system_monitor")

        # --- Anomaly engine -----------------------------------------------
        if self._config.anomaly_engine.enabled:
            from blackboxrs.anomaly_engine import AnomalyEngine  # noqa: E402

            engine = AnomalyEngine(
                self._event_bus, self._config.anomaly_engine, self._session
            )
            self._register(engine, name="anomaly_engine")

        # --- PID file -----------------------------------------------------
        self._write_pid_file()

        # --- Signal handlers (only useful when running in foreground) -----
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (OSError, ValueError):
            # Cannot set signal handlers from a non-main thread; ignore.
            pass

        logger.info(
            "BlackBoxDaemon started  session=%s  pid=%d  components=%d",
            self._session.session_id,
            os.getpid(),
            len(self._components),
        )

    def stop(self) -> None:
        """Gracefully shut down all components and clean up.

        Stops every component, joins each thread (with a timeout), and
        removes the PID file.
        """
        if not self._running:
            return

        logger.info("BlackBoxDaemon stopping ...")
        self._running = False
        self._stop_event.set()

        # Stop components in reverse startup order
        for component in reversed(self._components):
            try:
                component.stop()
            except Exception:
                logger.exception("Error stopping component %s", type(component).__name__)

        for thread in self._threads:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("Thread %s did not terminate in time", thread.name)

        self._remove_pid_file()
        logger.info("BlackBoxDaemon stopped")

    def wait(self) -> None:
        """Block the calling thread until the daemon is stopped.

        Typically called from the main thread after :meth:`start` so
        that the process stays alive until a signal arrives.
        """
        self._stop_event.wait()

    # -- Class-level helpers ------------------------------------------------

    @classmethod
    def is_running(cls) -> tuple[bool, int | None]:
        """Check whether a BlackBoxRS daemon is currently running.

        Reads the PID file and verifies the process is alive via
        ``os.kill(pid, 0)``.

        Returns:
            A tuple of ``(is_running, pid)``.  *pid* is ``None`` when
            no PID file exists or the process is not alive.
        """
        pid_file = cls._PID_FILE
        if not pid_file.is_file():
            return False, None

        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return False, None

        try:
            os.kill(pid, 0)  # Signal 0: existence check, no actual signal sent
        except ProcessLookupError:
            # Stale PID file — process is gone
            cls._cleanup_stale_pid(pid_file)
            return False, None
        except PermissionError:
            # Process exists but we lack permissions (unusual, treat as running)
            return True, pid

        return True, pid

    @classmethod
    def stop_running(cls) -> None:
        """Send ``SIGTERM`` to a running BlackBoxRS daemon.

        Raises:
            RuntimeError: If no daemon is running.
            OSError: If the signal cannot be delivered.
        """
        running, pid = cls.is_running()
        if not running or pid is None:
            raise RuntimeError("No running BlackBoxRS daemon found")

        os.kill(pid, signal.SIGTERM)

    # -- Internal helpers ---------------------------------------------------

    def _register(self, component: _Component, *, name: str) -> None:
        """Register and start a component in a new daemon thread."""
        component.start()
        self._components.append(component)

        thread = Thread(target=self._run_component, args=(component,), name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _run_component(self, component: _Component) -> None:
        """Thread target that runs a component's main loop."""
        try:
            component.run()
        except Exception:
            logger.exception(
                "Component %s crashed", type(component).__name__
            )

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM by initiating a graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", sig_name)
        self.stop()

    def _write_pid_file(self) -> None:
        """Write the current process PID to the PID file."""
        self._PID_DIR.mkdir(parents=True, exist_ok=True)
        self._PID_FILE.write_text(str(os.getpid()))

    def _remove_pid_file(self) -> None:
        """Remove the PID file if it exists."""
        try:
            self._PID_FILE.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove PID file", exc_info=True)

    @staticmethod
    def _cleanup_stale_pid(pid_file: Path) -> None:
        """Remove a stale PID file left over from a previous run."""
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
