"""Integration test: custom detector wired into AnomalyEngine via config."""

from __future__ import annotations

import time

from blackboxrs.anomaly_engine.detectors.base import BaseDetector
from blackboxrs.anomaly_engine.engine import AnomalyEngine
from blackboxrs.core.config import AnomalyEngineConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session


class _AlwaysFires(BaseDetector):
    """Detector that fires an anomaly on every event."""

    @property
    def name(self) -> str:
        return "always_fires"

    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        return BlackBoxEvent.anomaly_event(
            event_type="anomaly.custom_always",
            data={"original_type": event.event_type},
            trigger_event=event,
        )


_THIS_MODULE = "tests.unit.test_engine_custom_detectors"


class TestEngineWithCustomDetector:

    def test_custom_detector_fires_on_event(self):
        bus = EventBus(default_queue_maxsize=256)
        config = AnomalyEngineConfig(
            custom_detectors=[
                {"class": f"{_THIS_MODULE}._AlwaysFires"},
            ],
        )
        session = Session()
        engine = AnomalyEngine(bus, config, session)

        # Verify the custom detector was loaded
        names = [d.name for d in engine._detectors]
        assert "always_fires" in names

        # Start engine, publish an event, check for anomaly on bus
        output_queue = bus.subscribe(channel="anomaly_engine", maxsize=64)
        engine.start()
        try:
            trigger = BlackBoxEvent.system_event(
                event_type="system.cpu",
                data={"cpu_percent": 50.0, "cpu_count": 4, "per_cpu_percent": [50.0]},
            )
            bus.publish(trigger)

            deadline = time.monotonic() + 2.0
            anomalies = []
            while time.monotonic() < deadline:
                try:
                    evt = output_queue.get(timeout=0.1)
                    if evt.event_type == "anomaly.custom_always":
                        anomalies.append(evt)
                        break
                except Exception:
                    continue

            assert len(anomalies) == 1
            assert anomalies[0].data["original_type"] == "system.cpu"
        finally:
            engine.stop()
