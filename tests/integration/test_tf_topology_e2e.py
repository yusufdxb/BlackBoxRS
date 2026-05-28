"""End-to-end integration test: TfSnapshotter event -> TfTopologyDetector fire.

Verifies the full path:

    synthesized ``ros.tf`` event
        -> EventBus.publish()
            -> AnomalyEngine (background thread)
                -> TfTopologyDetector.check()
                    -> anomaly.tf_topology published back on the bus

No component is mocked. The test constructs a real EventBus and a real
AnomalyEngine and uses the detector's own config shape (TfTopologyConfig).
rclpy / tf2_msgs are not required: we build the event payload by hand,
exactly mimicking what TfSnapshotter.emit_snapshot() produces, so the
test proves the wiring works without needing a live ROS 2 node.

Three code paths are exercised:

1. **stale_edge**: a non-static edge whose ``last_update_sec_ago`` exceeds
   ``stale_timeout_sec`` triggers the detector.
2. **orphan_frame**: a frame listed in ``expected_frames`` is absent from
   every edge, triggering an ``orphan_frame`` anomaly.
3. **healthy snapshot**: a fresh dynamic edge and a matching
   ``expected_frames`` entry produce no anomaly.
"""

from __future__ import annotations

import time
from queue import Empty, Queue
from typing import Iterator

import pytest

from blackboxrs.anomaly_engine.engine import AnomalyEngine
from blackboxrs.core.config import (
    BlackBoxConfig,
    TfTopologyConfig,
)
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STALE_TIMEOUT_SEC = 2.0
_POLL_INTERVAL = 0.05
_WAIT_TIMEOUT = 5.0


def _make_engine(bus: EventBus) -> AnomalyEngine:
    """Build an AnomalyEngine with TfTopologyDetector wired and a tight
    stale_timeout_sec so the test doesn't have to wait."""
    cfg = BlackBoxConfig.default()
    cfg.anomaly_engine.tf_topology = TfTopologyConfig(
        stale_timeout_sec=_STALE_TIMEOUT_SEC
    )
    sess = Session()
    return AnomalyEngine(bus, cfg.anomaly_engine, sess)


def _tf_event(
    edges: list[dict],
    expected_frames: list[str] | None = None,
) -> BlackBoxEvent:
    """Build a ``ros.tf`` event in the exact shape TfSnapshotter emits."""
    data: dict = {"edges": edges}
    if expected_frames is not None:
        data["expected_frames"] = expected_frames
    return BlackBoxEvent.ros_event(event_type="ros.tf", data=data)


def _wait_for_anomaly(
    queue: "Queue[BlackBoxEvent]",
    *,
    event_type: str,
    timeout: float = _WAIT_TIMEOUT,
) -> BlackBoxEvent | None:
    """Drain *queue* until an anomaly with *event_type* arrives or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            ev = queue.get(timeout=min(_POLL_INTERVAL, remaining))
        except Empty:
            continue
        if ev.source == "anomaly_engine" and ev.event_type == event_type:
            return ev
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus_and_engine() -> Iterator[tuple[EventBus, AnomalyEngine]]:
    """Start a real EventBus + AnomalyEngine, yield, then stop cleanly."""
    bus = EventBus()
    engine = _make_engine(bus)
    engine.start()
    try:
        yield bus, engine
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTfTopologyE2E:
    """Full bus-wiring tests for TfTopologyDetector."""

    def test_stale_edge_fires_anomaly(
        self, bus_and_engine: tuple[EventBus, AnomalyEngine]
    ):
        """A dynamic edge past stale_timeout_sec must trigger the detector."""
        bus, engine = bus_and_engine

        # Subscribe *before* publishing so we catch the anomaly.
        anomaly_q: Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        stale_age = _STALE_TIMEOUT_SEC + 1.5  # well past threshold
        event = _tf_event(
            edges=[
                {
                    "parent": "odom",
                    "child": "base_link",
                    "last_update_sec_ago": stale_age,
                    "is_static": False,
                }
            ]
        )
        bus.publish(event)

        anomaly = _wait_for_anomaly(anomaly_q, event_type="anomaly.tf_topology")
        assert anomaly is not None, (
            "TfTopologyDetector did not fire for a stale edge; "
            "check that TfTopologyDetector is wired in engine._init_detectors()"
        )
        assert anomaly.data["failure_kind"] == "stale_edge"
        assert anomaly.data["frame"] == "base_link"
        assert anomaly.data["parent"] == "odom"
        assert anomaly.data["value"] == pytest.approx(stale_age)
        assert anomaly.data["threshold"] == pytest.approx(_STALE_TIMEOUT_SEC)

        bus.unsubscribe(anomaly_q)

    def test_orphan_frame_fires_anomaly(
        self, bus_and_engine: tuple[EventBus, AnomalyEngine]
    ):
        """An expected_frames entry absent from all edges fires orphan_frame."""
        bus, engine = bus_and_engine

        anomaly_q: Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        # base_link is in expected_frames but NOT in any edge.
        event = _tf_event(
            edges=[
                {
                    "parent": "odom",
                    "child": "camera_link",
                    "last_update_sec_ago": 0.1,
                    "is_static": False,
                }
            ],
            expected_frames=["base_link", "camera_link"],
        )
        bus.publish(event)

        anomaly = _wait_for_anomaly(anomaly_q, event_type="anomaly.tf_topology")
        assert anomaly is not None, "TfTopologyDetector did not fire for orphan frame"
        assert anomaly.data["failure_kind"] == "orphan_frame"
        assert anomaly.data["frame"] == "base_link"

        bus.unsubscribe(anomaly_q)

    def test_healthy_snapshot_produces_no_anomaly(
        self, bus_and_engine: tuple[EventBus, AnomalyEngine]
    ):
        """A fresh, structurally sound snapshot must produce no anomaly."""
        bus, engine = bus_and_engine

        anomaly_q: Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        # Fresh dynamic edge + matching expected_frames entry.
        event = _tf_event(
            edges=[
                {
                    "parent": "odom",
                    "child": "base_link",
                    "last_update_sec_ago": 0.05,  # well within timeout
                    "is_static": False,
                },
                {
                    "parent": "base_link",
                    "child": "imu_link",
                    "last_update_sec_ago": 0.0,
                    "is_static": True,
                },
            ],
            expected_frames=["base_link", "imu_link"],
        )
        bus.publish(event)

        # Give the engine a full drain cycle to process the event.
        time.sleep(0.4)

        # Drain any events the engine may have published.
        anomalies = []
        while True:
            try:
                ev = anomaly_q.get_nowait()
                if ev.source == "anomaly_engine":
                    anomalies.append(ev)
            except Empty:
                break

        tf_anomalies = [a for a in anomalies if a.event_type == "anomaly.tf_topology"]
        assert tf_anomalies == [], (
            f"Expected no tf_topology anomaly for a healthy snapshot, "
            f"got: {[a.data for a in tf_anomalies]}"
        )

        bus.unsubscribe(anomaly_q)

    def test_static_edge_never_fires_stale(
        self, bus_and_engine: tuple[EventBus, AnomalyEngine]
    ):
        """A static edge must not trigger stale_edge even with a large age."""
        bus, engine = bus_and_engine

        anomaly_q: Queue[BlackBoxEvent] = bus.subscribe(maxsize=64)

        event = _tf_event(
            edges=[
                {
                    "parent": "base_link",
                    "child": "laser_link",
                    "last_update_sec_ago": 999.0,  # large, but is_static=True
                    "is_static": True,
                }
            ]
        )
        bus.publish(event)

        time.sleep(0.4)

        anomalies = []
        while True:
            try:
                ev = anomaly_q.get_nowait()
                if ev.source == "anomaly_engine":
                    anomalies.append(ev)
            except Empty:
                break

        tf_anomalies = [a for a in anomalies if a.event_type == "anomaly.tf_topology"]
        assert tf_anomalies == [], (
            "Static edge incorrectly triggered a stale_edge anomaly"
        )

        bus.unsubscribe(anomaly_q)

    def test_tf_topology_wired_in_observer_mode(self):
        """TfTopologyDetector must appear in the wired set in observer mode.

        It is DDS-bound (consumes ros.tf events over the EventBus) and
        therefore compatible with runtime.role: observer.
        """
        bus = EventBus()
        cfg = BlackBoxConfig.default()
        cfg.anomaly_engine.observer_mode = True
        sess = Session(role="observer")
        engine = AnomalyEngine(bus, cfg.anomaly_engine, sess)

        names = {d.name for d in engine._detectors}
        assert "tf_topology" in names, (
            "TfTopologyDetector must remain wired in observer mode "
            "(it is DDS-bound, not host-bound)"
        )
