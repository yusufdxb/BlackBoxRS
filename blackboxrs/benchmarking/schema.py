"""Machine-readable benchmark scenario and result schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScenarioStatus = Literal["supported", "unsupported"]
BenchmarkStatus = Literal["pass", "fail", "skipped", "unsupported", "error"]
ClockMode = Literal["virtual_ros_time", "wall_monotonic"]


class ExpectedTrigger(BaseModel):
    """Expected anomaly identity for a scenario."""

    model_config = ConfigDict(extra="forbid")

    detector: str
    event_type: str
    subject: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ReplayExpectation(BaseModel):
    """Replay assertion for a scenario."""

    model_config = ConfigDict(extra="forbid")

    supported: bool = True
    expected_detector: str | None = None
    event_count_tolerance: int = 0


class PreventionExpectation(BaseModel):
    """Prevention derivation and preflight expectation."""

    model_config = ConfigDict(extra="forbid")

    derivable: bool = False
    expected_check_kind: str | None = None
    recurrence_should_block: bool = False
    healthy_should_pass: bool = True


class ScenarioSpec(BaseModel):
    """Declarative description of one benchmark scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    description: str
    fault_class: str
    detector_expected: str | None = None
    setup: str
    fault_injection: str
    expected_anomaly_kind: str | None = None
    expected_trigger_fields: dict[str, Any] = Field(default_factory=dict)
    replay_expectation: ReplayExpectation = Field(default_factory=ReplayExpectation)
    prevention_expectation: PreventionExpectation = Field(
        default_factory=PreventionExpectation
    )
    timeout_sec: float = Field(default=5.0, gt=0)
    repetitions: int = Field(default=5, ge=1)
    status: ScenarioStatus = "supported"
    unsupported_reason: str | None = None
    healthy_control: bool = False
    expected_triggers: list[ExpectedTrigger] = Field(default_factory=list)
    clock_mode: ClockMode = "virtual_ros_time"

    @model_validator(mode="after")
    def _validate_status(self) -> "ScenarioSpec":
        if self.status == "unsupported" and not self.unsupported_reason:
            raise ValueError("unsupported scenarios must include unsupported_reason")
        if self.status == "supported" and self.unsupported_reason:
            raise ValueError("supported scenarios cannot include unsupported_reason")
        if self.healthy_control and self.expected_triggers:
            raise ValueError("healthy controls cannot declare expected fault triggers")
        if self.expected_triggers and not self.detector_expected:
            raise ValueError("fault scenarios with expected triggers need detector_expected")
        return self


class ScenarioInput(BaseModel):
    """Materialized deterministic event stream for one repetition."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    session_id: str
    events: list[Any]
    window_start: datetime
    window_end: datetime
    fault_activation_time: datetime | None = None
    clock_mode: ClockMode = "virtual_ros_time"


class EnvironmentMetadata(BaseModel):
    """Execution environment captured with every result."""

    model_config = ConfigDict(extra="forbid")

    blackboxrs_version: str
    python_version: str
    platform: str
    hostname: str
    git_commit: str | None = None
    git_dirty: bool | None = None
    clock_mode: ClockMode
    cpu_overhead_available: bool = False
    peak_memory_overhead_available: bool = False


class ReplayResultSchema(BaseModel):
    """Replay comparison outcome."""

    model_config = ConfigDict(extra="forbid")

    supported: bool
    attempted: bool = False
    expected_detector: str | None = None
    observed_detector: str | None = None
    agreement: bool | None = None
    event_count_agreement: bool | None = None
    error: str | None = None


class PreventionResultSchema(BaseModel):
    """Prevention derivation and preflight outcome."""

    model_config = ConfigDict(extra="forbid")

    derivable_expected: bool
    rule_derived: bool = False
    check_kind: str | None = None
    error: str | None = None
    recurrence_blocked: bool | None = None
    healthy_control_passed: bool | None = None


class BenchmarkResult(BaseModel):
    """One scenario repetition result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "blackboxrs.benchmark.result.v1"
    scenario_id: str
    repetition: int
    status: BenchmarkStatus
    passed: bool
    outcome_kind: Literal[
        "detector",
        "healthy_control",
        "artifact_rejection",
        "preflight_rejection",
        "unsupported",
    ] = "detector"
    fault_injected: bool
    expected_detector: str | None = None
    observed_detector: str | None = None
    detection_latency_sec: float | None = None
    latency_clock: ClockMode
    anomaly_count: int = 0
    duplicate_alert_count: int = 0
    incident_path: str | None = None
    incident_integrity_state: str | None = None
    trigger_to_evidence_traceability: bool | None = None
    replay: ReplayResultSchema
    replay_agreement: bool | None = None
    prevention: PreventionResultSchema
    prevention_rule_result: str | None = None
    preflight_recurrence_result: str | None = None
    healthy_control_result: str | None = None
    duration_sec: float
    runtime_duration_sec: float
    cpu_overhead_percent: float | None = None
    peak_memory_overhead_mb: float | None = None
    error: str | None = None
    skipped_reason: str | None = None
    environment: EnvironmentMetadata


class BenchmarkSummary(BaseModel):
    """Aggregated benchmark output."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "blackboxrs.benchmark.summary.v1"
    generated_at: datetime
    results_path: str
    report_path: str
    total_repetitions: int
    scenario_count: int
    supported_scenario_count: int
    unsupported_scenario_count: int
    passed: int
    failed: int
    skipped: int
    unsupported: int
    errors: int
    detector_passed: int
    healthy_control_passed: int
    artifact_rejection_passed: int
    preflight_rejection_passed: int
    scenario_statuses: dict[str, str]
    latency_summary_sec: dict[str, dict[str, float | None]]
    environment: EnvironmentMetadata


def validate_latency_clock(result: BenchmarkResult) -> None:
    """Enforce explicit latency clock semantics."""
    if result.detection_latency_sec is not None and not result.latency_clock:
        raise ValueError("latency clock must be present when latency is measured")
