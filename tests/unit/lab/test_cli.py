"""``robot-blackbox lab`` commands, including exit codes and the documented examples."""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from blackboxrs.cli.app import cli

from .conftest import ROOT

CASES = "examples/replay_lab/cases"
NOMINAL = "examples/replay_lab/evidence/nominal_motion"


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.chdir(ROOT)
    return CliRunner()


def test_replay_case_exit_code_is_the_verdict(runner):
    r = runner.invoke(cli, ["lab", "replay", f"{CASES}/teleop_vs_stop__twist_mux_legacy.json"])
    assert r.exit_code == 1, r.output
    assert "VERDICT   FAIL" in r.output and "expectations: match" in r.output
    r = runner.invoke(cli, ["lab", "replay", f"{CASES}/nominal_motion.json", "--no-timeline"])
    assert r.exit_code == 0 and "causal timeline" not in r.output


def test_replay_bare_bundle_with_injected_fault(runner, tmp_path):
    out = tmp_path / "r.json"
    r = runner.invoke(cli, ["lab", "replay", NOMINAL, "--sut", "twist_mux_legacy",
                            "--inject", "drop:topic=/nav/cmd_vel,from_s=4.0",
                            "--json", str(out)])
    assert r.exit_code == 1, r.output
    res = json.loads(out.read_text())
    assert res["verdict"]["result"] == "FAIL" and res["injections"][0]["kind"] == "drop"


def test_modified_case_skips_expectations(runner):
    r = runner.invoke(cli, ["lab", "replay", f"{CASES}/nominal_motion.json",
                            "--inject", "nan:topic=/nav/cmd_vel,field=linear.x,from_s=3,to_s=3.2"])
    assert r.exit_code == 4 and "not checked" in r.output


def test_window_and_set_override(runner):
    r = runner.invoke(cli, ["lab", "replay", NOMINAL, "--from", "1.0", "--to", "3.0",
                            "--set", "freshness_clock=source_timestamp", "--json", "-"])
    res = json.loads(r.output)
    assert res["replay"]["window_start_ns"] == 1_000_000_000
    assert res["config"]["sut"]["freshness_clock"] == "source_timestamp"
    assert res["replay"]["notes"]


def test_bad_inputs_exit_5(runner, tmp_path):
    r = runner.invoke(cli, ["lab", "replay", NOMINAL, "--inject", "explode:topic=/x"])
    assert r.exit_code == 5 and "unknown kind" in r.output
    r = runner.invoke(cli, ["lab", "replay", NOMINAL, "--inject", "drop:topic=/nope"])
    assert r.exit_code == 5
    r = runner.invoke(cli, ["lab", "replay", str(tmp_path)])
    assert r.exit_code == 5 and "records.jsonl" in r.output
    r = runner.invoke(cli, ["lab", "replay", NOMINAL, "--inject", "drop:topic=/cmd_vel"])
    assert r.exit_code == 5 and "observed" in r.output


def test_verify_detects_a_wrong_expectation(runner, tmp_path):
    case = json.loads((ROOT / CASES / "nominal_motion.json").read_text())
    case["expect"]["verdict"] = "FAIL"
    case["evidence"] = str(ROOT / NOMINAL)
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps(case))
    r = runner.invoke(cli, ["lab", "verify", str(p), "--repeat", "1"])
    assert r.exit_code == 1 and "expected FAIL, got PASS" in r.output


def test_verify_golden_subset(runner):
    r = runner.invoke(cli, ["lab", "verify", f"{CASES}/clean_stop.json",
                            f"{CASES}/nan_command__twist_mux_legacy.json", "--repeat", "2"])
    assert r.exit_code == 0 and "2/2 cases ok" in r.output


def test_faults_listing(runner):
    r = runner.invoke(cli, ["lab", "faults"])
    assert r.exit_code == 0
    for kind in ("drop", "gap", "delay", "duplicate", "reorder", "stale_redelivery",
                 "clock_skew", "timestamp_jump", "nan", "inf", "malformed", "freeze", "step",
                 "inject_stream", "set_value"):
        assert re.search(rf"^\s+{kind}\s", r.output, re.M), kind


def test_documented_commands_run_with_documented_exit_codes(runner):
    """Every `$ robot-blackbox lab ...  # exit N` line in docs/REPLAY_LAB.md runs as stated."""
    doc = (ROOT / "docs" / "REPLAY_LAB.md").read_text()
    lines = re.findall(r"^\$ robot-blackbox (lab .+?)\s+# exit (\d)\s*$", doc, re.M)
    assert len(lines) >= 4
    for cmd, code in lines:
        r = runner.invoke(cli, cmd.split())
        assert r.exit_code == int(code), f"{cmd}: {r.exit_code}\n{r.output}"
