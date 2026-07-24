"""ROS 2 runtime enforcement for one telemetry-health prevention rule."""

from __future__ import annotations

import json
import ctypes
import ctypes.util
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .rules import PreventionRule
from .telemetry_health import (
    TelemetryHealthContract,
    TelemetryHealthEvaluation,
    TelemetryHealthState,
    contract_from_rule,
)


class TelemetryGuardResult(BaseModel):
    """Structured outcome from a startup and sustained runtime guard."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_fingerprint: str
    source_incident_id: str
    source_fingerprint_id: str
    source_trigger_ids: list[str]
    topic: str
    expected_type: str
    graph_context: str
    resolved_topic: str
    publisher_semantics: Literal["aggregate_topic"]
    status: Literal["passed", "blocked", "dependent_failed"]
    reason: str | None = None
    detail: str = ""
    observed_messages: int = Field(..., ge=0)
    observed_rate_hz: float | None = None
    startup_delay_sec: float | None = None
    maximum_observed_gap_sec: float = Field(..., ge=0.0)
    dependent_started: bool
    dependent_pid: int | None = None
    dependent_exit_code: int | None = None
    detection_latency_sec: float | None = None
    enforcement_latency_sec: float | None = None
    guard_runtime_sec: float = Field(..., ge=0.0)
    dependent_launch_offset_sec: float | None = None
    dependent_supervision_sec: float | None = None
    structural: dict[str, Any] = Field(default_factory=dict)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    completed_at: datetime


def run_ros_telemetry_guard(
    rule: PreventionRule,
    command: Sequence[str],
    *,
    monitor_duration_sec: float | None = None,
    result_path: Path | None = None,
    spin_slice_sec: float = 0.01,
    runtime_context: str | None = None,
    trusted_rule_fingerprint: str | None = None,
) -> TelemetryGuardResult:
    """Hold a dependent command until healthy, then terminate it on failure.

    The implementation is intentionally limited to PoseStamped because the
    evidence-backed GO2 contract in this experiment is PoseStamped-specific.
    """
    contract = contract_from_rule(rule, trusted_rule_fingerprint=trusted_rule_fingerprint)
    if runtime_context is None or runtime_context != contract.graph_context:
        raise ValueError("Runtime context does not match the telemetry contract")
    if contract.expected_type != "geometry_msgs/msg/PoseStamped":
        raise ValueError(
            "Runtime guard currently supports only geometry_msgs/msg/PoseStamped"
        )
    if not command:
        raise ValueError("A dependent command is required")
    if monitor_duration_sec is not None and monitor_duration_sec <= 0:
        raise ValueError("monitor_duration_sec must be positive")

    try:
        import rclpy
        from geometry_msgs.msg import PoseStamped
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSCompatibility,
            QoSProfile,
            ReliabilityPolicy,
            qos_check_compatible,
        )
    except ImportError as exc:
        raise RuntimeError("ROS 2 rclpy and geometry_msgs are required") from exc

    qos = _subscription_qos(
        contract,
        QoSProfile=QoSProfile,
        HistoryPolicy=HistoryPolicy,
        ReliabilityPolicy=ReliabilityPolicy,
        DurabilityPolicy=DurabilityPolicy,
    )
    guard_started = time.monotonic()
    state = TelemetryHealthState(contract, started_at=guard_started)
    transitions: list[dict[str, Any]] = []
    message_count = 0
    last_state: str | None = None
    child: subprocess.Popen[bytes] | None = None
    structural: dict[str, Any] = {
        "topic_seen": False,
        "publisher_count": 0,
        "compatible_publisher_count": 0,
        "observed_types": [],
        "expected_qos": contract.expected_qos,
    }

    init_here = not rclpy.ok()
    if init_here:
        rclpy.init(args=[])
    node = rclpy.create_node("blackbox_telemetry_guard", namespace="/blackbox", use_global_arguments=False)

    def on_message(msg: Any) -> None:
        nonlocal message_count
        now = time.monotonic()
        header_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        state.observe(received_at=now, header_stamp_ns=header_ns)
        message_count += 1

    subscription = node.create_subscription(PoseStamped, contract.topic, on_message, qos)
    resolved_topic = str(subscription.topic_name)
    structural["resolved_topic"] = resolved_topic
    if resolved_topic != contract.topic:
        raise RuntimeError(f"Resolved subscription topic {resolved_topic!r} differs from contract")
    child_started_at: float | None = None
    try:
        while True:
            rclpy.spin_once(node, timeout_sec=spin_slice_sec)
            now = time.monotonic()
            structural = _inspect_graph(
                node,
                contract,
                qos,
                qos_check_compatible=qos_check_compatible,
                QoSCompatibility=QoSCompatibility,
            )
            evaluation = state.evaluate(now)
            if evaluation.state == "healthy" and not _graph_consistent(structural, contract, resolved_topic):
                evaluation = state.fail(now, "graph_inconsistent", "resolved topic or compatible graph does not match contract")
            if evaluation.state != last_state:
                transitions.append(
                    {
                        "state": evaluation.state,
                        "at_sec": now - guard_started,
                        "reason": evaluation.reason,
                        "detail": evaluation.detail,
                    }
                )
                last_state = evaluation.state

            if evaluation.state == "failed":
                reason, detail = _specific_failure(evaluation, structural)
                detection_latency = _detection_latency(state, evaluation)
                enforcement_started = time.monotonic()
                exit_code = None
                if child is not None:
                    exit_code = _terminate_process_group(child)
                enforcement_latency = time.monotonic() - enforcement_started
                result = _result(
                    rule,
                    contract,
                    state,
                    status="blocked",
                    reason=reason,
                    detail=detail,
                    messages=message_count,
                    evaluation=evaluation,
                    child=child,
                    child_exit_code=exit_code,
                    detection_latency=detection_latency,
                    enforcement_latency=enforcement_latency,
                    started_at=guard_started,
                    structural=structural,
                    transitions=transitions,
                    child_started_at=child_started_at,
                )
                return _persist(result, result_path)

            if evaluation.state == "healthy" and child is None:
                supervisor = [__import__("sys").executable, "-m", "blackboxrs.prevention.process_supervisor", "--", *command]
                parent_pid = os.getpid()
                child_env = os.environ.copy()
                child_env["BLACKBOXRS_OWNER_PID"] = str(parent_pid)
                child = subprocess.Popen(supervisor, start_new_session=True, preexec_fn=lambda: _set_parent_death_signal(parent_pid), env=child_env)
                child_started_at = time.monotonic()
                transitions.append(
                    {
                        "state": "dependent_started",
                        "at_sec": time.monotonic() - guard_started,
                        "pid": child.pid,
                    }
                )

            if child is None:
                continue
            child_code = child.poll()
            if child_code is not None:
                result = _result(
                    rule,
                    contract,
                    state,
                    status="passed" if child_code == 0 else "dependent_failed",
                    reason=None if child_code == 0 else "dependent_exit",
                    detail=f"dependent exited with code {child_code}",
                    messages=message_count,
                    evaluation=evaluation,
                    child=child,
                    child_exit_code=child_code,
                    detection_latency=None,
                    enforcement_latency=None,
                    started_at=guard_started,
                    structural=structural,
                    transitions=transitions,
                    child_started_at=child_started_at,
                )
                return _persist(result, result_path)

            if (
                monitor_duration_sec is not None
                and child_started_at is not None
                and now - child_started_at >= monitor_duration_sec
            ):
                exit_code = _terminate_process_group(child)
                result = _result(
                    rule,
                    contract,
                    state,
                    status="passed",
                    reason=None,
                    detail="monitor duration completed with healthy telemetry",
                    messages=message_count,
                    evaluation=evaluation,
                    child=child,
                    child_exit_code=exit_code,
                    detection_latency=None,
                    enforcement_latency=None,
                    started_at=guard_started,
                    structural=structural,
                    transitions=transitions,
                    child_started_at=child_started_at,
                )
                return _persist(result, result_path)
    finally:
        if child is not None and child.poll() is None:
            _terminate_process_group(child)
        node.destroy_subscription(subscription)
        node.destroy_node()
        if init_here and rclpy.ok():
            rclpy.shutdown()


def _subscription_qos(
    contract: TelemetryHealthContract,
    *,
    QoSProfile: Any,
    HistoryPolicy: Any,
    ReliabilityPolicy: Any,
    DurabilityPolicy: Any,
) -> Any:
    expected = contract.expected_qos
    if expected != {
        "history": "keep_last",
        "depth": 1,
        "reliability": "reliable",
        "durability": "volatile",
    }:
        raise ValueError(f"Unsupported evidence QoS profile: {expected!r}")
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


def _inspect_graph(
    node: Any,
    contract: TelemetryHealthContract,
    subscription_qos: Any,
    *,
    qos_check_compatible: Any,
    QoSCompatibility: Any,
) -> dict[str, Any]:
    types_by_topic = dict(node.get_topic_names_and_types())
    observed_types = list(types_by_topic.get(contract.topic, []))
    endpoints = list(node.get_publishers_info_by_topic(contract.topic))
    compatible = 0
    for endpoint in endpoints:
        if endpoint.topic_type != contract.expected_type:
            continue
        compatibility, _reason = qos_check_compatible(
            endpoint.qos_profile, subscription_qos
        )
        if compatibility != QoSCompatibility.ERROR:
            compatible += 1
    return {
        "topic_seen": contract.topic in types_by_topic,
        "publisher_count": len(endpoints),
        "compatible_publisher_count": compatible,
        "observed_types": observed_types,
        "expected_qos": contract.expected_qos,
        "resolved_topic": contract.topic,
    }


def _graph_consistent(structural: dict[str, Any], contract: TelemetryHealthContract, resolved_topic: str) -> bool:
    return bool(
        resolved_topic == contract.topic
        and structural.get("topic_seen")
        and structural.get("compatible_publisher_count", 0) >= 1
        and contract.expected_type in structural.get("observed_types", [])
    )


def _set_parent_death_signal(parent_pid: int) -> None:
    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        raise RuntimeError("libc unavailable for parent-death supervision")
    libc = ctypes.CDLL(libc_path, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def _specific_failure(
    evaluation: TelemetryHealthEvaluation,
    structural: dict[str, Any],
) -> tuple[str, str]:
    if evaluation.reason == "graph_inconsistent":
        return "graph_inconsistent", evaluation.detail
    if evaluation.reason != "startup_timeout":
        return str(evaluation.reason), evaluation.detail
    observed_types = structural["observed_types"]
    if observed_types and structural["compatible_publisher_count"] == 0:
        if structural["publisher_count"] > 0:
            return (
                "wrong_type_or_incompatible_qos",
                f"publishers are present but none match expected type/QoS; "
                f"observed types={observed_types}",
            )
    if structural["publisher_count"] == 0:
        return "no_publisher", evaluation.detail
    return "startup_timeout", evaluation.detail


def _detection_latency(
    state: TelemetryHealthState,
    evaluation: TelemetryHealthEvaluation,
) -> float | None:
    if evaluation.reason == "startup_timeout":
        return evaluation.evaluated_at - state.started_at
    if evaluation.reason == "stale" and state.last_received_at is not None:
        return evaluation.evaluated_at - state.last_received_at
    if (
        evaluation.reason == "frozen_timestamp"
        and state.last_header_progress_at is not None
    ):
        return evaluation.evaluated_at - state.last_header_progress_at
    if evaluation.reason == "below_rate" and state.first_received_at is not None:
        return evaluation.evaluated_at - state.first_received_at
    return None


def _terminate_process_group(child: subprocess.Popen[bytes]) -> int:
    if child.poll() is not None:
        return int(child.returncode)
    try:
        os.killpg(child.pid, signal.SIGTERM)
        return int(child.wait(timeout=1.0))
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        return int(child.wait(timeout=1.0))
    except ProcessLookupError:
        return int(child.wait(timeout=1.0))


def _result(
    rule: PreventionRule,
    contract: TelemetryHealthContract,
    state: TelemetryHealthState,
    *,
    status: Literal["passed", "blocked", "dependent_failed"],
    reason: str | None,
    detail: str,
    messages: int,
    evaluation: TelemetryHealthEvaluation,
    child: subprocess.Popen[bytes] | None,
    child_exit_code: int | None,
    detection_latency: float | None,
    enforcement_latency: float | None,
    started_at: float,
    structural: dict[str, Any],
    transitions: list[dict[str, Any]],
    child_started_at: float | None,
) -> TelemetryGuardResult:
    assert rule.rule_fingerprint is not None
    assert rule.source_incident_id is not None
    assert rule.source_fingerprint_id is not None
    return TelemetryGuardResult(
        rule_id=rule.rule_id,
        rule_fingerprint=rule.rule_fingerprint,
        source_incident_id=rule.source_incident_id,
        source_fingerprint_id=rule.source_fingerprint_id,
        source_trigger_ids=rule.source_trigger_ids,
        topic=contract.topic,
        expected_type=contract.expected_type,
        graph_context=contract.graph_context,
        resolved_topic=str(structural.get("resolved_topic", contract.topic)),
        publisher_semantics=contract.publisher_semantics,
        status=status,
        reason=reason,
        detail=detail,
        observed_messages=messages,
        observed_rate_hz=evaluation.observed_rate_hz,
        startup_delay_sec=(
            state.first_received_at - state.started_at
            if state.first_received_at is not None
            else None
        ),
        maximum_observed_gap_sec=state.maximum_observed_gap_sec,
        dependent_started=child is not None,
        dependent_pid=child.pid if child is not None else None,
        dependent_exit_code=child_exit_code,
        detection_latency_sec=detection_latency,
        enforcement_latency_sec=enforcement_latency,
        guard_runtime_sec=time.monotonic() - started_at,
        dependent_launch_offset_sec=(child_started_at - started_at if child_started_at else None),
        dependent_supervision_sec=(time.monotonic() - child_started_at if child_started_at else None),
        structural=structural,
        transitions=transitions,
        completed_at=datetime.now(timezone.utc),
    )


def _persist(
    result: TelemetryGuardResult, result_path: Path | None
) -> TelemetryGuardResult:
    if result_path is not None:
        path = Path(result_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result.model_dump(mode="json"), fh, indent=2, sort_keys=True)
            fh.write("\n")
    return result
