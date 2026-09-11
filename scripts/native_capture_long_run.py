#!/usr/bin/env python3
"""Run the native capture benchmark as a two-to-eight-hour stability study."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "blackboxrs.capture_long_run.v1"


def _slope_mb_per_hour(samples: list[dict[str, Any]], warmup_sec: float) -> float | None:
    points = [
        (float(sample["t_sec"]), float(sample["rss_mb"]))
        for sample in samples
        if float(sample.get("t_sec", 0.0)) >= warmup_sec
    ]
    if len(points) < 3:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator <= 0.0:
        return None
    slope_mb_per_sec = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return slope_mb_per_sec * 3600.0


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/native_capture_long_run.json")
    )
    parser.add_argument("--duration-hours", type=float, default=4.0)
    parser.add_argument(
        "--duration-sec", type=float, help="Short smoke duration, requires --allow-short"
    )
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--post-warmup-sec", type=float, default=600.0)
    parser.add_argument("--growth-threshold-mb-per-hour", type=float, default=5.0)
    parser.add_argument("--sample-period-sec", type=float, default=1.0)
    parser.add_argument("--topics", type=int, default=10)
    parser.add_argument("--rate", type=float, default=1_000.0)
    parser.add_argument("--payload-bytes", type=int, default=1_024)
    parser.add_argument("--qos", choices=["best_effort", "reliable"], default="best_effort")
    parser.add_argument(
        "--capture-command", default="ros2 run blackbox_capture_cpp blackbox_capture"
    )
    parser.add_argument("--publisher-command", default="ros2 run blackbox_capture_bench publisher")
    parser.add_argument("--recorder-params", type=Path)
    parser.add_argument("--capture-output-dir", type=Path)
    parser.add_argument("--startup-timeout-sec", type=float, default=15.0)
    parser.add_argument("--shutdown-timeout-sec", type=float, default=30.0)
    parser.add_argument("--keep-work-directory", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.duration_sec is not None:
        if not args.allow_short:
            parser.error("--duration-sec is reserved for smoke tests and requires --allow-short")
        duration_sec = args.duration_sec
    else:
        if not 2.0 <= args.duration_hours <= 8.0:
            parser.error("duration-hours must be between 2 and 8")
        duration_sec = args.duration_hours * 3600.0
    if duration_sec <= 0.0 or args.post_warmup_sec < 0.0:
        parser.error("duration must be positive and post-warmup must be non-negative")
    if args.recorder_params is not None and args.capture_output_dir is None:
        parser.error("--recorder-params requires the matching --capture-output-dir")
    if (
        not math.isfinite(args.growth_threshold_mb_per_hour)
        or args.growth_threshold_mb_per_hour < 0.0
    ):
        parser.error("growth threshold must be finite and non-negative")

    benchmark_script = Path(__file__).with_name("native_capture_benchmark.py")
    temporary_context: tempfile.TemporaryDirectory[str] | None = None
    if args.keep_work_directory:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(
            tempfile.mkdtemp(
                prefix="native_capture_long_run_work_",
                dir=args.output.resolve().parent,
            )
        )
    else:
        temporary_context = tempfile.TemporaryDirectory(prefix="blackboxrs-long-run-")
        workspace = Path(temporary_context.name)
    try:
        benchmark_output = workspace / "benchmark.json"
        command = [
            sys.executable,
            str(benchmark_script),
            "--output",
            str(benchmark_output),
            "--scenario",
            "custom",
            "--duration-sec",
            str(duration_sec),
            "--topics",
            str(args.topics),
            "--rate",
            str(args.rate),
            "--payload-bytes",
            str(args.payload_bytes),
            "--qos",
            args.qos,
            "--sample-period-sec",
            str(args.sample_period_sec),
            "--startup-timeout-sec",
            str(args.startup_timeout_sec),
            "--shutdown-timeout-sec",
            str(args.shutdown_timeout_sec),
            "--capture-command",
            args.capture_command,
            "--publisher-command",
            args.publisher_command,
            "--include-samples",
        ]
        if args.recorder_params:
            command.extend(["--recorder-params", str(args.recorder_params)])
        if args.capture_output_dir:
            command.extend(["--capture-output-dir", str(args.capture_output_dir)])
        if args.keep_work_directory:
            command.append("--keep-work-directory")
        completed = subprocess.run(command, check=False)
        if not benchmark_output.exists():
            return completed.returncode or 2
        benchmark = json.loads(benchmark_output.read_text(encoding="utf-8"))
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()

    samples = benchmark.pop("resource_samples", [])
    slope = _slope_mb_per_hour(samples, args.post_warmup_sec)
    observed_span = (
        float(samples[-1]["t_sec"]) - float(samples[0]["t_sec"]) if len(samples) >= 2 else 0.0
    )
    enough_evidence = (
        duration_sec >= 2.0 * 3600.0 and len(samples) >= 3 and observed_span >= duration_sec * 0.8
    )
    if not enough_evidence or slope is None:
        trend = "inconclusive"
    elif slope > args.growth_threshold_mb_per_hour:
        trend = "growth_observed"
    else:
        trend = "within_configured_threshold"

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": benchmark,
        "stability": {
            "requested_duration_sec": duration_sec,
            "sample_period_sec": args.sample_period_sec,
            "post_warmup_sec": args.post_warmup_sec,
            "sample_count": len(samples),
            "observed_span_sec": observed_span,
            "rss_slope_mb_per_hour": slope,
            "configured_growth_threshold_mb_per_hour": args.growth_threshold_mb_per_hour,
            "trend": trend,
            "threshold_is_a_user_selected_screen_not_a_production_claim": True,
            "allocator_metric_available": False,
            "allocator_metric_reason": (
                "the recorder does not export allocator telemetry; this artifact measures RSS"
            ),
            "first_closed_segment_count": samples[0]["closed_segments"] if samples else None,
            "last_closed_segment_count": samples[-1]["closed_segments"] if samples else None,
            "first_retention_evicted_segment_count": (
                samples[0].get("retention_evicted_segments") if samples else None
            ),
            "last_retention_evicted_segment_count": (
                samples[-1].get("retention_evicted_segments") if samples else None
            ),
        },
        "samples": samples,
    }
    _atomic_json(args.output.resolve(), artifact)
    print(
        json.dumps(
            {"output": str(args.output), "valid": benchmark["validity"]["valid"], "trend": trend}
        )
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
