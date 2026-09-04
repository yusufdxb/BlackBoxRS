"""Unit tests for the repeat-launch capture benchmark matrix."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jsonschema import validate


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "native_capture_benchmark_matrix.py"
SPEC = importlib.util.spec_from_file_location("native_capture_benchmark_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def _artifact(backend: str, launch_index: int, *, valid: bool = True) -> dict[str, Any]:
    label = matrix.BACKEND_LABELS[backend]
    return {
        "schema_version": "blackboxrs.capture_benchmark.v1",
        "generated_at": f"2026-08-11T00:00:{launch_index:02d}Z",
        "git_sha": "unit-sha",
        "git_dirty": True,
        "machine": {},
        "build": {},
        "scenario": {
            "name": "custom",
            "run_id": f"unit-{launch_index}",
            "topics": 2,
            "aggregate_rate_hz": 500.0,
            "payload_bytes": 256,
            "duration_sec": 1.0,
            "actual_duration_sec": 1.0,
            "qos": "best_effort",
            "qos_depth": 10,
            "burst_every_sec": 0.0,
            "burst_duration_ms": 0.0,
            "burst_multiplier": 1.0,
            "churn_every_sec": 0.0,
            "churn_down_ms": 0.0,
            "writer_delay_injection_ms": 0,
            "fail_after_bytes": -1,
            "expect_storage_fault": False,
            "shared_steady_clock_domain": True,
        },
        "capture_backend": label,
        "comparison": {
            "backend": backend,
            "reference_backend": "native",
            "comparable_scope": "unit",
            "content_equivalent_to_native": True,
            "content_equivalence_note": "unit",
            "counter_reconciled": True if backend == "native" else False,
            "reconciliation_note": "unit",
            "matched_count": 1,
            "approximated_count": 0,
            "unmatched_count": 0,
            "not_applicable_count": 0,
            "dimensions": {},
        },
        "measurement_limitations": [],
        "capture_quality": None,
        "validity": {"valid": valid, "errors": [] if valid else ["injected"], "warnings": []},
        "counters": {
            "sent": 1000,
            "publish_calls": 1000,
            "received": 1000 if backend == "native" else None,
            "admitted": 1000 if backend == "native" else None,
            "committed": 1000 if backend == "native" else None,
            "durable": 1000 if backend == "native" else None,
            "dropped": 0 if backend == "native" else None,
            "dropped_bytes": 0 if backend == "native" else None,
            "serialized_retained": 1000,
            "serialized_retained_bytes": 256000,
        },
        "drop_breakdown": None,
        "latency_us": {},
        "resources": {
            "cpu_percent": {"mean": float(launch_index)},
            "rss_mb": {"max": float(launch_index + 40)},
        },
        "queue": {},
        "storage": {},
        "lifecycle": {},
        "recovery": None,
        "provenance": {},
    }


def _args(tmp_path: Path, *extra: str) -> Any:
    return matrix.build_parser().parse_args(
        [
            "--exploratory",
            "--output-dir",
            str(tmp_path / "matrix"),
            "--scenario",
            "custom",
            "--topics",
            "2",
            "--rate",
            "500",
            "--payload-bytes",
            "256",
            "--duration-sec",
            "1",
            "--discovery-warmup-sec",
            "1",
            "--startup-timeout-sec",
            "1",
            "--shutdown-timeout-sec",
            "1",
            "--child-timeout-sec",
            "30",
            *extra,
        ]
    )


def _fake_runner(
    commands: list[list[str]],
    *,
    invalid_launch: int | None = None,
    null_cpu_launch: int | None = None,
    public_run: bool = False,
):
    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        launch_index = len(commands)
        output = Path(command[command.index("--output") + 1])
        backend = command[command.index("--backend") + 1]
        artifact = _artifact(backend, launch_index, valid=launch_index != invalid_launch)
        if public_run:
            artifact["git_dirty"] = False
        if launch_index == null_cpu_launch:
            artifact["resources"]["cpu_percent"]["mean"] = None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


def test_default_matrix_counterbalances_five_fresh_launches_and_hashes(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    args = _args(tmp_path)

    summary = matrix.run(args, runner=_fake_runner(commands))

    assert [command[command.index("--backend") + 1] for command in commands] == [
        "native",
        "rosbag2",
        "rosbag2",
        "native",
        "native",
        "rosbag2",
        "rosbag2",
        "native",
        "native",
        "rosbag2",
    ]
    assert len({command[command.index("--run-id") + 1] for command in commands}) == 10
    workload_arguments = matrix._workload_arguments(args)
    assert all(command[-len(workload_arguments) :] == workload_arguments for command in commands)
    assert summary["validity"]["valid"] is True
    assert summary["publication"]["eligible"] is False
    assert summary["comparison_scope"]["durability_equivalence_claimed"] is False
    assert all(run["sha256"] for run in summary["schedule"]["runs"])
    checksum_lines = (args.output_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(checksum_lines) == 11
    assert checksum_lines == sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1])
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        assert digest == matrix._sha256(args.output_dir / relative)
    schema = json.loads(
        (REPO / "scripts" / "native_capture_benchmark_matrix.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate(summary, schema)


def test_summary_uses_only_median_and_p95_for_matched_metrics(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    summary = matrix.run(_args(tmp_path), runner=_fake_runner(commands))

    native_cpu = summary["backends"]["native"]["metrics"]["recorder_cpu_percent_mean"]
    assert native_cpu["median"] == 5.0
    assert native_cpu["p95"] == pytest.approx(8.8)
    assert "mean" not in native_cpu
    assert set(summary["comparison_scope"]["included_metrics"]) == set(
        matrix.COMPARABLE_METRICS
    )
    assert "ingest_latency" in summary["comparison_scope"]["excluded_metrics"]
    assert "durability" in summary["comparison_scope"]["excluded_metrics"]


def test_null_child_metric_remains_null_and_unsupported(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    summary = matrix.run(_args(tmp_path), runner=_fake_runner(commands, null_cpu_launch=1))

    cpu = summary["backends"]["native"]["metrics"]["recorder_cpu_percent_mean"]
    assert cpu["supported"] is False
    assert cpu["null_count"] == 1
    assert cpu["median"] is None
    assert cpu["p95"] is None


def test_invalid_child_fails_summary_and_stops_further_launches(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    summary = matrix.run(_args(tmp_path), runner=_fake_runner(commands, invalid_launch=2))

    assert len(commands) == 2
    assert summary["validity"]["valid"] is False
    assert summary["validity"]["all_child_artifacts_valid"] is False
    assert "child benchmark validity is not true" in " ".join(summary["validity"]["errors"])
    assert (args_output := tmp_path / "matrix" / "summary.json").is_file()
    assert json.loads(args_output.read_text(encoding="utf-8"))["validity"]["valid"] is False


def test_summary_is_deterministic_for_identical_child_artifacts(tmp_path: Path) -> None:
    first_commands: list[list[str]] = []
    second_commands: list[list[str]] = []

    first = matrix.run(_args(tmp_path / "first"), runner=_fake_runner(first_commands))
    second = matrix.run(_args(tmp_path / "second"), runner=_fake_runner(second_commands))

    assert first == second


def test_public_preflight_rejects_fewer_than_five_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = matrix.build_parser().parse_args(["--repetitions", "4"])
    monkeypatch.setattr(matrix, "_git_is_clean", lambda _repo: True)

    with pytest.raises(ValueError, match="at least five"):
        matrix._public_provenance(args, REPO)


def test_public_preflight_attests_sourced_install_and_cmake_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "install"
    package_prefixes = {}
    for package, executable in (
        ("blackbox_capture_cpp", "blackbox_capture"),
        ("blackbox_capture_bench", "publisher"),
    ):
        prefix = install / package
        package_prefixes[package] = prefix
        package_xml = prefix / "share" / package / "package.xml"
        package_xml.parent.mkdir(parents=True)
        package_xml.write_text("<package/>", encoding="utf-8")
        binary = prefix / "lib" / package / executable
        binary.parent.mkdir(parents=True)
        binary.write_text("unit", encoding="utf-8")
        binary.chmod(0o755)
    cache = tmp_path / "CMakeCache.txt"
    cache.write_text(
        "CMAKE_BUILD_TYPE:STRING=RelWithDebInfo\n"
        "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n",
        encoding="utf-8",
    )
    args = matrix.build_parser().parse_args(
        [
            "--install-prefix",
            str(install),
            "--cmake-cache",
            str(cache),
            "--ros-distro",
            "humble",
            "--rmw-implementation",
            "rmw_fastrtps_cpp",
            "--ros-domain-id",
            "87",
        ]
    )
    monkeypatch.setenv("ROS_DISTRO", "humble")
    monkeypatch.setenv(
        "AMENT_PREFIX_PATH",
        ":".join(str(prefix) for prefix in package_prefixes.values()),
    )
    monkeypatch.setattr(matrix, "_git_is_clean", lambda _repo: True)
    monkeypatch.setattr(matrix, "_output_does_not_dirty_repo", lambda _repo, _path: True)
    monkeypatch.setattr(matrix, "_git", lambda _repo, _arguments: "unit-sha")
    monkeypatch.setattr(
        matrix,
        "_public_relative",
        lambda _repo, path, _label: Path(path).name,
    )
    monkeypatch.setattr(
        matrix.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/usr/bin/c++", "--version"], 0, stdout="g++ unit\n", stderr=""
        ),
    )

    provenance = matrix._public_provenance(args, REPO)

    assert provenance["git_sha"] == "unit-sha"
    assert provenance["install_sourced_via_ament_prefix_path"] is True
    assert provenance["compiler"] == "/usr/bin/c++"
    assert provenance["compiler_version"] == "g++ unit"
    assert provenance["build_type"] == "RelWithDebInfo"
    assert provenance["ros_domain_id"] == 87


@pytest.mark.parametrize("invalid_launch", [None, 2])
def test_publish_dir_is_populated_only_after_a_valid_public_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_launch: int | None,
) -> None:
    commands: list[list[str]] = []
    args = _args(tmp_path)
    args.exploratory = False
    args.publish_dir = tmp_path / "retained"
    provenance = {
        "git_sha": "unit-sha",
        "git_clean_at_start": True,
        "ros_distro": None,
        "rmw_implementation": None,
        "ros_domain_id": None,
        "compiler": None,
        "build_type": None,
        "capture_executable": None,
        "publisher_executable": None,
    }
    original_path_in_repo = matrix._path_in_repo
    monkeypatch.setattr(matrix, "_public_provenance", lambda _args, _repo: provenance)
    monkeypatch.setattr(matrix, "_git_is_clean", lambda _repo: True)
    monkeypatch.setattr(matrix, "_path_is_ignored", lambda _repo, _path: False)
    monkeypatch.setattr(
        matrix,
        "_path_in_repo",
        lambda repo, path: (
            Path("docs/benchmarks/native_capture/unit")
            if Path(path).resolve() == args.publish_dir.resolve()
            else original_path_in_repo(repo, path)
        ),
    )

    summary = matrix.run(
        args,
        runner=_fake_runner(commands, invalid_launch=invalid_launch, public_run=True),
    )

    if invalid_launch is None:
        assert summary["validity"]["valid"] is True
        assert summary["publication"]["retained_schema_paths"]["matrix_summary"] == (
            "schemas/native_capture_benchmark_matrix.schema.json"
        )
        assert args.publish_dir.is_dir()
        published_json = list(args.publish_dir.rglob("*.json"))
        assert len(published_json) == 13
        assert (args.publish_dir / "schemas" / "native_capture_benchmark.schema.json").is_file()
        checksums = (args.publish_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        assert len(checksums) == len(published_json)
    else:
        assert summary["validity"]["valid"] is False
        assert not args.publish_dir.exists()
