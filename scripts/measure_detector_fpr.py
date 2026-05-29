"""Measure detector false-positive and true-positive rates on synthetic streams.

Runs each of the 7 BlackBoxRS detectors against:
  1. a "healthy" stream of N hours of 1Hz samples drawn from a calibrated
     noise distribution, counts firings as false positives;
  2. a "violation" stream with one injected violation per hour, counts firings
     as true positives.

This is a deliberately synthetic measurement: real-robot calibration is owed,
see docs/DETECTOR_CHARACTERISTICS.md for the explicit list of what is NOT
measured. Output is intended to bound (not replace) the eventual
real-telemetry measurement.

Usage::

    python scripts/measure_detector_fpr.py --hours 24
    python scripts/measure_detector_fpr.py --hours 1 --json-output artifacts/detector_characteristics.json
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from blackboxrs.anomaly_engine.detectors.clock_skew import ClockSkewDetector
from blackboxrs.anomaly_engine.detectors.dead_topic import DeadTopicDetector
from blackboxrs.anomaly_engine.detectors.frequency import FrequencyDetector
from blackboxrs.anomaly_engine.detectors.process_signals import (
    ProcessSignalsDetector,
)
from blackboxrs.anomaly_engine.detectors.qos_mismatch import QoSMismatchDetector
from blackboxrs.anomaly_engine.detectors.tf_topology import TfTopologyDetector
from blackboxrs.anomaly_engine.detectors.threshold import ThresholdDetector
from blackboxrs.core.config import (
    AnomalyThresholds,
    ClockSkewConfig,
    DeadTopicConfig,
    FrequencyConfig,
    ProcessSignalsConfig,
    TfTopologyConfig,
)
from blackboxrs.core.schemas import BlackBoxEvent

_SEED = 0xB1ACB07
_TARGET_HZ = 10.0


@dataclass
class Result:
    detector: str
    samples: int
    healthy_fires: int
    violation_fires: int
    violations_injected: int
    median_time_to_fire_sec: float | None

    @property
    def fpr_per_hour(self) -> float:
        # Each sample is 1 simulated second; fires/hour = fires / (samples/3600).
        hours = self.samples / 3600.0
        return self.healthy_fires / hours if hours > 0 else 0.0

    @property
    def tpr(self) -> float:
        if self.violations_injected == 0:
            return 0.0
        return min(1.0, self.violation_fires / self.violations_injected)


def _hours_to_samples(hours: float) -> int:
    return int(hours * 3600)


def _ros_event(event_type: str, data: dict[str, Any]) -> BlackBoxEvent:
    return BlackBoxEvent.ros_event(event_type=event_type, data=data)


def _sys_event(event_type: str, data: dict[str, Any]) -> BlackBoxEvent:
    return BlackBoxEvent.system_event(event_type=event_type, data=data)


def _run(
    detector,
    stream: Callable[[random.Random, int], BlackBoxEvent],
    samples: int,
    rng: random.Random,
) -> tuple[int, list[int]]:
    fires = 0
    fire_indices: list[int] = []
    for i in range(samples):
        ev = stream(rng, i)
        if detector.check(ev) is not None:
            fires += 1
            fire_indices.append(i)
    return fires, fire_indices


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# ---------------------------------------------------------------------------
# Stream definitions
# ---------------------------------------------------------------------------


def threshold_healthy(rng: random.Random, _i: int) -> BlackBoxEvent:
    cpu = max(0.0, min(100.0, rng.gauss(35.0, 8.0)))
    return _sys_event("system.cpu", {"cpu_percent": cpu})


def threshold_violation(rng: random.Random, i: int) -> BlackBoxEvent:
    if i % 3600 in (1800, 1801):
        return _sys_event("system.cpu", {"cpu_percent": 96.0})
    return threshold_healthy(rng, i)


def frequency_healthy(rng: random.Random, _i: int) -> BlackBoxEvent:
    hz = max(0.1, rng.gauss(_TARGET_HZ, _TARGET_HZ * 0.05))
    return _ros_event("ros.frequency", {"topic": "/scan", "frequency_hz": hz})


def frequency_violation(rng: random.Random, i: int) -> BlackBoxEvent:
    if i % 3600 in (1800, 1801):
        return _ros_event(
            "ros.frequency", {"topic": "/scan", "frequency_hz": 1.0}
        )
    return frequency_healthy(rng, i)


def dead_topic_healthy(_rng: random.Random, _i: int) -> BlackBoxEvent:
    # Always-fresh frequency keeps the topic alive.
    return _ros_event("ros.frequency", {"topic": "/scan", "frequency_hz": 10.0})


def dead_topic_violation(_rng: random.Random, i: int) -> BlackBoxEvent:
    # Per-hour 7-second silence window simulated as off-topic noise.
    in_silence = (i % 3600) >= 1800 and (i % 3600) < 1807
    if in_silence:
        return _ros_event(
            "ros.frequency", {"topic": "/other", "frequency_hz": 5.0}
        )
    return _ros_event("ros.frequency", {"topic": "/scan", "frequency_hz": 10.0})


def qos_healthy(_rng: random.Random, _i: int) -> BlackBoxEvent:
    return _ros_event(
        "ros.qos",
        {
            "topic": "/scan",
            "publisher_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "VOLATILE"}
            ],
            "subscriber_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "VOLATILE"}
            ],
        },
    )


def qos_violation(_rng: random.Random, i: int) -> BlackBoxEvent:
    # Mismatch direction: subscriber is stricter than publisher (RELIABLE > BEST_EFFORT).
    if i % 3600 in (1800, 1801):
        return _ros_event(
            "ros.qos",
            {
                "topic": "/scan",
                "publisher_qos_profiles": [
                    {"reliability": "BEST_EFFORT", "durability": "VOLATILE"}
                ],
                "subscriber_qos_profiles": [
                    {"reliability": "RELIABLE", "durability": "VOLATILE"}
                ],
            },
        )
    return qos_healthy(_rng, i)


def tf_healthy(_rng: random.Random, _i: int) -> BlackBoxEvent:
    return _ros_event(
        "ros.tf",
        {
            "expected_frames": ["base_link", "odom"],
            "edges": [
                {
                    "parent": "odom",
                    "child": "base_link",
                    "last_update_sec_ago": 0.1,
                    "is_static": False,
                }
            ],
        },
    )


def tf_violation(_rng: random.Random, i: int) -> BlackBoxEvent:
    if i % 3600 in (1800, 1801):
        return _ros_event(
            "ros.tf",
            {
                "expected_frames": ["base_link", "odom"],
                "edges": [
                    {
                        "parent": "odom",
                        "child": "base_link",
                        "last_update_sec_ago": 9.5,
                        "is_static": False,
                    }
                ],
            },
        )
    return tf_healthy(_rng, i)


def clock_healthy(rng: random.Random, _i: int) -> BlackBoxEvent:
    base = 1_700_000_000.0
    return _sys_event(
        "system.clock_skew",
        {
            "sources": [
                {"name": "system", "epoch_sec": base + rng.gauss(0, 0.005)},
                {"name": "ntp:pool", "epoch_sec": base + rng.gauss(0, 0.005)},
            ]
        },
    )


def clock_violation(rng: random.Random, i: int) -> BlackBoxEvent:
    if i % 3600 in (1800, 1801):
        base = 1_700_000_000.0
        return _sys_event(
            "system.clock_skew",
            {
                "sources": [
                    {"name": "system", "epoch_sec": base},
                    {"name": "ntp:pool", "epoch_sec": base + 0.42},
                ]
            },
        )
    return clock_healthy(rng, i)


def process_healthy(rng: random.Random, _i: int) -> BlackBoxEvent:
    procs = []
    for name in ("scan_node", "controller", "planner", "tf_broadcaster", "ros2"):
        procs.append(
            {
                "pid": 1000 + hash(name) % 9000,
                "name": name,
                "cpu_percent": max(0.0, rng.gauss(15.0, 5.0)),
                "rss_mb": max(20.0, rng.gauss(150.0, 30.0)),
            }
        )
    return _sys_event(
        "system.process_signals",
        {"sampling_interval_sec": 1.0, "processes": procs},
    )


def process_violation(rng: random.Random, i: int) -> BlackBoxEvent:
    if i % 3600 in (1800, 1801):
        procs = [
            {
                "pid": 1234,
                "name": "scan_node",
                "cpu_percent": 96.0,
                "rss_mb": 180.0,
            },
            {
                "pid": 1235,
                "name": "controller",
                "cpu_percent": 12.0,
                "rss_mb": 95.0,
            },
        ]
        return _sys_event(
            "system.process_signals",
            {"sampling_interval_sec": 1.0, "processes": procs},
        )
    return process_healthy(rng, i)


# ---------------------------------------------------------------------------
# Detector + stream registry
# ---------------------------------------------------------------------------


def _registry() -> list[tuple[str, Callable, Callable, Callable, int]]:
    # (detector_name, detector_factory, healthy_stream, violation_stream, expected_violations_per_hour)
    return [
        (
            "threshold",
            lambda: ThresholdDetector(AnomalyThresholds()),
            threshold_healthy,
            threshold_violation,
            1,
        ),
        (
            "frequency",
            lambda: FrequencyDetector(FrequencyConfig()),
            frequency_healthy,
            frequency_violation,
            1,
        ),
        (
            "dead_topic",
            lambda: DeadTopicDetector(DeadTopicConfig()),
            dead_topic_healthy,
            dead_topic_violation,
            1,
        ),
        (
            "qos_mismatch",
            lambda: QoSMismatchDetector(),
            qos_healthy,
            qos_violation,
            1,
        ),
        (
            "tf_topology",
            lambda: TfTopologyDetector(TfTopologyConfig()),
            tf_healthy,
            tf_violation,
            1,
        ),
        (
            "clock_skew",
            lambda: ClockSkewDetector(ClockSkewConfig()),
            clock_healthy,
            clock_violation,
            1,
        ),
        (
            "process_signals",
            lambda: ProcessSignalsDetector(ProcessSignalsConfig()),
            process_healthy,
            process_violation,
            1,
        ),
    ]


def measure(hours: float) -> list[Result]:
    samples = _hours_to_samples(hours)
    results: list[Result] = []
    for name, factory, healthy, violation, per_hr in _registry():
        rng_h = random.Random(_SEED)
        det_h = factory()
        healthy_fires, _ = _run(det_h, healthy, samples, rng_h)

        rng_v = random.Random(_SEED + 1)
        det_v = factory()
        violation_fires, fire_indices = _run(det_v, violation, samples, rng_v)

        # Time-to-fire: distance from injected violation index (1800 each hour) to first fire.
        ttf: list[float] = []
        for fire_i in fire_indices:
            hour_start = (fire_i // 3600) * 3600
            injection = hour_start + 1800
            if fire_i >= injection:
                ttf.append(float(fire_i - injection))

        results.append(
            Result(
                detector=name,
                samples=samples,
                healthy_fires=healthy_fires,
                violation_fires=violation_fires,
                violations_injected=int(per_hr * hours),
                median_time_to_fire_sec=_median(ttf),
            )
        )
    return results


def render_table(results: list[Result]) -> str:
    lines = []
    lines.append("| Detector | FPR (fires/hr) | TPR | Median time-to-fire (s) |")
    lines.append("|---|---|---|---|")
    for r in results:
        ttf = (
            f"{r.median_time_to_fire_sec:.1f}"
            if r.median_time_to_fire_sec is not None
            else "n/a"
        )
        lines.append(
            f"| `{r.detector}` | {r.fpr_per_hour:.3f} | {r.tpr:.2f} | {ttf} |"
        )
    return "\n".join(lines)


def to_jsonable(results: list[Result], hours: float) -> dict[str, Any]:
    return {
        "hours": hours,
        "seed": _SEED,
        "detectors": [
            {
                "detector": r.detector,
                "samples": r.samples,
                "healthy_fires": r.healthy_fires,
                "violation_fires": r.violation_fires,
                "violations_injected": r.violations_injected,
                "fpr_per_hour": round(r.fpr_per_hour, 4),
                "tpr": round(r.tpr, 4),
                "median_time_to_fire_sec": r.median_time_to_fire_sec,
            }
            for r in results
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--json-output", type=str, default=None)
    args = p.parse_args()

    t0 = time.monotonic()
    results = measure(args.hours)
    elapsed = time.monotonic() - t0

    print(f"# Detector characteristics (synthetic, hours={args.hours})")
    print(f"# wall-clock: {elapsed:.1f}s, seed=0x{_SEED:X}")
    print()
    print(render_table(results))

    if args.json_output:
        out = Path(args.json_output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(to_jsonable(results, args.hours), indent=2))
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
