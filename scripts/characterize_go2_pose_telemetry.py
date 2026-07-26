#!/usr/bin/env python3
"""Characterize the genuine GO2 PoseStamped stream and select v1 thresholds."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path
from typing import Iterable

import rosbag2_py
import yaml
from geometry_msgs.msg import PoseStamped
from rclpy.serialization import deserialize_message

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.prevention.telemetry_health import (  # noqa: E402
    HealthyTelemetryStatistics,
    TelemetryHealthEvidence,
    compute_evidence_fingerprint,
    derive_thresholds,
)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def _rolling_counts(
    arrival_sec: list[float], window_sec: float, *, step_sec: float = 0.1
) -> list[int]:
    limit = arrival_sec[-1] - window_sec
    starts: list[float] = []
    current = arrival_sec[0]
    while current <= limit + 1e-12:
        starts.append(current)
        current += step_sec
    return [
        bisect.bisect_right(arrival_sec, start + window_sec)
        - bisect.bisect_left(arrival_sec, start)
        for start in starts
    ]


def _bootstrap_confidence(intervals: list[float]) -> dict[str, list[float]]:
    rng = random.Random(7)
    mean_rates: list[float] = []
    medians: list[float] = []
    p99s: list[float] = []
    for _ in range(500):
        sample = [intervals[rng.randrange(len(intervals))] for _ in intervals]
        mean_rates.append(1.0 / statistics.fmean(sample))
        medians.append(1.0 / statistics.median(sample))
        p99s.append(_percentile(sample, 99.0))
    return {
        "mean_rate_hz_95pct_bootstrap": [
            _percentile(mean_rates, 2.5),
            _percentile(mean_rates, 97.5),
        ],
        "median_rate_hz_95pct_bootstrap": [
            _percentile(medians, 2.5),
            _percentile(medians, 97.5),
        ],
        "p99_inter_arrival_sec_95pct_bootstrap": [
            _percentile(p99s, 2.5),
            _percentile(p99s, 97.5),
        ],
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_bag(bag: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.name):
        digest.update(path.relative_to(bag).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _metadata_for_topic(metadata: dict, topic: str) -> tuple[dict, dict]:
    info = metadata["rosbag2_bagfile_information"]
    for item in info["topics_with_message_count"]:
        topic_metadata = item["topic_metadata"]
        if topic_metadata["name"] != topic:
            continue
        offered = yaml.safe_load(topic_metadata["offered_qos_profiles"])
        if len(offered) != 1:
            raise ValueError("Expected exactly one offered QoS profile")
        raw = offered[0]
        if (
            raw["history"],
            raw["depth"],
            raw["reliability"],
            raw["durability"],
        ) != (1, 1, 1, 2):
            raise ValueError(f"Unexpected GO2 pose QoS metadata: {raw!r}")
        qos = {
            "history": "keep_last",
            "depth": 1,
            "reliability": "reliable",
            "durability": "volatile",
        }
        return topic_metadata, qos
    raise ValueError(f"Topic {topic!r} not found in metadata")


def characterize(
    bag: Path,
    *,
    topic: str,
    graph_context: str,
) -> TelemetryHealthEvidence:
    metadata_path = bag / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    info = metadata["rosbag2_bagfile_information"]
    topic_metadata, offered_qos = _metadata_for_topic(metadata, topic)
    if topic_metadata["type"] != "geometry_msgs/msg/PoseStamped":
        raise ValueError("This bounded analyzer requires PoseStamped")

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id="sqlite3"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    bag_first_ns: int | None = None
    arrivals_ns: list[int] = []
    headers_ns: list[int] = []
    pose_values: list[tuple[float, ...]] = []
    while reader.has_next():
        observed_topic, data, timestamp_ns = reader.read_next()
        if bag_first_ns is None:
            bag_first_ns = timestamp_ns
        if observed_topic != topic:
            continue
        msg = deserialize_message(data, PoseStamped)
        arrivals_ns.append(timestamp_ns)
        headers_ns.append(
            msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        )
        p = msg.pose.position
        q = msg.pose.orientation
        pose_values.append((p.x, p.y, p.z, q.x, q.y, q.z, q.w))

    if bag_first_ns is None or len(arrivals_ns) < 2:
        raise ValueError("Bag does not contain enough pose messages")
    intervals = [
        (current - previous) / 1e9
        for previous, current in zip(arrivals_ns, arrivals_ns[1:])
    ]
    header_deltas = [
        current - previous
        for previous, current in zip(headers_ns, headers_ns[1:])
    ]
    arrival_sec = [value / 1e9 for value in arrivals_ns]
    duration_sec = (arrivals_ns[-1] - arrivals_ns[0]) / 1e9

    rolling: dict[str, dict[str, float]] = {}
    rolling_counts: dict[str, list[int]] = {}
    for window_sec in (0.5, 1.0, 2.0, 3.0, 5.0):
        counts = _rolling_counts(arrival_sec, window_sec)
        key = f"{window_sec:g}s"
        rolling_counts[key] = counts
        rates = [count / window_sec for count in counts]
        rolling[key] = {
            "minimum": min(rates),
            "p01": _percentile(rates, 1.0),
            "p05": _percentile(rates, 5.0),
            "median": statistics.median(rates),
            "maximum": max(rates),
            "window_count": float(len(rates)),
        }

    nonfinite = sum(
        1 for vector in pose_values for value in vector if not math.isfinite(value)
    )
    exact_repeats = sum(
        current == previous
        for previous, current in zip(pose_values, pose_values[1:])
    )
    stats = HealthyTelemetryStatistics(
        message_count=len(arrivals_ns),
        startup_delay_sec=(arrivals_ns[0] - bag_first_ns) / 1e9,
        observed_duration_sec=duration_sec,
        mean_rate_hz=(len(arrivals_ns) - 1) / duration_sec,
        median_rate_hz=1.0 / statistics.median(intervals),
        inter_arrival_sec={
            "minimum": min(intervals),
            "mean": statistics.fmean(intervals),
            "median": statistics.median(intervals),
            "p90": _percentile(intervals, 90.0),
            "p95": _percentile(intervals, 95.0),
            "p99": _percentile(intervals, 99.0),
            "p99_5": _percentile(intervals, 99.5),
            "p99_9": _percentile(intervals, 99.9),
            "max": max(intervals),
        },
        rolling_rate_hz=rolling,
        header_nonprogressing_deltas=sum(delta <= 0 for delta in header_deltas),
        header_frozen_deltas=sum(delta == 0 for delta in header_deltas),
        header_negative_deltas=sum(delta < 0 for delta in header_deltas),
        payload_nonfinite_values=nonfinite,
        consecutive_exact_pose_repeats=exact_repeats,
        unique_pose_vectors=len(set(pose_values)),
    )
    thresholds = derive_thresholds(stats)
    two_second_counts = rolling_counts["2s"]
    below_minimum = sum(
        count / thresholds.rate_window_sec < thresholds.minimum_rate_hz
        for count in two_second_counts
    )
    confidence = _bootstrap_confidence(intervals)
    confidence.update(
        {
            "interval_sample_count": len(intervals),
            "zero_gaps_over_stale_timeout": sum(
                gap > thresholds.stale_timeout_sec for gap in intervals
            ),
            "rule_of_three_upper_gap_violation_probability_95pct": (
                3.0 / len(intervals)
            ),
            "two_second_window_count": len(two_second_counts),
            "two_second_windows_below_minimum_rate": below_minimum,
            "rule_of_three_upper_window_violation_probability_95pct": (
                3.0 / len(two_second_counts)
            ),
            "caveat": (
                "Bootstrap intervals and overlapping rolling windows are "
                "descriptive; adjacent samples are not independent."
            ),
        }
    )

    bag_files = [metadata_path] + [bag / rel for rel in info["relative_file_paths"]]
    bag_hash = _hash_bag(bag, bag_files)
    evidence_id = "evh_" + hashlib.sha256(
        f"telemetry-health-evidence-v1|{bag_hash}|{topic}".encode("utf-8")
    ).hexdigest()[:12]
    evidence = TelemetryHealthEvidence(
        schema_version="telemetry-health-evidence-v1",
        evidence_id=evidence_id,
        source_bag_path=str(bag.resolve()),
        source_bag_sha256=bag_hash,
        metadata_sha256=_hash_file(metadata_path),
        source_bag_size_bytes=sum(path.stat().st_size for path in bag_files),
        source_bag_duration_sec=info["duration"]["nanoseconds"] / 1e9,
        source_bag_message_count=info["message_count"],
        topic=topic,
        message_type=topic_metadata["type"],
        offered_qos=offered_qos,
        graph_context=graph_context,
        statistics=stats,
        thresholds=thresholds,
        derivation_method={
            "startup_grace": "ceil_50ms(3 * observed startup delay)",
            "stale_timeout": "ceil_50ms(2 * maximum healthy inter-arrival)",
            "minimum_rate": "floor_0.5Hz(0.80 * median healthy rate)",
            "rate_window": "fixed 2s qualification window, 37-message healthy minimum",
            "header_progress": "same bound as arrival freshness",
            "payload_policy": "measured but not enforced; source incident was liveness loss",
        },
        confidence_bounds=confidence,
    )
    return evidence.model_copy(
        update={"evidence_fingerprint": compute_evidence_fingerprint(evidence)}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag", type=Path)
    parser.add_argument("--topic", default="/utlidar/robot_pose")
    parser.add_argument("--graph-context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    evidence = characterize(
        args.bag,
        topic=args.topic,
        graph_context=args.graph_context,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(
                evidence.model_dump(mode="json"),
                fh,
                indent=2,
                sort_keys=True,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, args.output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(args.output)
    print(json.dumps(evidence.thresholds.model_dump(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
