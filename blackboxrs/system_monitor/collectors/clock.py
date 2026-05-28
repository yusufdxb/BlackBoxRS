"""Clock-skew producer for BlackBoxRS.

Samples all configured time sources once per tick and emits a single
``system.clock_skew`` event for the :class:`ClockSkewDetector` to
evaluate.  Sources sampled on each tick:

- ``system``       — ``time.time()`` on the host running this process.
- ``ntp:<peer>``   — offset reported by ``chronyc -c tracking`` (preferred)
                     or ``ntpq -p`` (fallback).  Skipped cleanly if neither
                     binary is present on the path.
- ``ros:/clock``   — latest stamp from the ROS ``/clock`` topic, included
                     only when ``include_ros_clock`` is enabled or ``"auto"``
                     and ``rclpy`` is importable and an active subscription
                     has received at least one message.

In observer mode the ``system`` and ``ntp:*`` sources reflect the
observer workstation's clock, not the robot's.  That is intentional and
honest: the workstation's clock is what timestamps the event bundle.  See
``docs/design/orphan_detector_producers.md`` §2.5 for the full discussion.

The producer is implemented as a standalone class with a synchronous
``tick()`` method so it can be driven either by the ``SystemMonitor``
run-loop or by test harnesses without spawning threads.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blackboxrs.core.config import ClockProducerConfig

logger = logging.getLogger(__name__)

# One-shot sentinels so we log the "tool not found" warning only once per
# process lifetime, rather than once per tick.
_CHRONYC_WARNED = False
_NTPQ_WARNED = False
_ROS_IMPORT_WARNED = False


# ---------------------------------------------------------------------------
# Pure-function NTP parsers (no subprocess at import time, unit-testable)
# ---------------------------------------------------------------------------


def parse_chronyc_tracking(output: str) -> list[tuple[str, float]] | None:
    """Parse the output of ``chronyc -c tracking``.

    ``chronyc tracking`` with the ``-c`` (CSV) flag emits one line per
    field.  The columns are:

    .. code-block:: text

        <ref-id-hex>,<ref-source>,<stratum>,<ref-time>,<last-offset>,
        <rms-offset>,<freq-ppm>,<skew>,<root-delay>,<root-dispersion>,
        <last-update-interval>,...

    We only need column 4 (0-indexed): ``Last offset`` in seconds.  To
    convert the offset to an absolute epoch we add it to the current
    ``time.time()`` reading captured *before* the subprocess call by the
    caller.

    Args:
        output: Raw stdout from ``chronyc -c tracking``.

    Returns:
        A list of ``(name, epoch_sec)`` tuples — typically one entry for
        the selected NTP peer — or ``None`` if the output could not be
        parsed.
    """
    results: list[tuple[str, float]] = []
    for line in output.strip().splitlines():
        cols = line.split(",")
        # Column layout (0-indexed):
        #   0  Reference ID (hex)
        #   1  Reference source address / name
        #   2  Stratum
        #   3  Ref time (seconds)
        #   4  Last offset (seconds, positive = system ahead of NTP)
        if len(cols) < 5:
            continue
        ref_source = cols[1].strip()
        try:
            offset_sec = float(cols[4].strip())
        except ValueError:
            logger.debug(
                "chronyc: could not parse offset from line %r", line
            )
            continue
        if not ref_source or ref_source in ("0.0.0.0", "127.127.1.0"):
            # No peer selected (unsynchronised).
            continue
        # epoch_sec for the NTP peer: subtract the offset because
        # offset = system - NTP, so NTP_time = system_time - offset.
        system_now = time.time()
        ntp_epoch = system_now - offset_sec
        results.append((f"ntp:{ref_source}", ntp_epoch))
    return results if results else None


def parse_ntpq_peers(output: str) -> list[tuple[str, float]] | None:
    """Parse the output of ``ntpq -p``.

    ``ntpq -p`` emits a table with a header and one row per peer.  The
    currently-selected peer is prefixed with ``*``; other peers use
    ``+``, ``-``, space, or ``#``.  Column layout (space-separated):

    .. code-block:: text

        remote  refid  st  t  when  poll  reach  delay  offset  jitter

    ``offset`` (column index 8 in the data rows) is in milliseconds.
    We only emit the ``*``-selected peer; if none is selected we emit
    the ``+``-preferred peer instead.

    Args:
        output: Raw stdout from ``ntpq -p``.

    Returns:
        A list of ``(name, epoch_sec)`` tuples or ``None`` if no usable
        peer is found.
    """
    selected: tuple[str, float] | None = None
    preferred: tuple[str, float] | None = None

    for line in output.strip().splitlines():
        if not line or line.startswith("=") or line.startswith(" remote"):
            continue
        marker = line[0]
        if marker not in ("*", "+"):
            continue
        parts = line[1:].split()
        if len(parts) < 9:
            continue
        peer = parts[0].lstrip()
        try:
            offset_ms = float(parts[8])
        except (ValueError, IndexError):
            continue
        offset_sec = offset_ms / 1000.0
        system_now = time.time()
        ntp_epoch = system_now - offset_sec
        entry = (f"ntp:{peer}", ntp_epoch)
        if marker == "*":
            selected = entry
        elif preferred is None:
            preferred = entry

    result = selected or preferred
    return [result] if result else None


# ---------------------------------------------------------------------------
# NTP dispatcher
# ---------------------------------------------------------------------------


def _read_ntp_sources(ntp_tool: str) -> list[tuple[str, float]]:
    """Shell out to the configured NTP tool and return parsed sources.

    Args:
        ntp_tool: One of ``"chronyc"``, ``"ntpq"``, or ``"auto"``.

    Returns:
        A (possibly empty) list of ``(name, epoch_sec)`` tuples.
    """
    global _CHRONYC_WARNED, _NTPQ_WARNED  # noqa: PLW0603

    def _try_chronyc() -> list[tuple[str, float]]:
        global _CHRONYC_WARNED  # noqa: PLW0603
        if not shutil.which("chronyc"):
            if not _CHRONYC_WARNED:
                logger.warning(
                    "chronyc not found on PATH — NTP source skipped. "
                    "Install chrony or switch ntp_tool to 'ntpq'."
                )
                _CHRONYC_WARNED = True
            return []
        try:
            result = subprocess.run(
                ["chronyc", "-c", "tracking"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            parsed = parse_chronyc_tracking(result.stdout)
            return parsed or []
        except Exception:
            logger.debug("chronyc invocation failed", exc_info=True)
            return []

    def _try_ntpq() -> list[tuple[str, float]]:
        global _NTPQ_WARNED  # noqa: PLW0603
        if not shutil.which("ntpq"):
            if not _NTPQ_WARNED:
                logger.warning(
                    "ntpq not found on PATH — NTP source skipped. "
                    "Install ntp or chrony."
                )
                _NTPQ_WARNED = True
            return []
        try:
            result = subprocess.run(
                ["ntpq", "-p"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            parsed = parse_ntpq_peers(result.stdout)
            return parsed or []
        except Exception:
            logger.debug("ntpq invocation failed", exc_info=True)
            return []

    if ntp_tool == "chronyc":
        return _try_chronyc()
    if ntp_tool == "ntpq":
        return _try_ntpq()
    # "auto": chronyc preferred, fall back to ntpq.
    sources = _try_chronyc()
    if not sources:
        sources = _try_ntpq()
    return sources


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class ClockSkewCollector:
    """Samples time sources and emits ``system.clock_skew`` events.

    Intended to be driven by the :class:`SystemMonitor` run-loop, which
    calls :meth:`tick` once per configured interval.

    In observer mode (``runtime.role="observer"``) the ``system`` and
    ``ntp:*`` sources reflect the observer workstation's clock, not the
    robot's. This is honest and intentional. The ``ros:/clock`` source
    (if subscribed) reflects the robot's simulated time over DDS, which
    is the most interesting cross-host comparison available without
    on-robot SSH access.

    Args:
        config: A :class:`ClockProducerConfig` instance.
        event_bus: The shared event bus.  ``tick()`` publishes directly
            to the bus so it integrates with the existing monitor
            run-loop that drives all other collectors.
        session_meta: Flat dict of session-level metadata attached to
            every event (``session_id``, ``hostname``, ``role``, etc.).
    """

    def __init__(
        self,
        config: ClockProducerConfig,
        event_bus: object,
        session_meta: dict,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._session_meta = session_meta

        # Cached last /clock stamp from ROS subscription (seconds).
        self._ros_clock_epoch: float | None = None
        # Whether we attempted /clock subscription already.
        self._ros_sub_attempted = False
        self._ros_node: object | None = None
        self._ros_sub: object | None = None

        # Read-window guard threshold: half of max_skew_sec.  We import
        # the detector config lazily to avoid a circular import at module
        # level.  If the config is unavailable we fall back to 50 ms.
        self._window_budget_sec: float = 0.05

    # ------------------------------------------------------------------
    # ROS /clock subscription
    # ------------------------------------------------------------------

    def _try_init_ros_clock(self) -> None:
        """Attempt to subscribe to /clock via rclpy (best-effort).

        No-ops silently if rclpy is not importable or if the include_ros_clock
        setting resolves to False.
        """
        global _ROS_IMPORT_WARNED  # noqa: PLW0603

        include = self._config.include_ros_clock
        if include == "false":
            return

        try:
            import rclpy  # type: ignore[import]
            from rclpy.qos import (  # type: ignore[import]
                QoSProfile,
                ReliabilityPolicy,
                DurabilityPolicy,
                HistoryPolicy,
            )
            from rosgraph_msgs.msg import Clock as ClockMsg  # type: ignore[import]

            if not rclpy.ok():
                return

            if self._ros_node is None:
                self._ros_node = rclpy.create_node(
                    "blackboxrs_clock_monitor",
                    automatically_declare_parameters_from_overrides=False,
                )

            qos = QoSProfile(
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
            )

            def _cb(msg: ClockMsg) -> None:
                sec = msg.clock.sec + msg.clock.nanosec * 1e-9
                self._ros_clock_epoch = sec

            self._ros_sub = self._ros_node.create_subscription(
                ClockMsg, "/clock", _cb, qos
            )
            logger.info("ClockSkewCollector: subscribed to /clock")
        except ImportError:
            if not _ROS_IMPORT_WARNED:
                logger.debug(
                    "rclpy/rosgraph_msgs not importable — ros:/clock source disabled"
                )
                _ROS_IMPORT_WARNED = True
        except Exception:
            logger.debug("Failed to subscribe to /clock", exc_info=True)

    def _spin_ros_once(self) -> None:
        """Drain pending ROS callbacks without blocking."""
        try:
            import rclpy  # type: ignore[import]

            if self._ros_node is not None and rclpy.ok():
                rclpy.spin_once(self._ros_node, timeout_sec=0.0)
        except Exception:
            logger.debug("spin_once failed", exc_info=True)

    # ------------------------------------------------------------------
    # Snapshot building
    # ------------------------------------------------------------------

    def _collect_sources(self) -> tuple[list[dict], float]:
        """Read all enabled sources and return (sources, window_sec).

        Returns:
            A tuple of the source list and the elapsed wall-clock time
            for the entire collection window.  The caller uses
            ``window_sec`` for the read-window guard.
        """
        t_start = time.monotonic()
        sources: list[dict] = []

        # 1. system
        system_epoch = time.time()
        sources.append({"name": "system", "epoch_sec": system_epoch})

        # 2. NTP
        if self._config.include_ntp:
            ntp_sources = _read_ntp_sources(self._config.ntp_tool)
            for name, epoch in ntp_sources:
                sources.append({"name": name, "epoch_sec": epoch})

        # 3. /clock
        include_ros = self._config.include_ros_clock
        if include_ros in ("auto", "true"):
            if not self._ros_sub_attempted:
                self._try_init_ros_clock()
                self._ros_sub_attempted = True
            self._spin_ros_once()
            if self._ros_clock_epoch is not None:
                sources.append(
                    {"name": "ros:/clock", "epoch_sec": self._ros_clock_epoch}
                )

        window_sec = time.monotonic() - t_start
        return sources, window_sec

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_window_budget(self, budget_sec: float) -> None:
        """Override the read-window budget used by the guard.

        Normally derived from :attr:`ClockSkewConfig.max_skew_sec` / 2
        by whoever constructs this collector.  Exposed for testing.

        Args:
            budget_sec: Maximum allowed duration for a single collection
                window.  Snapshots that exceed this are dropped.
        """
        self._window_budget_sec = budget_sec

    def tick(self) -> dict | None:
        """Sample all sources and emit one ``system.clock_skew`` event.

        Skips the snapshot if:

        - fewer than 2 sources are available (nothing to compare).
        - the collection window exceeded :attr:`_window_budget_sec`
          (readings would be too stale for honest skew arithmetic).

        Returns:
            The ``data`` dict that was published (for testing convenience),
            or ``None`` if the snapshot was skipped.
        """
        sources, window_sec = self._collect_sources()

        if len(sources) < 2:
            logger.debug(
                "ClockSkewCollector: only %d source(s) — skipping snapshot "
                "(need ≥2 to compare)",
                len(sources),
            )
            return None

        if window_sec > self._window_budget_sec:
            logger.warning(
                "ClockSkewCollector: collection window %.1f ms exceeds budget "
                "%.1f ms — dropping snapshot to avoid stale skew arithmetic.",
                window_sec * 1000,
                self._window_budget_sec * 1000,
            )
            return None

        from blackboxrs.core.schemas import BlackBoxEvent

        data = {"sources": sources}
        event = BlackBoxEvent.system_event(
            event_type="system.clock_skew",
            data=data,
            severity="info",
            **self._session_meta,
        )
        self._event_bus.publish(event)
        logger.debug(
            "ClockSkewCollector: emitted snapshot with %d source(s) "
            "(window=%.1f ms)",
            len(sources),
            window_sec * 1000,
        )
        return data

    def close(self) -> None:
        """Release ROS resources if a /clock subscription was created."""
        try:
            import rclpy  # type: ignore[import]

            if self._ros_sub is not None and self._ros_node is not None:
                self._ros_node.destroy_subscription(self._ros_sub)
                self._ros_sub = None
            if self._ros_node is not None and rclpy.ok():
                self._ros_node.destroy_node()
                self._ros_node = None
        except Exception:
            logger.debug("ClockSkewCollector.close(): cleanup error", exc_info=True)
