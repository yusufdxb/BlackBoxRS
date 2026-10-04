"""Unit tests for :class:`TfTopologyDetector`.

Mirrors the style of ``test_detectors.py``: events constructed here use
the *real* ``ros.tf`` event_type and data shape that
:class:`TfTopologyDetector` consumes. No ``rclpy`` / ``tf2`` imports
are needed: the detector is a pure event-stream consumer.
"""

from __future__ import annotations

from blackboxrs.anomaly_engine.detectors.tf_topology import TfTopologyDetector
from blackboxrs.core.config import TfTopologyConfig
from blackboxrs.core.schemas import BlackBoxEvent


def _tf_event(
    edges: list[dict] | None = None,
    expected_frames: list[str] | None = None,
) -> BlackBoxEvent:
    """Build a ``ros.tf`` snapshot event."""
    data: dict = {"edges": edges or []}
    if expected_frames is not None:
        data["expected_frames"] = expected_frames
    return BlackBoxEvent.ros_event(event_type="ros.tf", data=data)


class TestTfTopologyDetector:
    """End-to-end behavioural tests for the TF topology detector."""

    # -- Empty / silent paths -------------------------------------------

    def test_empty_snapshot_returns_none(self):
        detector = TfTopologyDetector()
        result = detector.check(_tf_event(edges=[]))
        assert result is None

    def test_ignores_non_tf_event_type(self):
        detector = TfTopologyDetector()
        event = BlackBoxEvent.ros_event(
            event_type="ros.frequency",
            data={"topic": "/cmd_vel", "frequency_hz": 10.0},
        )
        assert detector.check(event) is None

    def test_ignores_non_ros_source(self):
        detector = TfTopologyDetector()
        event = BlackBoxEvent.system_event(
            event_type="ros.tf",
            data={"edges": []},
        )
        assert detector.check(event) is None

    def test_ignores_non_dict_edges(self):
        detector = TfTopologyDetector()
        # edges must be a list of dicts; a list of strings is malformed.
        result = detector.check(_tf_event(edges=["not-a-dict"]))  # type: ignore[list-item]
        assert result is None

    # -- Healthy graph ---------------------------------------------------

    def test_healthy_chain_is_silent(self):
        detector = TfTopologyDetector()
        edges = [
            {
                "parent": "map",
                "child": "odom",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": 0.05,
                "is_static": False,
            },
            {
                "parent": "base_link",
                "child": "lidar_link",
                "last_update_sec_ago": 0.0,
                "is_static": True,
            },
        ]
        assert detector.check(_tf_event(edges=edges)) is None

    def test_static_edge_never_flagged_stale(self):
        detector = TfTopologyDetector(TfTopologyConfig(stale_timeout_sec=1.0))
        edges = [
            {
                "parent": "base_link",
                "child": "lidar_link",
                # Statics live forever; the producer reports it however
                # old it really is and we must not panic.
                "last_update_sec_ago": 999.0,
                "is_static": True,
            },
        ]
        assert detector.check(_tf_event(edges=edges)) is None

    # -- Anomaly: orphan_frame ------------------------------------------

    def test_orphan_frame_fires(self):
        detector = TfTopologyDetector()
        edges = [
            {
                "parent": "map",
                "child": "odom",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
        ]
        result = detector.check(
            _tf_event(edges=edges, expected_frames=["odom", "base_link"])
        )
        assert result is not None
        assert result.source == "anomaly_engine"
        assert result.event_type == "anomaly.tf_topology"
        assert result.data["failure_kind"] == "orphan_frame"
        assert result.data["frame"] == "base_link"
        assert "missing" in result.data["message"]
        assert result.metadata["signature_fields"] == [
            "failure_kind", "frame", "parent",
        ]
        assert result.metadata["target_subsystem"] == "ros"

    def test_orphan_frame_silent_without_expected_frames(self):
        """Without ``expected_frames`` we cannot tell orphans from leaves."""
        detector = TfTopologyDetector()
        edges = [
            {
                "parent": "map",
                "child": "odom",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
        ]
        # No expected_frames => no orphan to report.
        assert detector.check(_tf_event(edges=edges)) is None

    def test_root_frame_is_not_an_orphan(self):
        """A frame that appears only as a parent is the root, not an orphan."""
        detector = TfTopologyDetector()
        edges = [
            {
                "parent": "map",
                "child": "odom",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
        ]
        # ``map`` is expected and only ever appears as a parent. Healthy.
        assert detector.check(
            _tf_event(edges=edges, expected_frames=["map", "odom"])
        ) is None

    # -- Anomaly: multi_parent ------------------------------------------

    def test_multi_parent_fires(self):
        detector = TfTopologyDetector()
        edges = [
            {
                "parent": "map",
                "child": "base_link",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": 0.1,
                "is_static": False,
            },
        ]
        result = detector.check(_tf_event(edges=edges))
        assert result is not None
        assert result.data["failure_kind"] == "multi_parent"
        assert result.data["frame"] == "base_link"
        # Parent string lists both parents (sorted) so the fingerprint
        # is order-stable across producer iteration order changes.
        assert "map" in result.data["parent"]
        assert "odom" in result.data["parent"]

    # -- Anomaly: stale_edge --------------------------------------------

    def test_stale_edge_fires(self):
        detector = TfTopologyDetector(TfTopologyConfig(stale_timeout_sec=1.0))
        edges = [
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": 5.0,
                "is_static": False,
            },
        ]
        result = detector.check(_tf_event(edges=edges))
        assert result is not None
        assert result.data["failure_kind"] == "stale_edge"
        assert result.data["frame"] == "base_link"
        assert result.data["parent"] == "odom"
        assert result.data["value"] == 5.0
        assert result.data["threshold"] == 1.0

    def test_stale_edge_silent_within_timeout(self):
        detector = TfTopologyDetector(TfTopologyConfig(stale_timeout_sec=10.0))
        edges = [
            {
                "parent": "odom",
                "child": "base_link",
                "last_update_sec_ago": 0.5,
                "is_static": False,
            },
        ]
        assert detector.check(_tf_event(edges=edges)) is None

    # -- Misc ------------------------------------------------------------

    def test_name_property(self):
        assert TfTopologyDetector().name == "tf_topology"
