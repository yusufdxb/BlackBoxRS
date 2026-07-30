"""ROS 2 runtime enforcement for one telemetry-health prevention rule."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
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

    schema_version: Literal["telemetry-guard-result-v2"] = (
        "telemetry-guard-result-v2"
    )
    run_id: str
    invocation_started_at: datetime
    declared_context_label: str
    exit_code: int
    rule_id: str
    rule_fingerprint: str
    source_incident_id: str
    source_fingerprint_id: str
    source_trigger_ids: list[str]
    topic: str
    expected_type: str
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
    guard_started_at: datetime
    qualification_started_at: datetime
    qualification_completed_at: datetime | None = None
    dependent_launched_at: datetime | None = None
    supervision_started_at: datetime | None = None
    supervision_ended_at: datetime | None = None
    dependent_exited_at: datetime | None = None
    enforcement_at: datetime | None = None
    structural: dict[str, Any] = Field(default_factory=dict)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    completed_at: datetime


class TelemetryGuardLifecycleResult(BaseModel):
    """Current-invocation state before a completed guard result exists."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["telemetry-guard-result-v2"] = (
        "telemetry-guard-result-v2"
    )
    run_id: str
    status: Literal["starting", "refused", "failed"]
    requested_topic: str
    declared_context_label: str
    started_at: datetime
    reason: str | None = None
    error_category: str | None = None
    exit_code: int | None = None
    completed_at: datetime | None = None


GuardResultDocument = TelemetryGuardLifecycleResult | TelemetryGuardResult


@dataclass
class GuardInvocation:
    """Stable identity and output destination for one CLI or API invocation."""

    run_id: str
    requested_topic: str
    declared_context_label: str
    started_at: datetime
    result_path: Path | None
    terminal_written: bool = False


def begin_guard_invocation(
    result_path: Path | None,
    *,
    requested_topic: str,
    declared_context_label: str,
    run_id: str | None = None,
) -> GuardInvocation:
    """Create a run ID and replace any old result with this invocation."""
    invocation = GuardInvocation(
        run_id=run_id or str(uuid.uuid4()),
        requested_topic=requested_topic,
        declared_context_label=declared_context_label,
        started_at=datetime.now(timezone.utc),
        result_path=Path(result_path) if result_path is not None else None,
    )
    _persist_document(
        TelemetryGuardLifecycleResult(
            run_id=invocation.run_id,
            status="starting",
            requested_topic=invocation.requested_topic,
            declared_context_label=invocation.declared_context_label,
            started_at=invocation.started_at,
        ),
        invocation.result_path,
        invocation.run_id,
    )
    return invocation


def refuse_guard_invocation(
    invocation: GuardInvocation, *, reason: str, exit_code: int = 1
) -> None:
    """Atomically record a prelaunch refusal for the current run."""
    _persist_document(
        TelemetryGuardLifecycleResult(
            run_id=invocation.run_id,
            status="refused",
            requested_topic=invocation.requested_topic,
            declared_context_label=invocation.declared_context_label,
            started_at=invocation.started_at,
            reason=_bounded_text(reason),
            exit_code=exit_code,
            completed_at=datetime.now(timezone.utc),
        ),
        invocation.result_path,
        invocation.run_id,
    )
    invocation.terminal_written = True


def fail_guard_invocation(
    invocation: GuardInvocation,
    *,
    error_category: str,
    exit_code: int = 1,
) -> None:
    """Atomically record an unexpected current-run failure."""
    _persist_document(
        TelemetryGuardLifecycleResult(
            run_id=invocation.run_id,
            status="failed",
            requested_topic=invocation.requested_topic,
            declared_context_label=invocation.declared_context_label,
            started_at=invocation.started_at,
            error_category=_bounded_text(error_category, limit=100),
            exit_code=exit_code,
            completed_at=datetime.now(timezone.utc),
        ),
        invocation.result_path,
        invocation.run_id,
    )
    invocation.terminal_written = True


def load_guard_result(
    path: Path, *, expected_run_id: str | None = None
) -> GuardResultDocument:
    """Load an atomic result and optionally require a caller-known run ID."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    model: type[TelemetryGuardLifecycleResult] | type[TelemetryGuardResult]
    if raw.get("status") in {"starting", "refused", "failed"}:
        model = TelemetryGuardLifecycleResult
    else:
        model = TelemetryGuardResult
    result = model.model_validate(raw)
    if expected_run_id is not None and result.run_id != expected_run_id:
        raise ValueError("Telemetry guard result run ID mismatch")
    return result


@dataclass
class _GuardTiming:
    """Paired monotonic and UTC event times for one guard run."""

    guard_started_mono: float
    guard_started_at: datetime
    qualification_completed_mono: float | None = None
    qualification_completed_at: datetime | None = None
    dependent_launched_mono: float | None = None
    dependent_launched_at: datetime | None = None
    supervision_started_mono: float | None = None
    supervision_started_at: datetime | None = None
    supervision_ended_mono: float | None = None
    supervision_ended_at: datetime | None = None
    dependent_exited_mono: float | None = None
    dependent_exited_at: datetime | None = None
    enforcement_mono: float | None = None
    enforcement_at: datetime | None = None
    completed_mono: float | None = None
    completed_at: datetime | None = None

    @classmethod
    def start(cls) -> "_GuardTiming":
        return cls(
            guard_started_mono=time.monotonic(),
            guard_started_at=datetime.now(timezone.utc),
        )

    def mark_qualification_completed(self, mono: float) -> None:
        if self.qualification_completed_mono is None:
            self.qualification_completed_mono = mono
            self.qualification_completed_at = datetime.now(timezone.utc)

    def mark_dependent_launched(self) -> None:
        mono = time.monotonic()
        wall = datetime.now(timezone.utc)
        self.dependent_launched_mono = mono
        self.dependent_launched_at = wall
        self.supervision_started_mono = mono
        self.supervision_started_at = wall

    def mark_enforcement(self, mono: float | None = None) -> None:
        if self.enforcement_mono is None:
            self.enforcement_mono = mono if mono is not None else time.monotonic()
            self.enforcement_at = datetime.now(timezone.utc)

    def mark_dependent_exit(self) -> None:
        mono = time.monotonic()
        wall = datetime.now(timezone.utc)
        self.dependent_exited_mono = mono
        self.dependent_exited_at = wall
        self.supervision_ended_mono = mono
        self.supervision_ended_at = wall

    def finish(self) -> None:
        self.completed_mono = time.monotonic()
        self.completed_at = datetime.now(timezone.utc)
        if (
            self.supervision_started_mono is not None
            and self.supervision_ended_mono is None
        ):
            self.supervision_ended_mono = self.completed_mono
            self.supervision_ended_at = self.completed_at


def run_ros_telemetry_guard(
    rule: PreventionRule,
    command: Sequence[str],
    *,
    monitor_duration_sec: float | None = None,
    result_path: Path | None = None,
    spin_slice_sec: float = 0.01,
    declared_context_label: str | None = None,
    trusted_rule_fingerprint: str | None = None,
    invocation: GuardInvocation | None = None,
) -> TelemetryGuardResult:
    """Run one guarded command with current-invocation result lifecycle."""
    active_invocation = invocation or begin_guard_invocation(
        result_path,
        requested_topic=str(rule.check.params.get("topic", "<unavailable>")),
        declared_context_label=declared_context_label or "",
    )
    try:
        return _run_ros_telemetry_guard(
            rule,
            command,
            monitor_duration_sec=monitor_duration_sec,
            spin_slice_sec=spin_slice_sec,
            declared_context_label=declared_context_label,
            trusted_rule_fingerprint=trusted_rule_fingerprint,
            invocation=active_invocation,
        )
    except ValueError as exc:
        if not active_invocation.terminal_written:
            refuse_guard_invocation(active_invocation, reason=str(exc))
        raise
    except Exception as exc:
        if not active_invocation.terminal_written:
            fail_guard_invocation(
                active_invocation,
                error_category=_error_category(exc),
            )
        raise


def _run_ros_telemetry_guard(
    rule: PreventionRule,
    command: Sequence[str],
    *,
    monitor_duration_sec: float | None,
    spin_slice_sec: float,
    declared_context_label: str | None,
    trusted_rule_fingerprint: str | None,
    invocation: GuardInvocation,
) -> TelemetryGuardResult:
    """Hold a dependent command until healthy, then terminate it on failure.

    The implementation is intentionally limited to PoseStamped because the
    evidence-backed GO2 contract in this experiment is PoseStamped-specific.
    """
    contract = contract_from_rule(rule, trusted_rule_fingerprint=trusted_rule_fingerprint)
    if (
        declared_context_label is None
        or declared_context_label != contract.declared_context_label
    ):
        raise ValueError(
            "Declared context label does not match the telemetry contract"
        )
    if contract.expected_type != "geometry_msgs/msg/PoseStamped":
        raise ValueError(
            "Runtime guard currently supports only geometry_msgs/msg/PoseStamped"
        )
    if not command:
        raise ValueError("A dependent command is required")
    if monitor_duration_sec is not None and monitor_duration_sec < 0:
        raise ValueError("monitor_duration_sec must be non-negative")

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
    timing = _GuardTiming.start()
    guard_started = timing.guard_started_mono
    state = TelemetryHealthState(contract, started_at=guard_started)
    transitions: list[dict[str, Any]] = []
    message_count = 0
    last_state: str | None = None
    child: subprocess.Popen[bytes] | None = None
    child_ready_fd: int | None = None
    dependent_pid: int | None = None
    structural: dict[str, Any] = {
        "topic_seen": False,
        "publisher_count": 0,
        "compatible_publisher_count": 0,
        "observed_types": [],
        "expected_qos": contract.expected_qos,
    }

    child_started_at: float | None = None

    init_here = not rclpy.ok()
    if init_here:
        rclpy.init(args=[])
    node = rclpy.create_node("blackbox_telemetry_guard", namespace="/blackbox", use_global_arguments=False)

    def on_message(msg: Any) -> None:
        nonlocal message_count
        now = time.monotonic()
        if (
            child_started_at is not None
            and monitor_duration_sec is not None
            and now > child_started_at + monitor_duration_sec
        ):
            return
        header_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        state.observe(received_at=now, header_stamp_ns=header_ns)
        message_count += 1

    subscription = node.create_subscription(PoseStamped, contract.topic, on_message, qos)
    resolved_topic = str(subscription.topic_name)
    structural["resolved_topic"] = resolved_topic
    if resolved_topic != contract.topic:
        raise RuntimeError(f"Resolved subscription topic {resolved_topic!r} differs from contract")
    try:
        while True:
            rclpy.spin_once(node, timeout_sec=spin_slice_sec)
            now = time.monotonic()

            if child is not None and child_ready_fd is not None:
                ready_pid, ready_closed = _read_dependent_pid(child_ready_fd)
                if ready_closed:
                    os.close(child_ready_fd)
                    child_ready_fd = None
                if ready_pid is not None:
                    dependent_pid = ready_pid
                    timing.mark_dependent_launched()
                    child_started_at = timing.dependent_launched_mono
                    assert child_started_at is not None
                    transitions.append(
                        {
                            "state": "dependent_started",
                            "at_sec": child_started_at - guard_started,
                            "pid": dependent_pid,
                        }
                    )

            if child is not None:
                child_code = child.poll()
                if child_code is not None:
                    timing.mark_dependent_exit()
                    timing.finish()
                    evaluation = state.evaluate(
                        min(now, timing.supervision_ended_mono or now)
                    )
                    result = _result(
                        rule,
                        contract,
                        state,
                        status="passed" if child_code == 0 else "dependent_failed",
                        reason=None if child_code == 0 else "dependent_exit",
                        detail=f"dependent exited with code {child_code}",
                        messages=message_count,
                        evaluation=evaluation,
                        dependent_pid=dependent_pid,
                        child_exit_code=child_code,
                        detection_latency=None,
                        enforcement_latency=None,
                        structural=structural,
                        transitions=transitions,
                        timing=timing,
                        invocation=invocation,
                    )
                    return _persist(result, invocation)

            supervision_deadline = (
                child_started_at + monitor_duration_sec
                if child_started_at is not None
                and monitor_duration_sec is not None
                else None
            )
            deadline_reached = (
                supervision_deadline is not None and now >= supervision_deadline
            )
            evaluation_at = (
                supervision_deadline if deadline_reached else now
            )
            if not deadline_reached:
                structural = _inspect_graph(
                    node,
                    contract,
                    qos,
                    qos_check_compatible=qos_check_compatible,
                    QoSCompatibility=QoSCompatibility,
                )
            evaluation = state.evaluate(evaluation_at)
            if evaluation.state == "healthy" and not _graph_consistent(structural, contract, resolved_topic):
                evaluation = state.fail(
                    evaluation_at,
                    "graph_inconsistent",
                    "resolved topic or compatible graph does not match contract",
                )
            if evaluation.state == "healthy":
                timing.mark_qualification_completed(evaluation_at)
            if evaluation.state != last_state:
                transitions.append(
                    {
                        "state": evaluation.state,
                        "at_sec": evaluation_at - guard_started,
                        "reason": evaluation.reason,
                        "detail": evaluation.detail,
                    }
                )
                last_state = evaluation.state

            if evaluation.state == "failed":
                reason, detail = _specific_failure(evaluation, structural)
                detection_latency = _detection_latency(state, evaluation)
                enforcement_started = time.monotonic()
                timing.mark_enforcement(enforcement_started)
                exit_code = None
                if child is not None:
                    exit_code = _terminate_process_group(child)
                    timing.mark_dependent_exit()
                enforcement_latency = time.monotonic() - enforcement_started
                timing.finish()
                result = _result(
                    rule,
                    contract,
                    state,
                    status="blocked",
                    reason=reason,
                    detail=detail,
                    messages=message_count,
                    evaluation=evaluation,
                    dependent_pid=dependent_pid,
                    child_exit_code=exit_code,
                    detection_latency=detection_latency,
                    enforcement_latency=enforcement_latency,
                    structural=structural,
                    transitions=transitions,
                    timing=timing,
                    invocation=invocation,
                )
                return _persist(result, invocation)

            if deadline_reached:
                assert child is not None
                enforcement_started = time.monotonic()
                timing.mark_enforcement(enforcement_started)
                exit_code = _terminate_process_group(child)
                timing.mark_dependent_exit()
                enforcement_latency = time.monotonic() - enforcement_started
                timing.finish()
                result = _result(
                    rule,
                    contract,
                    state,
                    status="passed",
                    reason=None,
                    detail="monitor duration completed with healthy telemetry",
                    messages=message_count,
                    evaluation=evaluation,
                    dependent_pid=dependent_pid,
                    child_exit_code=exit_code,
                    detection_latency=None,
                    enforcement_latency=enforcement_latency,
                    structural=structural,
                    transitions=transitions,
                    timing=timing,
                    invocation=invocation,
                )
                return _persist(result, invocation)

            if evaluation.state == "healthy" and child is None:
                supervisor = [__import__("sys").executable, "-m", "blackboxrs.prevention.process_supervisor", "--", *command]
                parent_pid = os.getpid()
                child_env = os.environ.copy()
                child_env["BLACKBOXRS_OWNER_PID"] = str(parent_pid)
                child_ready_fd, ready_write_fd = os.pipe()
                os.set_blocking(child_ready_fd, False)
                child_env["BLACKBOXRS_READY_FD"] = str(ready_write_fd)
                try:
                    child = subprocess.Popen(
                        supervisor,
                        start_new_session=True,
                        env=child_env,
                        pass_fds=(ready_write_fd,),
                    )
                except BaseException:
                    os.close(child_ready_fd)
                    child_ready_fd = None
                    raise
                finally:
                    os.close(ready_write_fd)

            if child is None:
                continue
    finally:
        if child_ready_fd is not None:
            os.close(child_ready_fd)
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


def _read_dependent_pid(ready_fd: int) -> tuple[int | None, bool]:
    """Read one atomic dependent-launch acknowledgement from the supervisor."""
    try:
        payload = os.read(ready_fd, 64)
    except BlockingIOError:
        return None, False
    if not payload:
        return None, True
    try:
        return int(payload.strip()), True
    except ValueError as exc:
        raise RuntimeError("Process supervisor returned an invalid dependent PID") from exc


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
    dependent_pid: int | None,
    child_exit_code: int | None,
    detection_latency: float | None,
    enforcement_latency: float | None,
    structural: dict[str, Any],
    transitions: list[dict[str, Any]],
    timing: _GuardTiming,
    invocation: GuardInvocation,
) -> TelemetryGuardResult:
    assert rule.rule_fingerprint is not None
    assert rule.source_incident_id is not None
    assert rule.source_fingerprint_id is not None
    assert timing.completed_mono is not None
    assert timing.completed_at is not None
    return TelemetryGuardResult(
        run_id=invocation.run_id,
        invocation_started_at=invocation.started_at,
        declared_context_label=contract.declared_context_label,
        exit_code=0 if status == "passed" else 1,
        rule_id=rule.rule_id,
        rule_fingerprint=rule.rule_fingerprint,
        source_incident_id=rule.source_incident_id,
        source_fingerprint_id=rule.source_fingerprint_id,
        source_trigger_ids=rule.source_trigger_ids,
        topic=contract.topic,
        expected_type=contract.expected_type,
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
        dependent_started=timing.dependent_launched_mono is not None,
        dependent_pid=dependent_pid,
        dependent_exit_code=child_exit_code,
        detection_latency_sec=detection_latency,
        enforcement_latency_sec=enforcement_latency,
        guard_runtime_sec=timing.completed_mono - timing.guard_started_mono,
        dependent_launch_offset_sec=(
            timing.dependent_launched_mono - timing.guard_started_mono
            if timing.dependent_launched_mono is not None
            else None
        ),
        dependent_supervision_sec=(
            timing.supervision_ended_mono - timing.supervision_started_mono
            if timing.supervision_started_mono is not None
            and timing.supervision_ended_mono is not None
            else None
        ),
        guard_started_at=timing.guard_started_at,
        qualification_started_at=timing.guard_started_at,
        qualification_completed_at=timing.qualification_completed_at,
        dependent_launched_at=timing.dependent_launched_at,
        supervision_started_at=timing.supervision_started_at,
        supervision_ended_at=timing.supervision_ended_at,
        dependent_exited_at=timing.dependent_exited_at,
        enforcement_at=timing.enforcement_at,
        structural=structural,
        transitions=transitions,
        completed_at=timing.completed_at,
    )


def _persist(
    result: TelemetryGuardResult,
    invocation: GuardInvocation,
) -> TelemetryGuardResult:
    _persist_document(result, invocation.result_path, invocation.run_id)
    invocation.terminal_written = True
    return result


def _persist_document(
    result: GuardResultDocument,
    result_path: Path | None,
    run_id: str,
) -> None:
    if result_path is None:
        return
    path = Path(result_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{run_id}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                result.model_dump(mode="json"),
                fh,
                indent=2,
                sort_keys=True,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _bounded_text(value: str, *, limit: int = 500) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _error_category(exc: Exception) -> str:
    if isinstance(exc, ImportError):
        return "ros_import_error"
    if isinstance(exc, OSError):
        return "operating_system_error"
    if isinstance(exc, RuntimeError):
        return "runtime_setup_error"
    return "unexpected_error"
