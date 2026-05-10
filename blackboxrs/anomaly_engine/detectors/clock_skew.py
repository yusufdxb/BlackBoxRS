"""NTP / clock-skew anomaly detector.

Fires when a sample of clock readings from multiple sources shows
that any two sources disagree by more than a configured tolerance.
Clock drift across nodes is one of the most common upstream causes of
TF lookup failures (``ExtrapolationException``) and bag playback bugs,
so we treat it as a first-class precursor signal.

Like every other detector in this package, this one is a pure
event-stream consumer. The producer is responsible for *gathering* the
readings (running ``ntpq -p``, comparing rclpy Time + system time,
walking ROS message header.stamp deltas, etc.) and emitting one
``BlackBoxEvent`` per sampling tick of the form::

    BlackBoxEvent.system_event(
        event_type="system.clock_skew",
        data={
            "sources": [
                {"name": "system", "epoch_sec": 1234567890.0},
                {"name": "ntp:pool.ntp.org", "epoch_sec": 1234567890.4},
                {"name": "ros:/clock", "epoch_sec": 1234567889.2},
            ],
        },
    )

Each entry's ``epoch_sec`` is a float, *all sampled at the same
instant by the producer*; the detector computes pairwise differences
and reports the worst pair when it exceeds the threshold.
"""

from __future__ import annotations

import logging
from typing import Any

from blackboxrs.core.config import ClockSkewConfig
from blackboxrs.core.schemas import AnomalyData, BlackBoxEvent

from .base import BaseDetector

logger = logging.getLogger(__name__)


class ClockSkewDetector(BaseDetector):
    """Fires when clock readings across sources disagree past tolerance.

    Consumes ``system_monitor`` events with
    ``event_type == "system.clock_skew"``. Each event carries a
    list of ``{name, epoch_sec}`` dicts; the detector finds the pair
    with the largest absolute skew and emits one anomaly when the
    skew exceeds :attr:`ClockSkewConfig.max_skew_sec`. Single-source
    snapshots are silently ignored because there is no pair to compare.

    Args:
        config: A :class:`ClockSkewConfig` with the maximum tolerated
            pairwise skew.
    """

    #: Identity for fingerprinting. The two source names define the
    #: pair; the actual skew magnitude is timing noise and is *not*
    #: a signature field. Source names are stored in lexical order so
    #: (a, b) and (b, a) collide on fingerprint.
    signature_fields = ["source_a", "source_b"]

    target_subsystem = "system"

    def __init__(self, config: ClockSkewConfig | None = None) -> None:
        cfg = config or ClockSkewConfig()
        self._max_skew_sec = cfg.max_skew_sec

    @property
    def name(self) -> str:
        """Return the detector identifier."""
        return "clock_skew"

    # -- Detection ------------------------------------------------------

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate one clock-skew snapshot.

        Args:
            event: The incoming pipeline event.

        Returns:
            An anomaly event for the worst-offending source pair, or
            ``None`` if every pair is within tolerance.
        """
        if (
            event.source != "system_monitor"
            or event.event_type != "system.clock_skew"
        ):
            return None

        sources_raw = event.data.get("sources")
        if not isinstance(sources_raw, list):
            return None

        # Filter to entries we can actually score.
        sources: list[tuple[str, float]] = []
        for src in sources_raw:
            if not isinstance(src, dict):
                continue
            name = src.get("name")
            epoch = src.get("epoch_sec")
            if not isinstance(name, str):
                continue
            if not isinstance(epoch, (int, float)):
                continue
            sources.append((name, float(epoch)))

        # A single source has no pair to skew against. Empty too.
        if len(sources) < 2:
            return None

        # Find the worst pair. O(n^2) is fine: a typical snapshot has
        # 2-5 sources (system, ntp peer(s), /clock, maybe a peer node).
        worst_pair: tuple[str, str] | None = None
        worst_skew = 0.0
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                a_name, a_epoch = sources[i]
                b_name, b_epoch = sources[j]
                skew = abs(a_epoch - b_epoch)
                if skew > worst_skew:
                    worst_skew = skew
                    # Lexical order so (a, b) == (b, a) for fingerprint.
                    if a_name <= b_name:
                        worst_pair = (a_name, b_name)
                    else:
                        worst_pair = (b_name, a_name)

        if worst_pair is None or worst_skew <= self._max_skew_sec:
            return None

        return self._emit(
            event,
            source_a=worst_pair[0],
            source_b=worst_pair[1],
            skew=worst_skew,
            all_sources=sources,
        )

    # -- Helpers --------------------------------------------------------

    def _emit(
        self,
        event: BlackBoxEvent,
        *,
        source_a: str,
        source_b: str,
        skew: float,
        all_sources: list[tuple[str, float]],
    ) -> BlackBoxEvent:
        """Build the anomaly event with the standard envelope."""
        message = (
            f"Clock skew between {source_a!r} and {source_b!r} is "
            f"{skew:.3f}s, exceeding tolerance {self._max_skew_sec:.3f}s."
        )

        anomaly = AnomalyData(
            detector=self.name,
            metric=f"clock_skew:{source_a}|{source_b}",
            value=float(skew),
            threshold=float(self._max_skew_sec),
            message=message,
        )

        logger.warning("Clock skew anomaly: %s", message)

        data: dict[str, Any] = anomaly.model_dump()
        data["source_a"] = source_a
        data["source_b"] = source_b
        # all_sources is rendered into the report so an operator can
        # see whether one source is the outlier or two pairs disagree.
        data["all_sources"] = [
            {"name": n, "epoch_sec": e} for n, e in all_sources
        ]

        metadata = dict(event.metadata)
        metadata.update(self.detector_metadata())

        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.clock_skew",
            data=data,
            severity="warning",
            **metadata,
        )
