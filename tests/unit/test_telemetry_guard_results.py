"""Current-invocation lifecycle tests for telemetry guard result artifacts."""

from __future__ import annotations

import builtins
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import blackboxrs.prevention.telemetry_guard as guard_module
from blackboxrs.cli.app import cli
from blackboxrs.prevention.telemetry_guard import (
    begin_guard_invocation,
    load_guard_result,
    refuse_guard_invocation,
)
from tests.telemetry_fixtures import (
    GRAPH_CONTEXT,
    build_telemetry_provenance_fixture,
)


def _seed_pass(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": "old-success",
                "status": "passed",
                "sentinel": "stale",
            }
        ),
        encoding="utf-8",
    )


def _invoke_guard(
    fixture,
    result_path: Path,
    *,
    context_label: str = GRAPH_CONTEXT,
    trusted_fingerprint: str | None = None,
):
    return CliRunner().invoke(
        cli,
        [
            "prevention",
            "guard",
            "--rule",
            str(fixture.rule_path),
            "--result",
            str(result_path),
            "--context-label",
            context_label,
            "--trusted-rule-fingerprint",
            trusted_fingerprint or fixture.rule.rule_fingerprint,
            "--",
            "/bin/true",
        ],
    )


def test_preseeded_pass_is_replaced_by_context_label_refusal(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path / "fixture")
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)

    completed = _invoke_guard(
        fixture,
        result_path,
        context_label="different-declared-label",
    )
    result = load_guard_result(result_path)

    assert completed.exit_code == 1
    assert result.status == "refused"
    assert result.run_id != "old-success"
    assert result.reason is not None
    assert "Declared context label" in result.reason
    assert result.exit_code == 1


def test_preseeded_pass_is_replaced_by_provenance_refusal(tmp_path):
    fixture = build_telemetry_provenance_fixture(tmp_path / "fixture")
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)

    completed = _invoke_guard(
        fixture,
        result_path,
        trusted_fingerprint="0" * 64,
    )
    result = load_guard_result(result_path)

    assert completed.exit_code == 1
    assert result.status == "refused"
    assert result.run_id != "old-success"
    assert result.reason is not None
    assert "trusted local allowlist" in result.reason


def test_preseeded_pass_is_replaced_by_ros_import_failure(
    tmp_path, monkeypatch
):
    fixture = build_telemetry_provenance_fixture(tmp_path / "fixture")
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)
    original_import = builtins.__import__

    def fail_rclpy_import(name, *args, **kwargs):
        if name == "rclpy":
            raise ImportError("simulated missing ROS")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_rclpy_import)

    completed = _invoke_guard(fixture, result_path)
    result = load_guard_result(result_path)

    assert completed.exit_code == 1
    assert result.status == "failed"
    assert result.run_id != "old-success"
    assert result.error_category == "runtime_setup_error"


def test_terminal_artifact_uses_current_run_id_and_reader_rejects_mismatch(
    tmp_path,
):
    result_path = tmp_path / "guard-result.json"
    invocation = begin_guard_invocation(
        result_path,
        requested_topic="/utlidar/robot_pose",
        declared_context_label=GRAPH_CONTEXT,
        run_id="current-run",
    )

    refuse_guard_invocation(invocation, reason="bounded refusal")

    result = load_guard_result(result_path, expected_run_id="current-run")
    assert result.run_id == "current-run"
    assert result.status == "refused"
    with pytest.raises(ValueError, match="run ID mismatch"):
        load_guard_result(result_path, expected_run_id="different-run")


def test_failed_terminal_replace_leaves_current_starting_not_old_success(
    tmp_path, monkeypatch
):
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)
    invocation = begin_guard_invocation(
        result_path,
        requested_topic="/utlidar/robot_pose",
        declared_context_label=GRAPH_CONTEXT,
        run_id="current-run",
    )
    original_replace = guard_module.os.replace

    def fail_terminal_replace(source, destination):
        if ".current-run.tmp" in str(source):
            raise OSError("simulated interruption before atomic replace")
        return original_replace(source, destination)

    monkeypatch.setattr(guard_module.os, "replace", fail_terminal_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        refuse_guard_invocation(invocation, reason="refusal")

    result = load_guard_result(result_path, expected_run_id="current-run")
    assert result.status == "starting"
    assert not list(result_path.parent.glob(f".{result_path.name}.*.tmp"))


def test_failed_initial_replace_leaves_no_old_consumable_result(
    tmp_path, monkeypatch
):
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)

    def fail_initial_replace(_source, _destination):
        raise OSError("simulated interruption before initial atomic replace")

    monkeypatch.setattr(guard_module.os, "replace", fail_initial_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        begin_guard_invocation(
            result_path,
            requested_topic="/utlidar/robot_pose",
            declared_context_label=GRAPH_CONTEXT,
            run_id="current-run",
        )

    assert not result_path.exists()
    assert not list(result_path.parent.glob(f".{result_path.name}.*.tmp"))


@pytest.mark.skipif(sys.platform != "linux", reason="SIGKILL proof requires Linux")
def test_sigkill_during_initial_write_cannot_leave_old_success_consumable(
    tmp_path,
):
    result_path = tmp_path / "guard-result.json"
    _seed_pass(result_path)
    program = """
import os
import signal
import sys
from pathlib import Path
import blackboxrs.prevention.telemetry_guard as guard

def die_before_replace(_source, _destination):
    os.kill(os.getpid(), signal.SIGKILL)

guard.os.replace = die_before_replace
guard.begin_guard_invocation(
    Path(sys.argv[1]),
    requested_topic="/utlidar/robot_pose",
    declared_context_label="bounded-evaluation",
    run_id="current-run",
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", program, str(result_path)],
        check=False,
        timeout=5,
    )

    assert completed.returncode == -signal.SIGKILL
    assert not result_path.exists()
    with pytest.raises(FileNotFoundError):
        load_guard_result(result_path)
    assert list(result_path.parent.glob(f".{result_path.name}.current-run.tmp"))
