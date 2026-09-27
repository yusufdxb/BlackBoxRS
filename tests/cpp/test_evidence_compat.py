"""Evidence written by the C++ recorder is read unchanged by the Python tools.

``blackboxrs synth-record`` runs the real recording pipeline (bounded queue,
FlightCore, evidence writer) on the synthetic GO2 load. The resulting bundle
must load with the Python flight loader and analyzer, pass Replay Lab's
strict evidence checks, replay identically in both engines, and validate
against its own integrity record.
"""

from __future__ import annotations

import json

import pytest

from .conftest import cpp_replay_json, first_difference, needs_cpp, python_replay_json, run_cpp

pytestmark = needs_cpp


@pytest.fixture(scope="module")
def cpp_bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    run_cpp("synth-record", "--out", str(out), "--seconds", "3", "--scale", "1")
    bundles = [p for p in out.iterdir() if p.is_dir()]
    assert len(bundles) == 1
    return bundles[0]


def test_integrity_record_verifies(cpp_bundle):
    out = run_cpp("validate", str(cpp_bundle), "--json")
    assert json.loads(out.stdout)["status"] == "verified"


def test_python_flight_tools_read_it(cpp_bundle):
    from blackboxrs.flight.analysis import analyze
    from blackboxrs.flight.bundle import load_bundle

    manifest, records, info = load_bundle(cpp_bundle)
    assert manifest["status"] == "complete"
    assert manifest["writer"]["implementation"] == "blackboxrs-cpp"
    assert info["torn_lines"] == 0 and not info["partial_dir"]
    report = analyze(manifest, records)
    assert report["topics"]["/lowstate"]["count"] > 1000
    assert report["topics"]["/lowstate"]["stored"] < report["topics"]["/lowstate"]["count"], \
        "store_max_hz decimation: every arrival recorded, payload kept at 20 Hz"


def test_python_lab_accepts_it_strictly(cpp_bundle):
    from blackboxrs.lab.evidence import load_evidence

    ev = load_evidence(cpp_bundle)
    assert not ev.partial
    assert ev.synthetic


@pytest.mark.parametrize("faults", [(), ("drop:topic=/nav/cmd_vel,from_s=1.2",),
                                    ("gap:host=robot,from_s=1.0,to_s=1.8",)])
def test_replays_identically_in_both_engines(cpp_bundle, faults):
    py = python_replay_json(str(cpp_bundle), faults)
    args = []
    for f in faults:
        args += ["--inject", f]
    cpp = cpp_replay_json(str(cpp_bundle), *args)
    assert py == cpp, first_difference(py, cpp)


def test_corruption_is_detected(cpp_bundle, tmp_path):
    import shutil

    b = tmp_path / cpp_bundle.name
    shutil.copytree(cpp_bundle, b)
    data = bytearray((b / "records.jsonl").read_bytes())
    data[len(data) // 3] ^= 0x20
    (b / "records.jsonl").write_bytes(bytes(data))
    out = run_cpp("validate", str(b), "--json", check_codes=(1,))
    assert json.loads(out.stdout)["status"] == "invalid"
