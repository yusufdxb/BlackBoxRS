"""Adversarial rate, header, and clock boundaries for telemetry health."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from blackboxrs.prevention.telemetry_health import (
    TelemetryHealthContract,
    TelemetryHealthState,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _contract() -> TelemetryHealthContract:
    return TelemetryHealthContract(
        topic="/utlidar/robot_pose",
        expected_type="geometry_msgs/msg/PoseStamped",
        expected_qos={
            "history": "keep_last",
            "depth": 1,
            "reliability": "reliable",
            "durability": "volatile",
        },
        graph_context="go2_utlidar_hardware_eval_20260406",
        startup_grace_sec=0.5,
        stale_timeout_sec=0.15,
        minimum_rate_hz=15.0,
        rate_window_sec=2.0,
        header_progress_timeout_sec=0.15,
        require_header_progress=True,
        lifecycle_stages=["startup", "runtime"],
    )


_RATE_TRIAL = r"""
import json
import random
import sys

from blackboxrs.prevention.telemetry_health import (
    TelemetryHealthContract,
    TelemetryHealthState,
)

spec = json.loads(sys.stdin.read())
contract = TelemetryHealthContract.model_validate(spec["contract"])
results = []
for trial in spec["trials"]:
    rng = random.Random(trial["seed"])
    phase = trial["phase"]
    pattern = trial["pattern"]
    duration = trial.get("duration", 4.5)
    arrivals = []
    current = phase
    index = 0
    while current <= duration:
        if pattern == "constant":
            interval = 1.0 / trial["rate_hz"]
        elif pattern == "jitter":
            base = 1.0 / trial["rate_hz"]
            interval = max(0.001, base + rng.uniform(-trial["jitter_sec"], trial["jitter_sec"]))
        elif pattern == "genuine_jitter":
            jitter = [0.007, -0.005, 0.004, -0.006, 0.002, -0.002]
            interval = max(0.001, (1.0 / trial["rate_hz"]) + jitter[(index + trial["seed"]) % len(jitter)])
        elif pattern == "one_delayed":
            interval = 1.0 / trial["rate_hz"]
            if index == trial["delay_index"]:
                interval += trial["delay_sec"]
        elif pattern == "burst_gap":
            interval = 1.0 / 30.0
            if index > 0 and index % 12 == 0:
                interval += 0.20
        elif pattern == "alternating":
            phase_index = int(max(0.0, current - phase) / 0.5)
            interval = 1.0 / (20.0 if phase_index % 2 == 0 else 10.0)
        elif pattern == "short_transient":
            elapsed = current - phase
            rate = 14.0 if 2.4 <= elapsed < 2.65 else 18.75
            interval = 1.0 / rate
        elif pattern == "sustained_below":
            elapsed = current - phase
            rate = 14.0 if elapsed >= 2.35 else 18.75
            interval = 1.0 / rate
        else:
            raise AssertionError(pattern)
        arrivals.append(current)
        current += interval
        index += 1

    state = TelemetryHealthState(contract, started_at=0.0)
    evaluation = state.evaluate(0.0)
    for index, arrival in enumerate(arrivals):
        evaluation = state.evaluate(arrival)
        if evaluation.state == "failed":
            break
        state.observe(received_at=arrival, header_stamp_ns=1_000_000_000 + index * 1_000_000)
        evaluation = state.evaluate(arrival)
        if evaluation.state == "failed":
            break
    results.append(
        {
            "state": evaluation.state,
            "reason": evaluation.reason,
            "observed_rate_hz": evaluation.observed_rate_hz,
        }
    )
print(json.dumps(results, sort_keys=True))
"""


def _process_trials(
    *,
    pattern: str,
    rate_hz: float | None = None,
    case_count: int = 20,
    **parameters: object,
) -> list[dict[str, object]]:
    trials = []
    for case in range(case_count):
        trial: dict[str, object] = {
            "pattern": pattern,
            "seed": case,
            "phase": 0.05 + (case % 10) * 0.007,
        }
        if rate_hz is not None:
            trial["rate_hz"] = rate_hz
        trial.update(parameters)
        trials.append(trial)
    completed = subprocess.run(
        [sys.executable, "-c", _RATE_TRIAL],
        cwd=REPO_ROOT,
        input=json.dumps(
            {
                "contract": _contract().model_dump(mode="json"),
                "trials": trials,
            }
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("rate_hz", "expected_state"),
    [
        (14.9, "failed"),
        (15.0, "healthy"),
        (15.1, "healthy"),
        (16.0, "healthy"),
        (18.75, "healthy"),
    ],
)
def test_hard_rate_boundary_is_phase_stable_in_process(
    rate_hz: float, expected_state: str
) -> None:
    results = _process_trials(pattern="constant", rate_hz=rate_hz)

    assert [result["state"] for result in results] == [expected_state] * 20
    if expected_state == "failed":
        assert {result["reason"] for result in results} == {"below_rate"}


@pytest.mark.parametrize(
    ("rate_hz", "expected_state"),
    [(14.9, "failed"), (15.1, "healthy")],
)
def test_near_boundary_jitter_is_seed_and_phase_stable_in_process(
    rate_hz: float,
    expected_state: str,
) -> None:
    results = _process_trials(
        pattern="jitter",
        rate_hz=rate_hz,
        jitter_sec=0.0002,
    )

    assert [result["state"] for result in results] == [expected_state] * 20


@pytest.mark.parametrize(
    ("pattern", "parameters", "expected_state"),
    [
        ("genuine_jitter", {"rate_hz": 18.75}, "healthy"),
        (
            "one_delayed",
            {"rate_hz": 18.75, "delay_index": 40, "delay_sec": 0.05},
            "healthy",
        ),
        ("burst_gap", {}, "failed"),
        ("alternating", {}, "failed"),
        ("short_transient", {}, "healthy"),
        ("sustained_below", {"duration": 6.5}, "failed"),
    ],
)
def test_rate_shapes_have_stable_selected_outcomes_in_process(
    pattern: str,
    parameters: dict[str, object],
    expected_state: str,
) -> None:
    results = _process_trials(pattern=pattern, **parameters)

    assert [result["state"] for result in results] == [expected_state] * 20


def _observe(
    state: TelemetryHealthState,
    *,
    start: float,
    count: int,
    interval: float,
    stamps: list[int] | None = None,
) -> None:
    for index in range(count):
        state.observe(
            received_at=start + index * interval,
            header_stamp_ns=stamps[index] if stamps is not None else 1_000 + index,
        )


def test_zero_header_timestamps_fail_as_nonprogressing() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe(state, start=0.05, count=40, interval=0.05, stamps=[0] * 40)

    result = state.evaluate(2.0)

    assert result.state == "failed"
    assert result.reason == "frozen_timestamp"


def test_frozen_header_timestamps_fail_while_arrivals_continue() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe(state, start=0.05, count=40, interval=0.05, stamps=[123] * 40)

    assert state.evaluate(2.0).reason == "frozen_timestamp"


def test_advancing_header_with_frozen_pose_is_admitted_by_narrow_contract() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe(state, start=0.05, count=45, interval=0.05)

    result = state.evaluate(2.2)

    assert result.state == "healthy"


def test_changing_pose_with_frozen_header_is_blocked() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    _observe(state, start=0.05, count=45, interval=0.05, stamps=[77] * 45)

    assert state.evaluate(2.2).reason == "frozen_timestamp"


def test_one_reordered_header_recovers_before_progress_timeout() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [1_000 + index for index in range(45)]
    stamps[41], stamps[42] = stamps[40] - 1, stamps[40] + 2
    _observe(state, start=0.05, count=45, interval=0.05, stamps=stamps)

    assert state.evaluate(2.2).state == "healthy"


def test_sustained_backward_header_jump_fails() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [1_000 + index for index in range(40)] + [10] * 6
    _observe(state, start=0.05, count=len(stamps), interval=0.05, stamps=stamps)

    assert state.evaluate(2.3).reason == "frozen_timestamp"


def test_forward_header_jump_preserves_progress() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [1_000 + index for index in range(40)] + [
        10_000_000 + index for index in range(5)
    ]
    _observe(state, start=0.05, count=len(stamps), interval=0.05, stamps=stamps)

    assert state.evaluate(2.2).state == "healthy"


def test_ros_time_pause_is_rejected_as_frozen_header() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [1_000 + index for index in range(40)] + [1_039] * 5
    _observe(state, start=0.05, count=len(stamps), interval=0.05, stamps=stamps)

    assert state.evaluate(2.25).reason == "frozen_timestamp"


def test_ros_time_reset_is_rejected_until_old_high_water_mark_is_exceeded() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [10_000 + index for index in range(40)] + list(range(10))
    _observe(state, start=0.05, count=len(stamps), interval=0.05, stamps=stamps)

    assert state.evaluate(2.5).reason == "frozen_timestamp"


def test_old_replayed_headers_arriving_live_pass_when_monotonic() -> None:
    state = TelemetryHealthState(_contract(), started_at=0.0)
    stamps = [100 + index for index in range(45)]
    _observe(state, start=0.05, count=45, interval=0.05, stamps=stamps)

    assert state.evaluate(2.2).state == "healthy"


def test_wall_clock_jump_is_outside_monotonic_state_inputs() -> None:
    state = TelemetryHealthState(_contract(), started_at=10.0)
    _observe(state, start=10.05, count=45, interval=0.05)

    assert state.evaluate(12.2).state == "healthy"


def test_monotonic_receive_clock_continuity_preserves_health() -> None:
    state = TelemetryHealthState(_contract(), started_at=1_000_000.0)
    _observe(state, start=1_000_000.05, count=45, interval=0.05)

    assert state.evaluate(1_000_002.2).state == "healthy"
