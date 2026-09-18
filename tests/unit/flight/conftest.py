"""Shared fixtures for flight-recorder tests."""

from __future__ import annotations

import pytest

from blackboxrs.flight import load_profile
from blackboxrs.flight.profile import FlightProfile

# A fixed session so two runs of the same scenario are byte-comparable.
FIXED_SESSION = {
    "session_id": "fixture_session",
    "experiment": "unit-test",
    "hostname": "fixture-host",
    "platform": "fixture",
    "ros_distro": "humble",
    "rmw_implementation": "rmw_cyclonedds_cpp",
    "blackboxrs_version": "test",
    "blackboxrs_git_sha": "0" * 40,
    "blackboxrs_git_dirty": False,
    "experiment_repos": [],
    "profile_name": "go2",
}


@pytest.fixture
def go2(tmp_path) -> FlightProfile:
    return load_profile("go2", evidence_dir=str(tmp_path / "evidence"))
