"""Benchmark schema and aggregation tests."""

from __future__ import annotations

import pytest

from blackboxrs.benchmarking.runner import summarize_results
from blackboxrs.benchmarking.schema import (
    BenchmarkResult,
    EnvironmentMetadata,
    PreventionResultSchema,
    ReplayResultSchema,
    ScenarioSpec,
)


def _env() -> EnvironmentMetadata:
    return EnvironmentMetadata(
        blackboxrs_version="0.0.test",
        python_version="3.10",
        platform="test",
        hostname="test-host",
        clock_mode="virtual_ros_time",
    )


def _result(
    scenario_id: str,
    status: str,
    repetition: int = 1,
    *,
    replay_agreement: bool | None = None,
) -> BenchmarkResult:
    return BenchmarkResult(
        scenario_id=scenario_id,
        repetition=repetition,
        status=status,
        passed=status == "pass",
        fault_injected=True,
        latency_clock="virtual_ros_time",
        replay=ReplayResultSchema(
            supported=True,
            attempted=True,
            agreement=replay_agreement,
        ),
        replay_agreement=replay_agreement,
        prevention=PreventionResultSchema(derivable_expected=False),
        duration_sec=0.1,
        runtime_duration_sec=0.1,
        environment=_env(),
    )


def test_scenario_schema_rejects_unsupported_without_reason():
    with pytest.raises(ValueError, match="unsupported_reason"):
        ScenarioSpec(
            scenario_id="x",
            description="x",
            fault_class="x",
            setup="x",
            fault_injection="x",
            status="unsupported",
        )


def test_result_schema_requires_latency_clock():
    result = _result("x", "pass")
    assert result.latency_clock == "virtual_ros_time"
    assert result.model_dump(mode="json")["schema_version"] == "blackboxrs.benchmark.result.v1"


def test_silent_skip_cannot_count_as_pass():
    skipped = _result("x", "skipped")
    assert not skipped.passed


def test_failed_repetition_remains_visible_in_summary(tmp_path):
    results = [_result("x", "pass", 1), _result("x", "fail", 2)]
    summary = summarize_results(results, output_dir=tmp_path, environment=_env())

    assert summary.failed == 1
    assert summary.scenario_statuses["x"] == "fail"


def test_unsupported_scenario_is_distinguished_from_skipped(tmp_path):
    results = [_result("unsupported_case", "unsupported")]
    summary = summarize_results(results, output_dir=tmp_path, environment=_env())

    assert summary.unsupported == 1
    assert summary.skipped == 0
    assert summary.scenario_statuses["unsupported_case"] == "unsupported"


def test_replay_disagreement_fails_relevant_stage():
    result = _result("x", "fail", replay_agreement=False)
    assert result.replay_agreement is False
    assert not result.passed


def test_summary_aggregation_is_deterministic(tmp_path):
    results = [_result("b", "pass"), _result("a", "pass")]
    first = summarize_results(results, output_dir=tmp_path, environment=_env())
    second = summarize_results(list(reversed(results)), output_dir=tmp_path, environment=_env())

    assert list(first.scenario_statuses) == ["a", "b"]
    assert first.scenario_statuses == second.scenario_statuses


def test_machine_readable_output_valid_when_scenario_errors():
    result = _result("x", "error")
    result.error = "RuntimeError: boom"

    parsed = BenchmarkResult.model_validate(result.model_dump(mode="json"))

    assert parsed.status == "error"
    assert parsed.error == "RuntimeError: boom"
