"""Replay: reports regenerate identically and triggers reproduce offline."""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from blackboxrs.flight import synthetic
from blackboxrs.flight.analysis import analyze
from blackboxrs.flight.bundle import BundleWriter, load_bundle
from blackboxrs.flight.render import render_markdown
from blackboxrs.flight.replay import canonical, regenerate, retrigger

from .conftest import FIXED_SESSION

SCENARIOS = [n for n in synthetic.VARIANTS]


def bundle_for(go2, out, name):
    _, bundles = synthetic.run(go2, synthetic.scenario(name), out, session=dict(FIXED_SESSION))
    assert len(bundles) == 1
    return Path(bundles[0])


@pytest.mark.parametrize("name", SCENARIOS)
def test_report_regenerates_identically(go2, tmp_path, name):
    b = bundle_for(go2, tmp_path, name)
    res = regenerate(b)
    assert res["stored_report"] and res["identical"], res["differs_in"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_replayed_stream_reproduces_primary_trigger(go2, tmp_path, name):
    b = bundle_for(go2, tmp_path, name)
    rt = retrigger(b)
    assert rt["ok"], rt


def test_two_runs_are_byte_identical(go2, tmp_path):
    a = bundle_for(go2, tmp_path / "a", "stopmove")
    b = bundle_for(go2, tmp_path / "b", "stopmove")
    assert (a / "records.jsonl").read_bytes() == (b / "records.jsonl").read_bytes()
    ra = json.loads((a / "report.json").read_text())
    rb = json.loads((b / "report.json").read_text())
    assert canonical(ra) == canonical(rb)


def test_analysis_is_order_insensitive_to_input_list(go2, tmp_path):
    b = bundle_for(go2, tmp_path, "stopmove")
    manifest, records, _ = load_bundle(b)
    assert canonical(analyze(manifest, records)) == canonical(analyze(manifest, records[::-1]))


def test_killed_recorder_leaves_readable_partial_bundle(go2, tmp_path):
    """SIGKILL mid-capture: no finalize, torn last line. Replay still works."""
    b = bundle_for(go2, tmp_path, "stopmove")
    partial = tmp_path / "killed" / (b.name + ".partial")
    partial.mkdir(parents=True)
    manifest = json.loads((b / "manifest.json").read_text())
    manifest["status"] = "capturing"
    (partial / "manifest.json").write_text(json.dumps(manifest))
    lines = (b / "records.jsonl").read_text().splitlines()
    cut = len(lines) // 2
    (partial / "records.jsonl").write_text("\n".join(lines[:cut]) + "\n" + lines[cut][:40])
    m, recs, info = load_bundle(partial)
    assert info["torn_lines"] == 1 and m["status"] == "interrupted_unfinalized"
    assert len(recs) == cut
    res = regenerate(partial)
    assert not res["stored_report"]
    assert res["report"]["status"] == "interrupted_unfinalized"


def test_write_failure_mid_stream_is_reported(go2, tmp_path):
    """Disk fills after the pre-window: bundle says write_failed, keeps a report."""
    em = synthetic.generate(synthetic.scenario("stopmove"), profile=go2)
    recs = [it[2] for it in sorted(em.items, key=lambda x: (x[0], x[1]))
            if isinstance(it[2], dict)]
    for i, r in enumerate(recs):
        r["seq"] = i + 1
    w = BundleWriter(tmp_path, go2, dict(FIXED_SESSION), lambda: {}, analyze=analyze,
                     render=render_markdown)
    w.open({"type": "manual_marker", "t_mono_ns": recs[500]["t_mono_ns"],
            "t_wall_ns": recs[500]["t_wall_ns"], "seq": 501}, recs[:500], {})
    w.wait(0.2)
    w._fh = _Failing(w._fh)
    for r in recs[500:900]:
        w.append(r)
    path = Path(w.close("complete", {}))
    w.wait(10)
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["status"] == "write_failed"
    assert manifest["writer"]["write_errors"] >= 400
    assert "ENOSPC" in manifest["writer"]["first_write_error"]
    report = json.loads((path / "report.json").read_text())
    assert report["data_quality"]["writer_errors"] >= 400


class _Failing:
    def __init__(self, fh):
        self._fh = fh

    def write(self, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        self._fh.flush()

    def fileno(self):
        return self._fh.fileno()

    def close(self):
        self._fh.close()


def test_disk_pressure_skips_incident_but_recording_continues(go2, tmp_path):
    core, bundles = synthetic.run(go2, synthetic.scenario("stopmove"), tmp_path,
                                  session=dict(FIXED_SESSION),
                                  can_open=lambda: (False, "disk_pressure: 12 MB free"))
    assert bundles == []
    assert core.stats.incidents_skipped >= 1
    assert core.stats_dict()["skip_reasons"]
    assert core.stats.records_in > 1000
