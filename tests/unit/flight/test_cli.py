"""CLI surface of the flight recorder."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from blackboxrs.cli.app import cli


def test_rehearse_then_replay_and_show(tmp_path):
    r = CliRunner().invoke(cli, ["flight", "rehearse", "--out", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC=True" in r.output and "replay_identical=True" in r.output
    bundle = next(p for p in Path(tmp_path).rglob("inc_*") if p.is_dir())
    r = CliRunner().invoke(cli, ["flight", "replay", str(bundle), "--retrigger"])
    assert r.exit_code == 0, r.output
    assert "IDENTICAL" in r.output and "reproduced" in r.output
    r = CliRunner().invoke(cli, ["flight", "show", str(bundle)])
    assert r.exit_code == 0 and "SYNTHETIC DATA" in r.output


def test_rehearse_unknown_scenario(tmp_path):
    r = CliRunner().invoke(cli, ["flight", "rehearse", "--out", str(tmp_path),
                                 "--scenario", "nope"])
    assert r.exit_code != 0 and "unknown scenario" in r.output


def test_exclude_topic_must_exist(tmp_path):
    r = CliRunner().invoke(cli, ["flight", "preflight", "--evidence-dir", str(tmp_path),
                                 "--exclude-topic", "/not/in/profile"])
    assert r.exit_code != 0 and "not in profile" in r.output


def test_mark_drops_control_file(tmp_path):
    r = CliRunner().invoke(cli, ["flight", "mark", "hello", "--evidence-dir", str(tmp_path)])
    assert r.exit_code == 0
    marks = list((tmp_path / "control").glob("*.mark"))
    assert len(marks) == 1 and marks[0].read_text() == "hello"


def test_top_level_preflight_accepts_profile():
    r = CliRunner().invoke(cli, ["preflight", "--help"])
    assert "--profile" in r.output
