"""Per-process CPU/RSS signals detector.

Fires when a sampled per-process snapshot shows that a tracked ROS
process is consuming more CPU% or RSS (memory) than the configured
threshold. The intent is to capture the *peak* per-process pressure at
the moment of an incident, so the cause ranker can attribute a
trigger like "topic /scan went silent" to the actual node that
starved the bus.

Like every other detector in this package, this one is a pure
event-stream consumer. The producer (a small per-process collector
that calls into ``psutil`` on the system_monitor side) emits one
``BlackBoxEvent`` per sampling tick of the form::

    BlackBoxEvent.system_event(
        event_type="system.process_signals",
        data={
            "sampling_interval_sec": 1.0,
            "processes": [
                {"pid": 1234, "name": "scan_node",
                 "cpu_percent": 95.0, "rss_mb": 412.0},
                {"pid": 1235, "name": "controller",
                 "cpu_percent": 12.0, "rss_mb": 98.0},
                ...
            ],
        },
    )

``psutil`` is already a hard dependency of the package (used by the
existing ``system_monitor`` collectors), so no soft import is needed.
This detector itself does not import psutil because all sampling
happens upstream; we only consume the resulting payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)


@dataclass
class ProcessSignalsConfig:
    """Configuration for :class:`ProcessSignalsDetector`.

    Attributes:
        cpu_percent: A process whose ``cpu_percent`` exceeds this
            value is flagged. The value is per-process and may exceed
            100% on multi-core machines (psutil convention).
        rss_mb: A process whose resident-set-size in MB exceeds this
            value is flagged.
    """

    cpu_percent: float = 90.0
    rss_mb: float = 1024.0


class ProcessSignalsDetector(BaseDetector):
    """Fires when a per-process sample shows excessive CPU% or RSS.

    Consumes ``system_monitor`` events with
    ``event_type == "system.process_signals"``. Each event carries a
    list of ``{pid, name, cpu_percent, rss_mb}`` dicts; the detector
    finds the process with the highest CPU% over threshold (the *peak*
    in the snapshot) and emits one anomaly for it. If no process
    exceeds CPU but one exceeds RSS, that takes the slot instead.

    Emitting one anomaly per snapshot (rather than one per offending
    process) keeps the trigger count bounded; the full process list
    travels in ``data["all_processes"]`` so the report can render the
    full picture.

    Args:
        config: A :class:`ProcessSignalsConfig` with CPU and RSS limits.
    """

    #: Identity for fingerprinting. Two snapshots that flag the same
    #: process by name on the same metric should collide; pid is
    #: deliberately *not* a signature field because it changes every
    #: time the user restarts the node.
    signature_fields = ["process_name", "metric"]

    target_subsystem = "system"

    def __init__(self, config: ProcessSignalsConfig | None = None) -> None:
        cfg = config or ProcessSignalsConfig()
        self._cpu_limit = cfg.cpu_percent
        self._rss_limit = cfg.rss_mb

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "process_signals"

    # -- Detection ------------------------------------------------------

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate one process-signals snapshot.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event for the worst-offending process, or
            ``None`` if no process exceeds either limit.
        """
        if (
            event.source != "system_monitor"
            or event.event_type != "system.process_signals"
        ):
            return None

        processes_raw = event.data.get("processes")
        if not isinstance(processes_raw, list) or not processes_raw:
            return None

        # Filter to entries we can actually score. We tolerate missing
        # rss_mb (treated as 0) so a CPU spike in a process whose RSS
        # collection failed is still observable.
        candidates: list[dict[str, Any]] = []
        for proc in processes_raw:
            if not isinstance(proc, dict):
                continue
            cpu = proc.get("cpu_percent")
            rss = proc.get("rss_mb")
            if not isinstance(cpu, (int, float)):
                continue
            if rss is not None and not isinstance(rss, (int, float)):
                continue
            candidates.append(proc)

        if not candidates:
            return None

        # CPU peak takes priority: a runaway loop is the more common
        # cause of bus starvation than a slow memory leak.
        worst_cpu = max(
            candidates, key=lambda p: float(p.get("cpu_percent", 0.0))
        )
        worst_cpu_value = float(worst_cpu.get("cpu_percent", 0.0))
        if worst_cpu_value > self._cpu_limit:
            return self._emit(
                event,
                proc=worst_cpu,
                metric="cpu_percent",
                value=worst_cpu_value,
                threshold=self._cpu_limit,
                unit="%",
                all_processes=candidates,
            )

        worst_rss = max(
            candidates,
            key=lambda p: float(p.get("rss_mb") or 0.0),
        )
        worst_rss_value = float(worst_rss.get("rss_mb") or 0.0)
        if worst_rss_value > self._rss_limit:
            return self._emit(
                event,
                proc=worst_rss,
                metric="rss_mb",
                value=worst_rss_value,
                threshold=self._rss_limit,
                unit="MB",
                all_processes=candidates,
            )

        return None

    # -- Helpers --------------------------------------------------------

    def _emit(
        self,
        event: BlackBoxEvent,
        *,
        proc: dict[str, Any],
        metric: str,
        value: float,
        threshold: float,
        unit: str,
        all_processes: list[dict[str, Any]],
    ) -> BlackBoxEvent:
        """Build the anomaly event with the standard envelope."""
        process_name = proc.get("name") or f"pid:{proc.get('pid', '?')}"
        pid = proc.get("pid")

        message = (
            f"Process {process_name!r} (pid {pid}) "
            f"{metric}={value:.1f}{unit}, "
            f"exceeding threshold {threshold:.1f}{unit}."
        )

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"process:{metric}:{process_name}",
            value=float(value),
            threshold=float(threshold),
            message=message,
        )

        logger.warning("Process signals anomaly: %s", message)

        data: dict[str, Any] = anomaly.model_dump()
        data["process_name"] = str(process_name)
        data["pid"] = pid if isinstance(pid, int) else None
        # all_processes preserves the full snapshot for later
        # report rendering; it is not a signature field so noise here
        # does not perturb the fingerprint.
        data["all_processes"] = list(all_processes)

        metadata = dict(event.metadata)
        metadata.update(self.detector_metadata())

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.process_signals",
            data=data,
            severity="warning",
            **metadata,
        )
