"""Benchmark CLI and closed-loop scenario tests."""

from __future__ import annotations

import json

from click.testing import CliRunner

from blackboxrs.benchmarking.runner import run_benchmark
from blackboxrs.cli.app import cli


def test_dead_topic_closed_loop_benchmark(tmp_path):
    results, summary = run_benchmark(
        output_dir=tmp_path,
        scenario_ids=["dead_topic_dropout"],
        repetitions=1,
        repo_root=tmp_path,
    )

    result = results[0]
    assert summary.failed == 0
    assert result.status == "pass"
    assert result.observed_detector == "dead_topic"
    assert result.incident_integrity_state == "valid_finalized"
    assert result.replay_agreement is True
    assert result.prevention.rule_derived is True
    assert result.prevention.recurrence_blocked is True
    assert result.prevention.healthy_control_passed is True


def test_qos_mismatch_prevention_supported_benchmark(tmp_path):
    results, _summary = run_benchmark(
        output_dir=tmp_path,
        scenario_ids=["qos_mismatch_reliability"],
        repetitions=1,
        repo_root=tmp_path,
    )

    result = results[0]
    assert result.status == "pass"
    assert result.observed_detector == "qos_mismatch"
    assert result.prevention.check_kind == "qos_match"
    assert result.prevention.recurrence_blocked is True
    assert result.healthy_control_result == "pass"


def test_healthy_controls_do_not_raise_expected_faults(tmp_path):
    results, summary = run_benchmark(
        output_dir=tmp_path,
        scenario_ids=[
            "healthy_topic_publisher",
            "healthy_qos_compatible_graph",
            "healthy_tf_stream",
        ],
        repetitions=1,
        repo_root=tmp_path,
    )

    assert summary.failed == 0
    assert all(result.status == "pass" for result in results)
    assert all(result.anomaly_count == 0 for result in results)


def test_corrupted_bundle_scenario_passes_only_on_rejection(tmp_path):
    results, _summary = run_benchmark(
        output_dir=tmp_path,
        scenario_ids=["corrupted_bundle_rejection"],
        repetitions=1,
        repo_root=tmp_path,
    )

    result = results[0]
    assert result.status == "pass"
    assert result.incident_integrity_state == "corrupted"


def test_recurrence_blocking_does_not_break_healthy_control(tmp_path):
    results, _summary = run_benchmark(
        output_dir=tmp_path,
        scenario_ids=["dead_topic_dropout"],
        repetitions=1,
        repo_root=tmp_path,
    )

    result = results[0]
    assert result.preflight_recurrence_result == "block"
    assert result.healthy_control_result == "pass"


def test_cli_returns_nonzero_when_required_scenario_fails(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            "run",
            "--scenario",
            "duplicate_or_forbidden_publisher",
            "--include-unsupported",
            "--repetitions",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    raw = json.loads((tmp_path / "raw_results.json").read_text(encoding="utf-8"))
    assert raw[0]["status"] == "unsupported"


def test_cli_returns_nonzero_for_unknown_required_scenario(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            "run",
            "--scenario",
            "does_not_exist",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
