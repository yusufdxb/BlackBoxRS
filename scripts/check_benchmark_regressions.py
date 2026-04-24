#!/usr/bin/env python3
"""Fail CI when benchmark results regress past conservative thresholds."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    text = path.read_text().strip()
    if not text:
        raise ValueError(f"{path} is empty")
    if text.startswith("{"):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _normalise_benchmarks(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict):
        benches = payload.get("benchmarks")
        if not isinstance(benches, list):
            raise ValueError("structured benchmark JSON must contain a 'benchmarks' list")
    elif isinstance(payload, list):
        benches = payload
    else:
        raise ValueError("unsupported benchmark payload")

    normalised: dict[str, dict[str, Any]] = {}
    for bench in benches:
        name = bench.get("bench")
        if not isinstance(name, str):
            raise ValueError("each benchmark result must include a string 'bench' field")
        normalised[name] = bench
    return normalised


def _format_number(value: Any) -> str:
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:,.2f}"
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _resolve_min_threshold(rule: dict[str, Any], baseline_value: Any) -> float:
    candidates: list[float] = []
    if "min" in rule:
        candidates.append(float(rule["min"]))
    if baseline_value is not None and "min_ratio_to_baseline" in rule:
        candidates.append(float(baseline_value) * float(rule["min_ratio_to_baseline"]))
    if not candidates:
        raise ValueError("min rule needs 'min' and/or 'min_ratio_to_baseline'")
    return max(candidates)


def _resolve_max_threshold(rule: dict[str, Any], baseline_value: Any) -> float:
    candidates: list[float] = []
    if "max" in rule:
        candidates.append(float(rule["max"]))
    if baseline_value is not None and "max_ratio_to_baseline" in rule:
        candidates.append(float(baseline_value) * float(rule["max_ratio_to_baseline"]))
    if not candidates:
        raise ValueError("max rule needs 'max' and/or 'max_ratio_to_baseline'")
    return min(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()

    results = _normalise_benchmarks(_load_json(args.results))
    thresholds = _load_json(args.thresholds)
    if not isinstance(thresholds, dict) or "benchmarks" not in thresholds:
        raise ValueError("threshold file must be an object with a 'benchmarks' key")

    baseline = {}
    if args.baseline is not None:
        baseline = _normalise_benchmarks(_load_json(args.baseline))

    failures: list[str] = []
    passes: list[str] = []

    for bench_name, bench_rules in thresholds["benchmarks"].items():
        if bench_name not in results:
            failures.append(f"{bench_name}: missing from benchmark results")
            continue

        result = results[bench_name]
        baseline_result = baseline.get(bench_name, {})

        for key, expected in bench_rules.get("inputs", {}).items():
            actual = result.get(key)
            if actual != expected:
                failures.append(
                    f"{bench_name}.{key}: expected input {expected!r}, got {actual!r}"
                )

        for metric, rule in bench_rules.get("metrics", {}).items():
            if metric not in result:
                failures.append(f"{bench_name}.{metric}: metric missing from benchmark output")
                continue

            actual = result[metric]
            baseline_value = baseline_result.get(metric)
            direction = rule.get("direction")
            unit = rule.get("unit", "")
            unit_suffix = f" {unit}".rstrip()

            if direction == "min":
                threshold = _resolve_min_threshold(rule, baseline_value)
                if float(actual) < threshold:
                    failures.append(
                        f"{bench_name}.{metric}: actual {_format_number(actual)}{unit_suffix} "
                        f"< required {_format_number(threshold)}{unit_suffix} "
                        f"(baseline: {_format_number(baseline_value) if baseline_value is not None else 'n/a'})"
                    )
                else:
                    passes.append(
                        f"{bench_name}.{metric}: {_format_number(actual)}{unit_suffix} "
                        f">= {_format_number(threshold)}{unit_suffix}"
                    )
            elif direction == "max":
                threshold = _resolve_max_threshold(rule, baseline_value)
                if float(actual) > threshold:
                    failures.append(
                        f"{bench_name}.{metric}: actual {_format_number(actual)}{unit_suffix} "
                        f"> allowed {_format_number(threshold)}{unit_suffix} "
                        f"(baseline: {_format_number(baseline_value) if baseline_value is not None else 'n/a'})"
                    )
                else:
                    passes.append(
                        f"{bench_name}.{metric}: {_format_number(actual)}{unit_suffix} "
                        f"<= {_format_number(threshold)}{unit_suffix}"
                    )
            else:
                raise ValueError(
                    f"{bench_name}.{metric}: direction must be 'min' or 'max', got {direction!r}"
                )

    for line in passes:
        print(f"PASS {line}")

    if failures:
        for line in failures:
            print(f"FAIL {line}", file=sys.stderr)
        return 1

    print("Benchmark regression gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
