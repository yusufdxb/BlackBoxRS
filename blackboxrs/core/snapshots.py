"""SystemSnapshotter: project the BlackBoxRS event stream into typed snapshots.

A *snapshot* is a point-in-time projection of the data the daemon was
already collecting (CPU, memory, disk, thermal, GPU, ROS topic graph)
into a single :class:`SystemSnapshot` row. We compute snapshots at a
fixed cadence over the incident window so the bundle's
``evidence/snapshots.json`` is small, deterministic, and useful for the
graph-delta and resource-excursion timeline derivers.

Design constraints:

* No side effects on the live monitors. We project from already-emitted
  events; we do not re-collect anything.
* Deterministic given the same input event stream. Re-running the
  builder on the same JSONL produces the same snapshots list.
* Cheap. The default cadence yields O(window_seconds / cadence)
  snapshots, capped well below 10k for realistic incident windows.
* Honest about absence. If there are no system or graph events in the
  window, the snapshots list is empty rather than full of None-padded
  rows that look like data.
"""

from __future__ import annotations

import math
import socket
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable

from blackboxrs.core.schemas import BlackBoxEvent

# The snapshot model types live in blackboxrs.incident.models, but this
# module is imported by blackboxrs.incident.builder, which is imported
# during blackboxrs.incident package initialisation. To avoid the
# resulting circular import we resolve the model classes lazily inside
# the few functions that need them.
if TYPE_CHECKING:  # pragma: no cover - type hints only
    from blackboxrs.incident.models import (
        GPUSnapshot,
        SystemSnapshot,
        TopicSnapshot,
    )


# Default cadence; one snapshot every 1.0 s for short windows. The
# projector clamps cadence between 0.5 s (high-frequency demo windows)
# and 30 s (long sessions) regardless of caller input.
DEFAULT_CADENCE_SEC = 1.0
_MIN_CADENCE = 0.5
_MAX_CADENCE = 30.0
_MAX_SNAPSHOTS = 1024


def _clamp_cadence(cadence: float) -> float:
    if math.isnan(cadence) or cadence <= 0:
        return DEFAULT_CADENCE_SEC
    return max(_MIN_CADENCE, min(_MAX_CADENCE, float(cadence)))


def _topic_summary_qos(qos_data: dict) -> str | None:
    """Cheap, deterministic QoS class label for a topic.

    Returns a string like ``"reliable/volatile"``. Used for fingerprint
    topology hashing; not for human display.
    """
    pubs = qos_data.get("publisher_qos_profiles") or []
    if not pubs:
        return None
    pub = pubs[0]
    rel = str(pub.get("reliability", "")).rsplit(".", 1)[-1].lower()
    dur = str(pub.get("durability", "")).rsplit(".", 1)[-1].lower()
    return f"{rel}/{dur}" if (rel or dur) else None


class _MutableState:
    """Internal accumulator: latest values for each metric/topic."""

    def __init__(self) -> None:
        self.cpu_percent: float | None = None
        self.mem_percent: float | None = None
        self.disk_percent: float | None = None
        self.thermal_zones: dict[str, float] = {}
        self.gpu_util: float | None = None
        self.gpu_mem: float | None = None
        self.gpu_temp_c: float | None = None
        self.gpu_power_w: float | None = None
        self.topics: dict[str, dict] = {}  # name -> {pub_count, sub_count, last_freq_hz, qos_summary}
        self.nodes: set[str] = set()
        self.host: str | None = None

    # -- update helpers --------------------------------------------------

    def _ensure_topic(self, name: str) -> dict:
        return self.topics.setdefault(name, {
            "name": name,
            "msg_type": None,
            "pub_count": 0,
            "sub_count": 0,
            "last_freq_hz": None,
            "qos_summary": None,
        })

    def update(self, ev: BlackBoxEvent) -> None:
        """Fold *ev* into the accumulator."""
        self.host = self.host or ev.metadata.get("hostname")

        # System metrics. SystemMonitor emits one event per collector
        # carrying multiple keys; we read the canonical keys only.
        if ev.event_type == "system.cpu":
            v = ev.data.get("cpu_percent")
            if isinstance(v, (int, float)):
                self.cpu_percent = float(v)
        elif ev.event_type == "system.memory":
            v = ev.data.get("memory_percent")
            if isinstance(v, (int, float)):
                self.mem_percent = float(v)
        elif ev.event_type == "system.disk":
            v = ev.data.get("disk_percent")
            if isinstance(v, (int, float)):
                self.disk_percent = float(v)
        elif ev.event_type == "system.thermal":
            for key, value in ev.data.items():
                if key.startswith("thermal_") and isinstance(value, (int, float)):
                    self.thermal_zones[key] = float(value)
        elif ev.event_type == "system.gpu":
            for k, target in (
                ("gpu_utilization_percent", "gpu_util"),
                ("gpu_memory_percent", "gpu_mem"),
                ("gpu_temp_c", "gpu_temp_c"),
                ("gpu_power_w", "gpu_power_w"),
            ):
                v = ev.data.get(k)
                if isinstance(v, (int, float)):
                    setattr(self, target, float(v))

        # ROS topology / frequency.
        elif ev.event_type == "ros.frequency":
            topic = ev.data.get("topic")
            if isinstance(topic, str):
                row = self._ensure_topic(topic)
                hz = ev.data.get("frequency_hz")
                if isinstance(hz, (int, float)):
                    row["last_freq_hz"] = float(hz)
        elif ev.event_type == "ros.qos":
            topic = ev.data.get("topic")
            if isinstance(topic, str):
                row = self._ensure_topic(topic)
                row["pub_count"] = int(ev.data.get("publisher_count", row["pub_count"]))
                row["sub_count"] = int(ev.data.get("subscriber_count", row["sub_count"]))
                msg_type = ev.data.get("msg_type")
                if msg_type:
                    row["msg_type"] = str(msg_type)
                qos = _topic_summary_qos(ev.data)
                if qos:
                    row["qos_summary"] = qos
        elif ev.event_type == "ros.graph":
            for name in ev.data.get("nodes", []) or []:
                if isinstance(name, str):
                    self.nodes.add(name)
            for topic_info in ev.data.get("topics", []) or []:
                if isinstance(topic_info, dict) and "name" in topic_info:
                    self._ensure_topic(topic_info["name"])

    # -- materialise -----------------------------------------------------

    def freeze(self, t: datetime, fallback_host: str) -> "SystemSnapshot":
        # Lazy import to break the snapshots -> incident.models -> incident
        # package init cycle (snapshots is imported by incident.builder).
        from blackboxrs.incident.models import (
            GPUSnapshot,
            SystemSnapshot,
            TopicSnapshot,
        )

        gpu_present = any(v is not None for v in (
            self.gpu_util, self.gpu_mem, self.gpu_temp_c, self.gpu_power_w))
        gpu = (
            GPUSnapshot(
                util_percent=self.gpu_util,
                mem_percent=self.gpu_mem,
                temp_c=self.gpu_temp_c,
                power_w=self.gpu_power_w,
            ) if gpu_present else None
        )
        topics = sorted(
            (TopicSnapshot(**row) for row in self.topics.values()),
            key=lambda t: t.name,
        )
        return SystemSnapshot(
            t=t,
            host=self.host or fallback_host,
            cpu_percent=self.cpu_percent,
            mem_percent=self.mem_percent,
            disk_percent=self.disk_percent,
            thermal_zones=dict(self.thermal_zones) or None,
            gpu=gpu,
            topics=topics,
            nodes=sorted(self.nodes),
        )


class SystemSnapshotter:
    """Project an event stream into a list of typed snapshots.

    Args:
        cadence_sec: Time between successive snapshots in seconds.
            Clamped to ``[0.5, 30.0]``.
        fallback_host: Hostname to attribute snapshots to when no event
            metadata carries a hostname (e.g. synthetic fixtures).
    """

    def __init__(
        self,
        cadence_sec: float = DEFAULT_CADENCE_SEC,
        fallback_host: str | None = None,
    ) -> None:
        self._cadence = _clamp_cadence(cadence_sec)
        self._fallback_host = fallback_host or socket.gethostname()

    def project(
        self,
        events: Iterable[BlackBoxEvent],
        window_start: datetime,
        window_end: datetime,
    ) -> list["SystemSnapshot"]:
        """Return snapshots covering ``[window_start, window_end]``.

        Snapshots emit at multiples of ``cadence_sec`` from
        ``window_start``. Each snapshot reflects the most recent value
        observed in the events stream up to (but not after) the
        snapshot's timestamp. If no events have been seen yet, no
        snapshot is emitted at that time.

        The returned list is ordered by ``t`` ascending and capped at
        :data:`_MAX_SNAPSHOTS` rows.
        """
        if window_end < window_start:
            return []

        # Sort events by timestamp; the projector is order-sensitive.
        ordered = sorted(events, key=lambda e: e.timestamp)
        if not ordered:
            return []

        target_times = self._snapshot_times(window_start, window_end)
        state = _MutableState()
        snapshots: list = []

        i = 0
        for t in target_times:
            # Fold every event with timestamp <= t into the accumulator.
            while i < len(ordered) and ordered[i].timestamp <= t:
                state.update(ordered[i])
                i += 1

            # Refuse to emit empty rows: nothing observed yet.
            if state.cpu_percent is None and state.mem_percent is None \
                    and not state.topics and not state.gpu_temp_c \
                    and not state.thermal_zones:
                continue
            snapshots.append(state.freeze(t, self._fallback_host))

        return snapshots

    def _snapshot_times(self, start: datetime, end: datetime) -> list[datetime]:
        """Yield up to ``_MAX_SNAPSHOTS`` timestamps in ``[start, end]``."""
        cadence = self._cadence
        delta = (end - start).total_seconds()
        if delta < 0:
            return []
        step_count = int(delta / cadence) + 1
        if step_count > _MAX_SNAPSHOTS:
            # Stretch cadence to stay under the cap. Honest scaling
            # beats lying about resolution we don't have.
            cadence = delta / (_MAX_SNAPSHOTS - 1)
            step_count = _MAX_SNAPSHOTS
        return [start + timedelta(seconds=cadence * n) for n in range(step_count)]
