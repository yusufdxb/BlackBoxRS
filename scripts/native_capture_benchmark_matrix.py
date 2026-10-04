#!/usr/bin/env python3
"""Run a counterbalanced, repeat-launch native capture benchmark matrix.

The matrix delegates every measurement to ``native_capture_benchmark.py``.
It adds publication guards, repeat scheduling, artifact validation, hashing,
and deliberately narrow aggregation. It does not claim durability equivalence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


RESULT_SCHEMA = "blackboxrs.capture_benchmark.v1"
MATRIX_SCHEMA = "blackboxrs.capture_benchmark_matrix.v1"
BACKEND_LABELS = {"native": "cpp", "rosbag2": "rosbag2"}
WORKLOAD_KEYS = (
    "name",
    "topics",
    "aggregate_rate_hz",
    "payload_bytes",
    "duration_sec",
    "qos",
    "qos_depth",
    "burst_every_sec",
    "burst_duration_ms",
    "burst_multiplier",
    "churn_every_sec",
    "churn_down_ms",
    "writer_delay_injection_ms",
    "fail_after_bytes",
    "expect_storage_fault",
    "shared_steady_clock_domain",
)
COMPARABLE_METRICS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "recorder_cpu_percent_mean": (
        ("resources", "cpu_percent", "mean"),
        "percent_of_one_logical_cpu",
        "per-run mean for the matched recorder-process accounting boundary",
    ),
    "recorder_peak_rss_mb": (
        ("resources", "rss_mb", "max"),
        "MiB",
        "per-run maximum for the matched recorder-process accounting boundary",
    ),
    "publisher_calls": (
        ("counters", "publish_calls"),
        "messages",
        "identical instrumented publisher invocation for both backends",
    ),
    "serialized_retained_messages": (
        ("counters", "serialized_retained"),
        "messages",
        "full serialized records retained on the matched workload topics only",
    ),
    "serialized_retained_bytes": (
        ("counters", "serialized_retained_bytes"),
        "bytes",
        "serialized payload bytes retained on the matched workload topics only",
    ),
}
EXCLUDED_METRICS = {
    "durability": (
        "native fsync semantics and rosbag2 close/finalization are not equivalent; this matrix "
        "makes no durability-equivalence claim"
    ),
    "storage_bytes": (
        "native storage includes control chronology and has a different durability policy"
    ),
    "queue_and_drop_accounting": (
        "the buffering models differ and stock rosbag2 has no equivalent reasoned-drop ledger"
    ),
    "ingest_latency": (
        "rosbag2 requires sampled realtime-to-monotonic correction while native timestamps use "
        "the local monotonic clock directly"
    ),
    "startup_and_shutdown_latency": (
        "backend liveness markers and close semantics do not establish equivalent durability "
        "boundaries"
    ),
}

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _nested(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _quantile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _counterbalanced_schedule(repetitions: int, first_backend: str) -> list[tuple[int, str]]:
    other = "rosbag2" if first_backend == "native" else "native"
    schedule: list[tuple[int, str]] = []
    for pair_index in range(1, repetitions + 1):
        pair = (first_backend, other) if pair_index % 2 else (other, first_backend)
        schedule.extend((pair_index, backend) for backend in pair)
    return schedule


def _git(repo: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _git_is_clean(repo: Path) -> bool:
    return not _git(repo, ["status", "--porcelain", "--untracked-files=all"])


def _path_in_repo(repo: Path, path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(repo.resolve())
    except ValueError:
        return None


def _output_does_not_dirty_repo(repo: Path, output_dir: Path) -> bool:
    relative = _path_in_repo(repo, output_dir)
    if relative is None:
        return True
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(relative)],
        cwd=repo,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _path_is_ignored(repo: Path, path: Path) -> bool:
    relative = _path_in_repo(repo, path)
    if relative is None:
        return False
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(relative)],
        cwd=repo,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _publish_path(args: argparse.Namespace, repo: Path, output_dir: Path) -> tuple[Path, str] | None:
    if args.publish_dir is None:
        return None
    if args.exploratory:
        raise ValueError("--publish-dir is unavailable for exploratory matrices")
    publish_dir = args.publish_dir.resolve()
    relative = _path_in_repo(repo, publish_dir)
    if relative is None:
        raise ValueError("--publish-dir must be inside the repository")
    if publish_dir.exists():
        raise ValueError("--publish-dir must not exist at matrix start")
    if _path_is_ignored(repo, publish_dir):
        raise ValueError("--publish-dir must not be git-ignored")
    if publish_dir.is_relative_to(output_dir) or output_dir.is_relative_to(publish_dir):
        raise ValueError("--publish-dir and --output-dir must not overlap")
    return publish_dir, relative.as_posix()


def _cache_value(cache: Path, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}(?::[^=]+)?=(.*)$")
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip() or None
    return None


def _package_prefix(install_prefix: Path, package: str) -> Path | None:
    candidates = (install_prefix / package, install_prefix)
    for candidate in candidates:
        if (candidate / "share" / package / "package.xml").is_file():
            return candidate.resolve()
    return None


def _public_relative(repo: Path, path: Path, label: str) -> str:
    relative = _path_in_repo(repo, path)
    if relative is None:
        raise ValueError(f"public {label} must be inside the repository")
    return relative.as_posix()


def _public_provenance(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    if args.repetitions < 5:
        raise ValueError("public matrices require at least five launches per backend")
    if not _git_is_clean(repo):
        raise ValueError("public matrices require a clean git tree")
    if not _output_does_not_dirty_repo(repo, args.output_dir):
        raise ValueError("public matrix output must be outside the repository or git-ignored")
    if args.install_prefix is None or args.cmake_cache is None:
        raise ValueError("public matrices require --install-prefix and --cmake-cache")
    if not args.ros_distro or not args.rmw_implementation:
        raise ValueError("public matrices require --ros-distro and --rmw-implementation")
    if args.ros_domain_id is None:
        raise ValueError("public matrices require an isolated --ros-domain-id")
    if os.environ.get("ROS_DISTRO") != args.ros_distro:
        raise ValueError("--ros-distro must match ROS_DISTRO from the sourced underlay")

    install_prefix = args.install_prefix.resolve()
    cache = args.cmake_cache.resolve()
    if not cache.is_file():
        raise ValueError(f"CMake cache does not exist: {args.cmake_cache}")
    cache_relative = _public_relative(repo, cache, "CMake cache")
    install_relative = _public_relative(repo, install_prefix, "install prefix")

    package_prefixes: dict[str, Path] = {}
    for package in ("blackbox_capture_cpp", "blackbox_capture_bench"):
        prefix = _package_prefix(install_prefix, package)
        if prefix is None:
            raise ValueError(f"installed package {package} was not found under --install-prefix")
        package_prefixes[package] = prefix

    sourced_prefixes = {
        Path(entry).resolve()
        for entry in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep)
        if entry
    }
    missing = [
        package for package, prefix in package_prefixes.items() if prefix not in sourced_prefixes
    ]
    if missing:
        raise ValueError(
            "the requested install is not sourced in AMENT_PREFIX_PATH for: " + ", ".join(missing)
        )

    compiler = _cache_value(cache, "CMAKE_CXX_COMPILER")
    build_type = _cache_value(cache, "CMAKE_BUILD_TYPE")
    if not compiler or not Path(compiler).is_file():
        raise ValueError("CMAKE_CXX_COMPILER is missing or invalid in --cmake-cache")
    if not build_type:
        raise ValueError("CMAKE_BUILD_TYPE is missing in --cmake-cache")
    compiler_result = subprocess.run(
        [compiler, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    compiler_version = compiler_result.stdout.splitlines()[0].strip()

    capture_executable = (
        package_prefixes["blackbox_capture_cpp"]
        / "lib"
        / "blackbox_capture_cpp"
        / "blackbox_capture"
    )
    publisher_executable = (
        package_prefixes["blackbox_capture_bench"]
        / "lib"
        / "blackbox_capture_bench"
        / "publisher"
    )
    for executable in (capture_executable, publisher_executable):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"installed benchmark executable is missing: {executable.name}")

    executables = {
        "blackbox_capture_cpp": capture_executable,
        "blackbox_capture_bench": publisher_executable,
    }
    source_provenance: dict[str, Any] = {}
    for package, executable in executables.items():
        source_root = repo / "src" / package
        source_files = [path for path in source_root.rglob("*") if path.is_file()]
        if not source_files:
            raise ValueError(f"source package is missing: src/{package}")
        newest_source_ns = max(path.stat().st_mtime_ns for path in source_files)
        if executable.stat().st_mtime_ns < newest_source_ns:
            raise ValueError(
                f"installed {package} executable is older than its source tree; rebuild first"
            )
        source_provenance[package] = {
            "source_tree": f"src/{package}",
            "source_tree_sha256": _source_tree_sha256(source_root),
            "installed_executable_sha256": _sha256(executable),
            "installed_executable_not_older_than_source": True,
        }

    return {
        "git_sha": _git(repo, ["rev-parse", "HEAD"]),
        "git_clean_at_start": True,
        "ros_distro": args.ros_distro,
        "rmw_implementation": args.rmw_implementation,
        "ros_domain_id": args.ros_domain_id,
        "install_prefix": install_relative,
        "package_prefixes": {
            package: _public_relative(repo, prefix, f"{package} prefix")
            for package, prefix in package_prefixes.items()
        },
        "install_sourced_via_ament_prefix_path": True,
        "source_provenance": source_provenance,
        "cmake_cache": cache_relative,
        "cmake_cache_sha256": _sha256(cache),
        "compiler": compiler,
        "compiler_version": compiler_version,
        "build_type": build_type,
        "capture_executable": _public_relative(repo, capture_executable, "capture executable"),
        "capture_executable_sha256": _sha256(capture_executable),
        "publisher_executable": _public_relative(
            repo, publisher_executable, "publisher executable"
        ),
        "publisher_executable_sha256": _sha256(publisher_executable),
    }


def _exploratory_provenance(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    git_sha: str | None
    git_clean: bool | None
    try:
        git_sha = _git(repo, ["rev-parse", "HEAD"])
        git_clean = _git_is_clean(repo)
    except subprocess.SubprocessError:
        git_sha = None
        git_clean = None
    return {
        "git_sha": git_sha,
        "git_clean_at_start": git_clean,
        "ros_distro": args.ros_distro or os.environ.get("ROS_DISTRO"),
        "rmw_implementation": args.rmw_implementation or os.environ.get("RMW_IMPLEMENTATION"),
        "ros_domain_id": args.ros_domain_id,
        "install_prefix": None,
        "package_prefixes": {},
        "install_sourced_via_ament_prefix_path": False,
        "source_provenance": {},
        "cmake_cache": None,
        "cmake_cache_sha256": None,
        "compiler": os.environ.get("CXX"),
        "compiler_version": None,
        "build_type": os.environ.get("CMAKE_BUILD_TYPE"),
        "capture_executable": None,
        "capture_executable_sha256": None,
        "publisher_executable": None,
        "publisher_executable_sha256": None,
    }


def _workload_arguments(args: argparse.Namespace) -> list[str]:
    return [
        "--scenario",
        args.scenario,
        "--topics",
        str(args.topics),
        "--rate",
        str(args.rate),
        "--payload-bytes",
        str(args.payload_bytes),
        "--duration-sec",
        str(args.duration_sec),
        "--discovery-warmup-sec",
        str(args.discovery_warmup_sec),
        "--qos",
        args.qos,
        "--qos-depth",
        str(args.qos_depth),
        "--topic-prefix",
        args.topic_prefix,
        "--burst-every-sec",
        str(args.burst_every_sec),
        "--burst-duration-ms",
        str(args.burst_duration_ms),
        "--burst-multiplier",
        str(args.burst_multiplier),
        "--churn-every-sec",
        str(args.churn_every_sec),
        "--churn-down-ms",
        str(args.churn_down_ms),
        "--sample-period-sec",
        str(args.sample_period_sec),
        "--startup-timeout-sec",
        str(args.startup_timeout_sec),
        "--shutdown-timeout-sec",
        str(args.shutdown_timeout_sec),
    ]


def _child_command(
    args: argparse.Namespace,
    repo: Path,
    provenance: Mapping[str, Any],
    backend: str,
    output: Path,
    run_id: str,
) -> list[str]:
    capture = provenance.get("capture_executable")
    publisher = provenance.get("publisher_executable")
    if not isinstance(capture, str):
        capture = args.capture_command
    if not isinstance(publisher, str):
        publisher = args.publisher_command
    engine = repo / "scripts" / "native_capture_benchmark.py"
    return [
        sys.executable,
        str(engine),
        "--backend",
        backend,
        "--output",
        str(output),
        "--run-id",
        run_id,
        "--capture-command",
        capture,
        "--publisher-command",
        publisher,
        "--rosbag2-command",
        args.rosbag2_command,
        *_workload_arguments(args),
    ]


def _workload(artifact: Mapping[str, Any]) -> dict[str, Any] | None:
    scenario = artifact.get("scenario")
    if not isinstance(scenario, Mapping):
        return None
    return {key: scenario.get(key) for key in WORKLOAD_KEYS}


def _child_errors(
    artifact: dict[str, Any] | None,
    *,
    returncode: int,
    backend: str,
    expected_git_sha: str | None,
    expected_build: Mapping[str, Any],
    expected_workload: Mapping[str, Any] | None,
    schema_validator: Draft202012Validator,
    public_run: bool,
) -> list[str]:
    errors: list[str] = []
    if returncode != 0:
        errors.append(f"child process exited with code {returncode}")
    if artifact is None:
        return [*errors, "child did not write a JSON object"]
    schema_errors = sorted(schema_validator.iter_errors(artifact), key=lambda item: list(item.path))
    if schema_errors:
        errors.append("child artifact failed schema validation: " + schema_errors[0].message)
    if artifact.get("schema_version") != RESULT_SCHEMA:
        errors.append("child artifact has an unsupported schema version")
    if artifact.get("capture_backend") != BACKEND_LABELS[backend]:
        errors.append("child artifact backend does not match the scheduled backend")
    validity = artifact.get("validity")
    if not isinstance(validity, Mapping) or validity.get("valid") is not True:
        errors.append("child benchmark validity is not true")
    workload = _workload(artifact)
    if workload is None:
        errors.append("child artifact has no workload scenario")
    elif expected_workload is not None and workload != expected_workload:
        errors.append("child workload differs from the first matrix launch")
    if public_run:
        if artifact.get("git_sha") != expected_git_sha:
            errors.append("child git SHA differs from the matrix preflight SHA")
        if artifact.get("git_dirty") is not False:
            errors.append("child observed a dirty git tree")
        build = artifact.get("build")
        if not isinstance(build, Mapping):
            errors.append("child artifact has no build provenance")
        else:
            for key, expected in expected_build.items():
                if build.get(key) != expected:
                    errors.append(f"child build.{key} differs from matrix provenance")
    return errors


def _metric_summary(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name, (path, unit, reason) in COMPARABLE_METRICS.items():
        raw_values = [_nested(artifact, path) for artifact in artifacts]
        numeric = [
            float(value)
            for value in raw_values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        supported = len(numeric) == len(raw_values) and bool(raw_values)
        metrics[name] = {
            "unit": unit,
            "run_statistic": reason,
            "run_count": len(raw_values),
            "null_count": len(raw_values) - len(numeric),
            "supported": supported,
            "median": _quantile(numeric, 0.50) if supported else None,
            "p95": _quantile(numeric, 0.95) if supported else None,
            "unsupported_reason": (
                None if supported else "one or more child artifacts reported null or non-numeric"
            ),
        }
    return metrics


def _write_checksums(output_dir: Path, json_paths: Sequence[Path]) -> None:
    lines = [
        f"{_sha256(path)}  {path.relative_to(output_dir).as_posix()}"
        for path in sorted(json_paths, key=lambda item: item.relative_to(output_dir).as_posix())
    ]
    _atomic_text(output_dir / "SHA256SUMS", "\n".join(lines) + "\n")


def _publish_evidence(repo: Path, output_dir: Path, publish_dir: Path) -> None:
    parent_existed = publish_dir.parent.exists()
    publish_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{publish_dir.name}.tmp-", dir=publish_dir.parent))
    try:
        shutil.copytree(output_dir / "runs", staging / "runs")
        shutil.copy2(output_dir / "summary.json", staging / "summary.json")
        schemas = staging / "schemas"
        schemas.mkdir()
        for name in (
            "native_capture_benchmark.schema.json",
            "native_capture_benchmark_matrix.schema.json",
        ):
            shutil.copy2(repo / "scripts" / name, schemas / name)
        _write_checksums(staging, list(staging.rglob("*.json")))
        if publish_dir.exists():
            raise FileExistsError(f"publish directory appeared during the matrix: {publish_dir}")
        staging.rename(publish_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if not parent_existed:
            try:
                publish_dir.parent.rmdir()
            except OSError:
                pass
        raise


def run(
    args: argparse.Namespace,
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir.resolve()
    if args.repetitions <= 0:
        raise ValueError("--repetitions must be positive")
    if args.child_timeout_sec <= 0 or args.child_timeout_sec > 3600:
        raise ValueError("--child-timeout-sec must be in (0, 3600]")
    if args.ros_domain_id is not None and not 0 <= args.ros_domain_id <= 232:
        raise ValueError("--ros-domain-id must be in the portable range [0, 232]")
    minimum_timeout = (
        args.duration_sec
        + args.discovery_warmup_sec
        + args.startup_timeout_sec
        + args.shutdown_timeout_sec
        + 20.0
    )
    if args.child_timeout_sec < minimum_timeout:
        raise ValueError(
            f"--child-timeout-sec must be at least {minimum_timeout:g} for this workload"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", args.matrix_id):
        raise ValueError("--matrix-id must use 1-40 letters, digits, dots, underscores, or hyphens")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("--output-dir must not exist or must be empty")

    public_run = not args.exploratory
    provenance = (
        _public_provenance(args, repo) if public_run else _exploratory_provenance(args, repo)
    )
    publish_target = _publish_path(args, repo, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = output_dir / "runs"
    runs_dir.mkdir()

    child_schema = json.loads(
        (repo / "scripts" / "native_capture_benchmark.schema.json").read_text(encoding="utf-8")
    )
    child_validator = Draft202012Validator(child_schema)
    schedule = _counterbalanced_schedule(args.repetitions, args.first_backend)
    workload_arguments = _workload_arguments(args)
    child_env = os.environ.copy()
    if provenance.get("rmw_implementation"):
        child_env["RMW_IMPLEMENTATION"] = str(provenance["rmw_implementation"])
    if provenance.get("compiler"):
        child_env["CXX"] = str(provenance["compiler"])
    if provenance.get("build_type"):
        child_env["CMAKE_BUILD_TYPE"] = str(provenance["build_type"])
    if provenance.get("ros_domain_id") is not None:
        child_env["ROS_DOMAIN_ID"] = str(provenance["ros_domain_id"])

    expected_build = {
        "ros_distro": provenance.get("ros_distro"),
        "rmw_implementation": provenance.get("rmw_implementation"),
        "compiler": provenance.get("compiler"),
        "build_type": provenance.get("build_type"),
    }
    expected_workload: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    artifacts_by_backend: dict[str, list[dict[str, Any]]] = {"native": [], "rosbag2": []}
    valid_artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    json_paths: list[Path] = []

    for launch_index, (pair_index, backend) in enumerate(schedule, start=1):
        if public_run and not _git_is_clean(repo):
            errors.append(f"launch {launch_index}: git tree became dirty during the matrix")
            break
        filename = f"{launch_index:02d}_{backend}_pair{pair_index:02d}.json"
        output_path = runs_dir / filename
        run_id = f"{args.matrix_id}-{pair_index:02d}-{backend}"
        command = _child_command(args, repo, provenance, backend, output_path, run_id)
        try:
            completed = runner(
                command,
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
                timeout=args.child_timeout_sec,
                env=child_env,
            )
            returncode = completed.returncode
            launch_error: str | None = None
        except subprocess.TimeoutExpired:
            returncode = -1
            launch_error = f"child exceeded the {args.child_timeout_sec:g} second timeout"

        artifact = _read_json(output_path)
        artifact_hash = _sha256(output_path) if output_path.is_file() else None
        if output_path.is_file():
            json_paths.append(output_path)
        child_errors = _child_errors(
            artifact,
            returncode=returncode,
            backend=backend,
            expected_git_sha=provenance.get("git_sha"),
            expected_build=expected_build,
            expected_workload=expected_workload,
            schema_validator=child_validator,
            public_run=public_run,
        )
        if launch_error:
            child_errors.insert(0, launch_error)
        if artifact is not None and expected_workload is None:
            expected_workload = _workload(artifact)

        record = {
            "launch_index": launch_index,
            "pair_index": pair_index,
            "position_in_pair": ((launch_index - 1) % 2) + 1,
            "backend": backend,
            "artifact": f"runs/{filename}",
            "sha256": artifact_hash,
            "valid": not child_errors,
            "errors": child_errors,
        }
        records.append(record)
        if not child_errors and artifact is not None:
            artifacts_by_backend[backend].append(artifact)
            valid_artifacts.append(artifact)
        else:
            errors.extend(f"launch {launch_index}: {error}" for error in child_errors)
            break

    if public_run and not errors and not _git_is_clean(repo):
        errors.append("git tree became dirty before matrix finalization")
    complete = len(records) == len(schedule)
    if not complete and not errors:
        errors.append("matrix did not execute its complete schedule")

    backend_summaries: dict[str, Any] = {}
    for backend in ("native", "rosbag2"):
        backend_summaries[backend] = {
            "launch_count": len(artifacts_by_backend[backend]),
            "expected_launch_count": args.repetitions,
            "all_valid": len(artifacts_by_backend[backend]) == args.repetitions,
            "metrics": _metric_summary(artifacts_by_backend[backend]),
        }

    last_generated_at = next(
        (
            artifact.get("generated_at")
            for artifact in reversed(valid_artifacts)
            if isinstance(artifact.get("generated_at"), str)
        ),
        None,
    )
    summary: dict[str, Any] = {
        "schema_version": MATRIX_SCHEMA,
        "generated_at": last_generated_at,
        "matrix_id": args.matrix_id,
        "publication": {
            "eligible": public_run and not errors and complete,
            "exploratory": args.exploratory,
            "retained_evidence_path": publish_target[1] if publish_target else None,
            "retained_schema_paths": (
                {
                    "child_artifact": "schemas/native_capture_benchmark.schema.json",
                    "matrix_summary": "schemas/native_capture_benchmark_matrix.schema.json",
                }
                if publish_target
                else None
            ),
            "note": (
                "Exploratory matrices are never publication-eligible."
                if args.exploratory
                else "Publication eligibility requires every child and every preflight gate."
            ),
        },
        "validity": {
            "valid": not errors and complete,
            "all_child_artifacts_valid": not errors and complete,
            "errors": errors,
        },
        "provenance": provenance,
        "schedule": {
            "strategy": "paired order alternates AB then BA",
            "first_backend": args.first_backend,
            "repetitions_per_backend": args.repetitions,
            "workload_arguments": workload_arguments,
            "child_timeout_sec": args.child_timeout_sec,
            "runs": records,
        },
        "workload": expected_workload,
        "backends": backend_summaries,
        "comparison_scope": {
            "included_metrics": list(COMPARABLE_METRICS),
            "excluded_metrics": EXCLUDED_METRICS,
            "durability_equivalence_claimed": False,
            "aggregation": "linear-interpolated median and p95 across per-run statistics",
            "null_policy": (
                "if any run reports null or non-numeric for a metric, that backend aggregate "
                "remains null and unsupported"
            ),
        },
    }
    matrix_schema = json.loads(
        (repo / "scripts" / "native_capture_benchmark_matrix.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(matrix_schema).validate(summary)
    summary_path = output_dir / "summary.json"
    _atomic_json(summary_path, summary)
    json_paths.append(summary_path)
    _write_checksums(output_dir, json_paths)
    if summary["validity"]["valid"] and publish_target is not None:
        _publish_evidence(repo, output_dir, publish_target[0])
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/native_capture_matrix"))
    parser.add_argument(
        "--publish-dir",
        type=Path,
        help="After a valid public matrix, atomically copy commit-ready evidence here",
    )
    parser.add_argument("--matrix-id", default="matched")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--first-backend", choices=sorted(BACKEND_LABELS), default="native")
    parser.add_argument(
        "--exploratory",
        action="store_true",
        help="Bypass public provenance gates and mark the result non-public",
    )
    parser.add_argument("--install-prefix", type=Path)
    parser.add_argument("--cmake-cache", type=Path)
    parser.add_argument("--ros-distro")
    parser.add_argument("--rmw-implementation")
    parser.add_argument("--ros-domain-id", type=int)
    parser.add_argument("--child-timeout-sec", type=float, default=180.0)
    parser.add_argument("--scenario", choices=[*"ABCDEFG", "custom"], default="C")
    parser.add_argument("--topics", type=int, default=10)
    parser.add_argument("--rate", type=float, default=10_000.0)
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--discovery-warmup-sec", type=float, default=2.0)
    parser.add_argument("--qos", choices=["best_effort", "reliable"], default="best_effort")
    parser.add_argument("--qos-depth", type=int, default=10)
    parser.add_argument("--topic-prefix", default="/blackbox_bench/topic")
    parser.add_argument("--burst-every-sec", type=float, default=0.0)
    parser.add_argument("--burst-duration-ms", type=float, default=0.0)
    parser.add_argument("--burst-multiplier", type=float, default=1.0)
    parser.add_argument("--churn-every-sec", type=float, default=0.0)
    parser.add_argument("--churn-down-ms", type=float, default=0.0)
    parser.add_argument("--sample-period-sec", type=float, default=1.0)
    parser.add_argument("--startup-timeout-sec", type=float, default=15.0)
    parser.add_argument("--shutdown-timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--capture-command", default="ros2 run blackbox_capture_cpp blackbox_capture"
    )
    parser.add_argument(
        "--publisher-command", default="ros2 run blackbox_capture_bench publisher"
    )
    parser.add_argument("--rosbag2-command", default="ros2 bag record")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        summary = run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "summary": str(args.output_dir / "summary.json"),
                "valid": summary["validity"]["valid"],
                "publication_eligible": summary["publication"]["eligible"],
            }
        )
    )
    return 0 if summary["validity"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
