#!/usr/bin/env python3
"""Apply one telemetry-health rule to genuine and bounded synthetic traces."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import rosbag2_py
from geometry_msgs.msg import PoseStamped
from rclpy.serialization import deserialize_message

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.prevention.rules import load_rule  # noqa: E402
from blackboxrs.prevention.telemetry_health import (  # noqa: E402
    TelemetryHealthState,
    contract_from_rule,
)


def _read_pose_trace(bag: Path, topic: str) -> tuple[int, list[tuple[int, int]]]:
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id="sqlite3"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    bag_start: int | None = None
    trace: list[tuple[int, int]] = []
    while reader.has_next():
        observed_topic, data, arrival_ns = reader.read_next()
        if bag_start is None:
            bag_start = arrival_ns
        if observed_topic != topic:
            continue
        msg = deserialize_message(data, PoseStamped)
        header_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        trace.append((arrival_ns, header_ns))
    if bag_start is None or not trace:
        raise ValueError("No PoseStamped trace found")
    return bag_start, trace


def _feed_rate(
    state: TelemetryHealthState,
    *,
    rate_hz: float,
    duration_sec: float,
    jitter: list[float] | None = None,
) -> float:
    now = 0.1
    sent = 0
    while now <= duration_sec:
        state.observe(
            received_at=now,
            header_stamp_ns=1_000_000_000 + int(now * 1e9),
        )
        offset = jitter[sent % len(jitter)] if jitter else 0.0
        now += max(0.001, (1.0 / rate_hz) + offset)
        sent += 1
    return state.last_received_at or 0.0


def validate(
    bag: Path,
    rule_path: Path,
    *,
    trusted_rule_fingerprint: str,
) -> dict:
    rule = load_rule(rule_path)
    contract = contract_from_rule(
        rule,
        trusted_rule_fingerprint=trusted_rule_fingerprint,
    )
    bag_start_ns, trace = _read_pose_trace(bag, contract.topic)

    healthy = TelemetryHealthState(contract, started_at=0.0)
    healthy_failures = []
    healthy_states: dict[str, int] = {"starting": 0, "healthy": 0, "failed": 0}
    for arrival_ns, header_ns in trace:
        now = (arrival_ns - bag_start_ns) / 1e9
        healthy.observe(received_at=now, header_stamp_ns=header_ns)
        evaluation = healthy.evaluate(now)
        healthy_states[evaluation.state] += 1
        if evaluation.state == "failed":
            healthy_failures.append(
                {
                    "at_sec": now,
                    "reason": evaluation.reason,
                    "detail": evaluation.detail,
                }
            )
            break

    dropout_cutoff_sec = 5.0
    dropout = TelemetryHealthState(contract, started_at=0.0)
    retained = 0
    for arrival_ns, header_ns in trace:
        now = (arrival_ns - bag_start_ns) / 1e9
        if now > dropout_cutoff_sec:
            continue
        dropout.observe(received_at=now, header_stamp_ns=header_ns)
        dropout.evaluate(now)
        retained += 1
    assert dropout.last_received_at is not None
    dropout_eval = dropout.evaluate(
        dropout.last_received_at + contract.stale_timeout_sec + 0.001
    )

    jitter_state = TelemetryHealthState(contract, started_at=0.0)
    jitter_last = _feed_rate(
        jitter_state,
        rate_hz=18.75,
        duration_sec=3.2,
        jitter=[0.007, -0.005, 0.004, -0.006, 0.002, -0.002],
    )
    jitter_eval = jitter_state.evaluate(jitter_last)

    stale_state = TelemetryHealthState(contract, started_at=0.0)
    stale_last = _feed_rate(stale_state, rate_hz=18.75, duration_sec=3.0)
    stale_eval = stale_state.evaluate(
        stale_last + contract.stale_timeout_sec + 0.001
    )

    slow_state = TelemetryHealthState(contract, started_at=0.0)
    slow_last = _feed_rate(slow_state, rate_hz=10.0, duration_sec=2.2)
    slow_eval = slow_state.evaluate(slow_last)

    return {
        "schema_version": "telemetry-threshold-validation-v1",
        "rule_id": rule.rule_id,
        "rule_fingerprint": rule.rule_fingerprint,
        "source_bag": str(bag.resolve()),
        "healthy_genuine_bag": {
            "message_count": len(trace),
            "state_counts": healthy_states,
            "failures": healthy_failures,
            "final_state": healthy.evaluate(
                (trace[-1][0] - bag_start_ns) / 1e9
            ).state,
            "maximum_observed_gap_sec": healthy.maximum_observed_gap_sec,
        },
        "injected_dropout": {
            "drop_after_sec": dropout_cutoff_sec,
            "retained_pose_messages": retained,
            "detected": dropout_eval.state == "failed",
            "reason": dropout_eval.reason,
            "detection_latency_from_last_message_sec": (
                dropout_eval.evaluated_at - dropout.last_received_at
            ),
        },
        "synthetic_healthy_jitter": {
            "state": jitter_eval.state,
            "reason": jitter_eval.reason,
            "maximum_observed_gap_sec": jitter_state.maximum_observed_gap_sec,
        },
        "synthetic_stale": {
            "state": stale_eval.state,
            "reason": stale_eval.reason,
            "detection_latency_from_last_message_sec": (
                stale_eval.evaluated_at - stale_last
            ),
        },
        "synthetic_low_rate": {
            "state": slow_eval.state,
            "reason": slow_eval.reason,
            "observed_rate_hz": slow_eval.observed_rate_hz,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag", type=Path)
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--trusted-rule-fingerprint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(
        args.bag,
        args.rule,
        trusted_rule_fingerprint=args.trusted_rule_fingerprint,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
