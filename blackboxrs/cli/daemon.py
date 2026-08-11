"""BlackBoxRS daemon — orchestrates all monitoring components.

The :class:`BlackBoxDaemon` wires together the event bus, monitors, anomaly
engine, and logging pipeline.  Components are responsible for managing
their own background threads inside ``start()``; the daemon calls
``start()`` once per component and ``stop()`` to tear them down.  It also
manages PID-file lifecycle and signal handling.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.session import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PID-file identity helpers (Linux-specific; use /proc/<pid>/stat + cmdline)
# ---------------------------------------------------------------------------

# Substring that must appear in the recorded cmdline for the pidfile to
# be accepted as ours.  Short enough to survive quoting/wrapping but
# specific enough to reject unrelated processes with a recycled PID.
_CMDLINE_MARKER = "blackboxrs"


def _read_proc_starttime(pid: int) -> int | None:
    """Return field 22 (``starttime`` in jiffies) of /proc/<pid>/stat.

    The command name is enclosed in parentheses and may contain spaces
    or other whitespace, so we must find the last ``)`` and split the
    remainder — not ``shlex.split`` — to correctly locate subsequent
    fields.

    Returns ``None`` if the process does not exist or /proc is not
    available (e.g. on macOS in test environments).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    except OSError:
        return None

    try:
        rparen = data.rindex(b")")
    except ValueError:
        return None

    fields = data[rparen + 2 :].split()
    # After the closing paren, layout position 3 maps to fields[0], so
    # starttime (layout position 22) is fields[19].
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _read_proc_cmdline(pid: int) -> str | None:
    """Return the cmdline of *pid* as a single space-separated string.

    ``/proc/<pid>/cmdline`` is NUL-separated; we decode and rejoin with
    spaces for human-readable comparison and storage in the pidfile.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _current_process_identity() -> dict[str, Any]:
    """Build the identity payload for the current process."""
    pid = os.getpid()
    return {
        "pid": pid,
        "starttime": _read_proc_starttime(pid),
        "cmdline": _read_proc_cmdline(pid) or "",
    }


# ---------------------------------------------------------------------------
# Lightweight protocol so we can treat all components uniformly
# ---------------------------------------------------------------------------


class _Component(Protocol):
    """Minimal interface that every BlackBoxRS component exposes.

    Implementations own their background thread (if any).  ``start()``
    must be non-blocking and idempotent; ``stop()`` must be safe to call
    even if ``start()`` was never called or has already stopped.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...


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
        # Idempotent: applying the runtime-role policy a second time is a
        # no-op, so this protects callers who construct a daemon from a
        # raw `BlackBoxConfig()` without going through `load()`.
        self._config = config.apply_runtime_role()
        self._session = Session(
            observed_host=self._config.runtime.observed_host,
            role=self._config.runtime.role,
        )
        if self._config.runtime.is_observer:
            logger.info(
                "BlackBoxRS observer mode: observer=%s observed=%s "
                "(host-bound collectors and ProcessSignalsDetector disabled).",
                self._session.hostname,
                self._config.runtime.observed_host or "<unspecified>",
            )
        self._event_bus = EventBus(default_queue_maxsize=self._config.event_bus_queue_maxsize)
        self._components: list[_Component] = []
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
        """Start all enabled components.

        Each component owns its own background thread, started inside
        :meth:`_Component.start`.  The daemon does not spawn additional
        worker threads on top.  The anomaly engine starts before any
        producer so that it observes every event the producers emit.

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

        # Native capture is opt-in. Python monitoring and incident reasoning
        # remain active because they are the intelligence plane.
        if self._config.capture.backend == "cpp":
            from blackboxrs.recording import NativeCaptureProcess  # noqa: E402

            native_capture = NativeCaptureProcess(
                self._config.capture,
                self._config.runtime,
                self._event_bus,
                self._session,
            )
            self._register(native_capture)

        # --- Logging pipeline (always enabled) ----------------------------
        from blackboxrs.logging import LoggingPipeline  # noqa: E402

        log_pipeline = LoggingPipeline(self._event_bus, self._config, self._session)
        self._register(log_pipeline)

        # --- Anomaly engine ------------------------------------------------
        # Start before producers so it sees every event from t=0.
        if self._config.anomaly_engine.enabled:
            from blackboxrs.anomaly_engine import AnomalyEngine  # noqa: E402

            engine = AnomalyEngine(self._event_bus, self._config.anomaly_engine, self._session)
            self._register(engine)

        # --- Rosbag2 recorder --------------------------------------------
        # Start after the anomaly engine so it reacts to emitted anomaly
        # events, but before the producers so it can see the first
        # anomaly the session generates.
        if self._config.rosbag2.enabled:
            from blackboxrs.recording import Rosbag2Recorder  # noqa: E402

            recorder = Rosbag2Recorder(self._event_bus, self._config.rosbag2, self._session)
            self._register(recorder)

        # --- ROS monitor --------------------------------------------------
        if self._config.ros_monitor.enabled:
            from blackboxrs.ros_monitor import RosMonitor  # noqa: E402

            ros_mon = RosMonitor(self._event_bus, self._config.ros_monitor, self._session)
            self._register(ros_mon)

        # --- System monitor -----------------------------------------------
        if self._config.system_monitor.enabled:
            from blackboxrs.system_monitor import SystemMonitor  # noqa: E402

            sys_mon = SystemMonitor(self._event_bus, self._config.system_monitor, self._session)
            self._register(sys_mon)

        # --- Prometheus exporter ------------------------------------------
        if self._config.prometheus.enabled:
            from blackboxrs.metrics import PrometheusExporter  # noqa: E402

            prom = PrometheusExporter(self._event_bus, self._config.prometheus)
            self._register(prom)

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

        # Stop components in reverse startup order; each component is
        # responsible for joining its own thread inside ``stop()``.
        for component in reversed(self._components):
            try:
                component.stop()
            except Exception:
                logger.exception("Error stopping component %s", type(component).__name__)
        self._components.clear()

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
        """Check whether a verified BlackBoxRS daemon is currently running.

        The pidfile is a JSON payload carrying ``pid`` + identity tokens
        (``starttime`` from /proc/<pid>/stat and ``cmdline``).  Identity
        is rechecked every call so we never trust a PID that could have
        been recycled by the kernel after a crashed daemon left its
        pidfile behind.

        Any of the following conditions cause the pidfile to be treated
        as stale (cleaned up, returns ``(False, None)``):

        * pidfile missing or unparseable,
        * process with that PID no longer exists,
        * ``starttime`` from /proc differs from the recorded value
          (PID was recycled by an unrelated process),
        * ``cmdline`` of the live process does not mention
          ``blackboxrs`` (a foreign process holds our PID),
        * legacy plain-integer pidfile with no identity metadata
          (cannot verify → refuse to claim ownership).

        Returns:
            ``(True, pid)`` if the recorded daemon is confirmed alive;
            otherwise ``(False, None)``.
        """
        pid_file = cls._PID_FILE
        if not pid_file.is_file():
            return False, None

        try:
            raw = pid_file.read_text()
        except OSError:
            return False, None

        payload = cls._parse_pidfile(raw)
        if payload is None:
            # Unparseable / legacy plain-int format → cannot verify
            # identity.  Treat as stale and clean up.
            cls._cleanup_stale_pid(pid_file)
            return False, None

        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            cls._cleanup_stale_pid(pid_file)
            return False, None

        # 1. Liveness check.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            cls._cleanup_stale_pid(pid_file)
            return False, None
        except PermissionError:
            # Live but we lack perms.  Without /proc access we cannot
            # verify identity, so we must refuse to claim it.
            logger.warning(
                "PID %d exists but is not accessible; refusing to claim as ours",
                pid,
            )
            return False, None

        # 2. Identity check — starttime is the gold signal against PID
        # recycling.  If the stored starttime is missing (e.g. written
        # on a non-Linux host) we fall back to cmdline-only verification.
        recorded_start = payload.get("starttime")
        live_start = _read_proc_starttime(pid)
        if recorded_start is not None and live_start is not None:
            if int(recorded_start) != int(live_start):
                logger.info(
                    "PID %d recycled since daemon started (starttime "
                    "recorded=%s, live=%s); cleaning stale pidfile",
                    pid,
                    recorded_start,
                    live_start,
                )
                cls._cleanup_stale_pid(pid_file)
                return False, None

        # 3. cmdline sanity-check.  Accept if EITHER the live cmdline
        # carries the blackboxrs marker (production daemon fingerprint)
        # OR it exactly matches the recorded cmdline (round-trip proof
        # that the same process is still running — covers in-process
        # tests and hosts with unusual cmdline shapes).
        live_cmdline = _read_proc_cmdline(pid) or ""
        recorded_cmdline = payload.get("cmdline") or ""
        cmdline_ok = _CMDLINE_MARKER in live_cmdline or (
            recorded_cmdline and live_cmdline == recorded_cmdline
        )
        if not cmdline_ok:
            logger.info(
                "PID %d live cmdline %r does not match recorded %r and "
                "lacks marker %r; refusing to claim",
                pid,
                live_cmdline,
                recorded_cmdline,
                _CMDLINE_MARKER,
            )
            cls._cleanup_stale_pid(pid_file)
            return False, None

        return True, pid

    @classmethod
    def stop_running(cls) -> None:
        """Send ``SIGTERM`` to the *verified* running BlackBoxRS daemon.

        Identity is re-checked inside :meth:`is_running` before the
        signal is delivered, so a pidfile pointing at a recycled or
        foreign PID will raise instead of killing an unrelated process.

        Raises:
            RuntimeError: If no verified daemon is running.
            OSError: If the signal cannot be delivered.
        """
        running, pid = cls.is_running()
        if not running or pid is None:
            raise RuntimeError("No running BlackBoxRS daemon found")

        os.kill(pid, signal.SIGTERM)

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pidfile(raw: str) -> dict[str, Any] | None:
        """Parse a pidfile payload.

        Returns the parsed dict, or ``None`` if the content is not a
        JSON object with at least a ``pid`` integer.  Legacy plain-int
        pidfiles are rejected (``None``) so callers treat them as
        stale — we cannot verify identity without the metadata.
        """
        text = raw.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("pid"), int):
            return None
        return data

    # -- Internal helpers ---------------------------------------------------

    def _register(self, component: _Component) -> None:
        """Start a component and add it to the lifecycle list.

        Components own their background threads inside ``start()``; the
        daemon never spawns a second thread on top.  Earlier versions of
        this code did so, which caused every event to be delivered twice
        for thread-driven components and split the queue between two
        consumers for queue-driven ones.
        """
        component.start()
        self._components.append(component)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM by initiating a graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", sig_name)
        self.stop()

    def _write_pid_file(self) -> None:
        """Write the current process identity to the pidfile atomically.

        The payload is a JSON object carrying ``pid``, ``starttime``
        (jiffies from /proc/<pid>/stat) and ``cmdline``.  These identity
        tokens let :meth:`is_running` detect a recycled PID or foreign
        process that happens to land on the recorded PID after a crash.

        Atomicity: we write to a tmp file in the same directory and
        rename it over the pidfile so readers never observe a partial
        write.  ``os.replace`` is atomic on POSIX and Windows for same-
        filesystem operations.
        """
        self._PID_DIR.mkdir(parents=True, exist_ok=True)
        identity = _current_process_identity()
        payload = json.dumps(identity, separators=(",", ":"))

        fd, tmp_path = tempfile.mkstemp(prefix=".blackboxrs.pid.", dir=str(self._PID_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync is best-effort (e.g. on tmpfs or some FUSE
                    # backends it can fail harmlessly).
                    pass
            os.replace(tmp_path, self._PID_FILE)
        except Exception:
            # Leave nothing behind on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
