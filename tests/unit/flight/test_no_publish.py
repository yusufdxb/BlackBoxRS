"""BlackBoxRS must never command the robot: the flight package publishes nothing."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import blackboxrs.flight as flight

PKG = Path(flight.__file__).parent
FORBIDDEN = re.compile(r"create_publisher|create_client|create_service|\.publish\(|call_async|"
                       r"ActionClient|set_parameters")


def test_flight_source_has_no_publisher_or_client():
    hits = []
    for f in sorted(PKG.rglob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if FORBIDDEN.search(code):
                hits.append(f"{f.name}:{i}: {line.strip()}")
    assert hits == []


def test_live_node_has_no_publishers_services_or_clients(tmp_path, monkeypatch):
    pytest.importorskip("rclpy")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    monkeypatch.setenv("ROS_DOMAIN_ID", "91")
    from blackboxrs.flight import load_profile
    from blackboxrs.flight.provenance import build_session
    from blackboxrs.flight.recorder import NODE_NAME, NODE_NAMESPACE, FlightRecorder

    prof = load_profile("go2", evidence_dir=str(tmp_path))
    rec = FlightRecorder(prof, build_session(prof, synthetic=True), gpu="none")
    try:
        rec.spin(0.5)
        node = rec.node
        pubs = dict(node.get_publisher_names_and_types_by_node(NODE_NAME, NODE_NAMESPACE))
        assert set(pubs) <= {"/parameter_events"}, pubs
        assert node.get_service_names_and_types_by_node(NODE_NAME, NODE_NAMESPACE) == []
        assert node.get_client_names_and_types_by_node(NODE_NAME, NODE_NAMESPACE) == []
        assert not getattr(node, "publishers", None) or \
            {p.topic_name for p in node.publishers} <= {"/parameter_events"}
    finally:
        rec.close()
