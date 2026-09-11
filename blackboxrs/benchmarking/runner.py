"""Benchmark execution engine."""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from typing import Iterable

from blackboxrs import __version__
from blackboxrs.anomaly_engine.engine import AnomalyEngine
from blackboxrs.benchmarking.preflight import GraphProbe, QoSEndpoint, patched_graph_probe
from blackboxrs.benchmarking.scenarios import get_scenario, iter_scenarios
from blackboxrs.benchmarking.scenarios.base import BenchmarkScenario
from blackboxrs.benchmarking.scenarios.builtin import make_corrupted_copy
from blackboxrs.benchmarking.schema import (
    BenchmarkResult,
    BenchmarkStatus,
    BenchmarkSummary,
    EnvironmentMetadata,
    PreventionResultSchema,
    ReplayResultSchema,
    ScenarioInput,
    validate_latency_clock,
)
from blackboxrs.core.clock import Clock
from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.core.session import Session
from blackboxrs.incident.api import build_incident
from blackboxrs.incident.bundle import BundleReader, validate_bundle_path
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_rule_from_bundle,
)
from blackboxrs.prevention.runner import PreflightRunner
from blackboxrs.prevention.rules import PreflightCheck, PreventionRule


def _git_metadata(repo: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return commit, bool(status.strip())
    except Exception:
        return None, None


def _environment(repo: Path, clock_mode: str) -> EnvironmentMetadata:
    commit, dirty = _git_metadata(repo)
    return EnvironmentMetadata(
        blackboxrs_version=__version__,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        hostname=socket.gethostname(),
        git_commit=commit,
        git_dirty=dirty,
        clock_mode=clock_mode,  # type: ignore[arg-type]
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def _write_events(log_dir: Path, events: Iterable[BlackBoxEvent]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "blackboxrs_20260714_120000_000000.jsonl"
    with open(target, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(event.to_jsonl())
            fh.write("\n")
    return target


def _match_expected(
    anomaly: BlackBoxEvent,
    *,
    expected_detector: str | None,
    expected_kind: str | None,
    expected_fields: dict[str, object],
) -> bool:
    if expected_detector and anomaly.data.get("detector") != expected_detector:
        return False
    if expected_kind and anomaly.event_type != expected_kind:
        return False
    for key, expected in expected_fields.items():
        if key.endswith("_prefix"):
            field = key.removesuffix("_prefix")
            value = anomaly.data.get(field)
            if not isinstance(value, str) or not value.startswith(str(expected)):
                return False
            continue
        if anomaly.data.get(key) != expected:
            return False
    return True


def _traceability_ok(bundle: Path) -> bool:
    try:
        reader = BundleReader(bundle)
        triggers = reader.load_triggers()
    except Exception:
        return False
    if not triggers:
        return False
    events_path = bundle / "evidence" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    for trigger in triggers:
        if not trigger.source_event_ref:
            return False
        if not trigger.source_event_ref.startswith("events.jsonl#L"):
            return False
        try:
            line_no = int(trigger.source_event_ref.rsplit("L", 1)[1])
        except ValueError:
            return False
        if line_no < 1 or line_no > len(lines):
            return False
    return True


def _run_detection(
    scenario_input: ScenarioInput,
    *,
    config: BlackBoxConfig,
) -> list[BlackBoxEvent]:
    bus = EventBus(default_queue_maxsize=4096)
    anomalies_q = bus.subscribe(channel="anomaly_engine", maxsize=256)
    session = Session()
    session.session_id = scenario_input.session_id
    engine = AnomalyEngine(bus, config.anomaly_engine, session)
    engine.start()
    anomalies: list[BlackBoxEvent] = []
    try:
        for event in scenario_input.events:
            if scenario_input.clock_mode == "virtual_ros_time":
                Clock.set_virtual_time(event.timestamp)
            else:
                Clock.use_wall_clock()
            bus.publish(event)
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                try:
                    anomaly = anomalies_q.get(timeout=0.01)
                except Empty:
                    continue
                anomalies.append(anomaly)
                break
        drain_deadline = time.monotonic() + 0.5
        while time.monotonic() < drain_deadline:
            try:
                anomalies.append(anomalies_q.get(timeout=0.02))
            except Empty:
                break
    finally:
        Clock.use_wall_clock()
        engine.stop()
    return anomalies


def _replay_bundle_detection(
    bundle: Path,
    *,
    config: BlackBoxConfig,
    session_id: str,
) -> list[BlackBoxEvent]:
    """Replay bundle evidence through EventBus and AnomalyEngine."""
    reader = BundleReader(bundle)
    raw_events = [event for event in reader.iter_events() if event.source != "anomaly_engine"]
    replay_input = ScenarioInput(
        session_id=f"{session_id}_replay",
        events=raw_events,
        window_start=raw_events[0].timestamp if raw_events else datetime.now(timezone.utc),
        window_end=raw_events[-1].timestamp if raw_events else datetime.now(timezone.utc),
        clock_mode="virtual_ros_time",
    )
    return _run_detection(replay_input, config=config)


def _latency(
    anomalies: list[BlackBoxEvent],
    scenario_input: ScenarioInput,
    *,
    expected_detector: str | None,
    expected_kind: str | None,
    expected_fields: dict[str, object],
) -> float | None:
    if scenario_input.fault_activation_time is None:
        return None
    for anomaly in anomalies:
        if _match_expected(
            anomaly,
            expected_detector=expected_detector,
            expected_kind=expected_kind,
            expected_fields=expected_fields,
        ):
            return (
                anomaly.timestamp - scenario_input.fault_activation_time
            ).total_seconds()
    return None


def _preflight_probe_for_rule(rule: PreventionRule, *, healthy: bool) -> GraphProbe:
    topic = rule.check.params.get("topic")
    if not isinstance(topic, str):
        return GraphProbe()
    if rule.check.kind == "topic_present":
        return GraphProbe(publishers={topic: 1 if healthy else 0})
    if rule.check.kind == "qos_match":
        if healthy:
            return GraphProbe(
                qos_publishers={topic: [QoSEndpoint("RELIABLE", "VOLATILE")]},
                qos_subscribers={topic: [QoSEndpoint("BEST_EFFORT", "VOLATILE")]},
            )
        return GraphProbe(
            qos_publishers={topic: [QoSEndpoint("BEST_EFFORT", "VOLATILE")]},
            qos_subscribers={topic: [QoSEndpoint("RELIABLE", "VOLATILE")]},
        )
    return GraphProbe()


def _run_preflight(rule: PreventionRule, probe: GraphProbe) -> int:
    with patched_graph_probe(probe):
        return PreflightRunner([rule]).run().exit_code


class BenchmarkRunner:
    """Runs benchmark scenarios and writes reproducible artifacts."""

    def __init__(
        self,
        *,
        output_dir: Path,
        repo_root: Path | None = None,
        seed: int = 0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.repo_root = repo_root or Path.cwd()
        self.seed = seed
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        scenarios: Iterable[BenchmarkScenario],
        *,
        repetitions_override: int | None = None,
        fail_fast: bool = False,
    ) -> list[BenchmarkResult]:
        results: list[BenchmarkResult] = []
        for scenario in scenarios:
            reps = repetitions_override or scenario.spec.repetitions
            if scenario.spec.status == "unsupported":
                for repetition in range(1, reps + 1):
                    results.append(self._unsupported_result(scenario, repetition))
                continue
            for repetition in range(1, reps + 1):
                result = self._run_one(scenario, repetition)
                results.append(result)
                if fail_fast and not result.passed:
                    return results
        return results

    def _unsupported_result(
        self,
        scenario: BenchmarkScenario,
        repetition: int,
    ) -> BenchmarkResult:
        env = _environment(self.repo_root, scenario.spec.clock_mode)
        outcome_kind = "detector"
        if scenario.spec.status == "unsupported":
            outcome_kind = "unsupported"
        elif scenario.spec.healthy_control:
            outcome_kind = "healthy_control"
        elif scenario.spec.scenario_id == "corrupted_bundle_rejection":
            outcome_kind = "artifact_rejection"
        elif scenario.spec.scenario_id == "unsupported_prevention_condition":
            outcome_kind = "preflight_rejection"
        return BenchmarkResult(
            scenario_id=scenario.spec.scenario_id,
            repetition=repetition,
            status="unsupported",
            passed=False,
            outcome_kind=outcome_kind,  # type: ignore[arg-type]
            fault_injected=False,
            expected_detector=scenario.spec.detector_expected,
            latency_clock=scenario.spec.clock_mode,
            replay=ReplayResultSchema(supported=False),
            prevention=PreventionResultSchema(
                derivable_expected=scenario.spec.prevention_expectation.derivable
            ),
            duration_sec=0.0,
            runtime_duration_sec=0.0,
            skipped_reason=scenario.spec.unsupported_reason,
            environment=env,
        )

    def _run_one(self, scenario: BenchmarkScenario, repetition: int) -> BenchmarkResult:
        start_ns = time.perf_counter_ns()
        env = _environment(self.repo_root, scenario.spec.clock_mode)
        outcome_kind = "healthy_control" if scenario.spec.healthy_control else "detector"
        if scenario.spec.scenario_id == "corrupted_bundle_rejection":
            outcome_kind = "artifact_rejection"
        elif scenario.spec.scenario_id == "unsupported_prevention_condition":
            outcome_kind = "preflight_rejection"
        work_dir = self.output_dir / "work" / scenario.spec.scenario_id / str(repetition)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        config = scenario.configure(BlackBoxConfig.default())
        config.log_dir = str(work_dir / "logs")
        incidents_dir = work_dir / "incidents"
        expected_detector = scenario.spec.detector_expected
        replay = ReplayResultSchema(
            supported=scenario.spec.replay_expectation.supported,
            expected_detector=scenario.spec.replay_expectation.expected_detector,
        )
        prevention = PreventionResultSchema(
            derivable_expected=scenario.spec.prevention_expectation.derivable
        )
        status: BenchmarkStatus = "fail"
        error: str | None = None
        bundle_path: Path | None = None
        anomalies: list[BlackBoxEvent] = []
        integrity_state: str | None = None
        traceability: bool | None = None
        observed_detector: str | None = None
        latency_sec: float | None = None
        healthy_result: str | None = None
        recurrence_result: str | None = None
        prevention_rule_result: str | None = None

        try:
            scenario_input = scenario.materialize(
                work_dir,
                repetition=repetition,
                seed=self.seed,
            )
            anomalies = _run_detection(scenario_input, config=config)
            observed_detector = anomalies[0].data.get("detector") if anomalies else None
            all_events = list(scenario_input.events) + anomalies
            all_events.sort(key=lambda event: event.timestamp)
            _write_events(Path(config.log_dir), all_events)
            bundle_path = build_incident(
                scenario_input.window_start,
                scenario_input.window_end,
                config=config,
                incidents_dir=incidents_dir,
                title=f"Benchmark: {scenario.spec.scenario_id}",
                notes=(
                    "Generated by the local BlackBoxRS reliability benchmark. "
                    "Synthetic/local evidence only; not live robot validation."
                ),
                tags=("benchmark", scenario.spec.fault_class),
            )
            validation = validate_bundle_path(bundle_path, require_finalized=True)
            integrity_state = validation.state
            traceability = _traceability_ok(bundle_path) if anomalies else None

            if scenario.spec.replay_expectation.supported:
                replay.attempted = True
                replay_anomalies = _replay_bundle_detection(
                    bundle_path,
                    config=config,
                    session_id=scenario_input.session_id,
                )
                replay.observed_detector = (
                    replay_anomalies[0].data.get("detector") if replay_anomalies else None
                )
                replay.agreement = (
                    len(replay_anomalies) == len(anomalies)
                    and all(
                        left.event_type == right.event_type
                        and left.data.get("detector") == right.data.get("detector")
                        and left.data.get("metric") == right.data.get("metric")
                        for left, right in zip(anomalies, replay_anomalies)
                    )
                )
                raw_event_count = sum(1 for event in all_events if event.source != "anomaly_engine")
                replay_event_count = len(
                    [event for event in BundleReader(bundle_path).iter_events() if event.source != "anomaly_engine"]
                )
                replay.event_count_agreement = raw_event_count == replay_event_count

            if scenario.spec.scenario_id == "corrupted_bundle_rejection":
                corrupted = make_corrupted_copy(
                    bundle_path,
                    work_dir / "corrupted_bundle",
                )
                corrupted_result = validate_bundle_path(corrupted, require_finalized=True)
                integrity_state = corrupted_result.state
                bundle_path = corrupted
                if corrupted_result.state == "corrupted" and corrupted_result.errors:
                    status = "pass"
                else:
                    status = "fail"
                    error = "corrupted bundle was not rejected"
                return self._result(
                    scenario=scenario,
                    repetition=repetition,
                    status=status,
                    expected_detector=expected_detector,
                    observed_detector=observed_detector,
                    latency_sec=None,
                    anomaly_count=len(anomalies),
                    bundle_path=bundle_path,
                    integrity_state=integrity_state,
                    traceability=traceability,
                    replay=replay,
                    prevention=prevention,
                    prevention_rule_result=None,
                    recurrence_result=None,
                    healthy_result=None,
                    duration_ns=time.perf_counter_ns() - start_ns,
                    error=error,
                    environment=env,
                    outcome_kind=outcome_kind,
                )

            if scenario.spec.scenario_id == "unsupported_prevention_condition":
                bad_check = PreflightCheck.model_construct(
                    name="unsupported benchmark check",
                    kind="future_kind",
                    params={},
                    severity_on_fail="block",
                )
                bad_rule = PreventionRule.model_construct(
                    rule_id="rule_benchmark_unsupported",
                    created_at=datetime.now(timezone.utc),
                    check=bad_check,
                    disabled=False,
                )
                report = PreflightRunner([bad_rule]).run()
                if report.exit_code == 1 and report.results[0].status == "error":
                    status = "pass"
                    prevention_rule_result = "unsupported_check_rejected"
                else:
                    status = "fail"
                    error = "unsupported preflight condition did not fail closed"
                return self._result(
                    scenario=scenario,
                    repetition=repetition,
                    status=status,
                    expected_detector=expected_detector,
                    observed_detector=observed_detector,
                    latency_sec=None,
                    anomaly_count=len(anomalies),
                    bundle_path=bundle_path,
                    integrity_state=integrity_state,
                    traceability=traceability,
                    replay=replay,
                    prevention=prevention,
                    prevention_rule_result=prevention_rule_result,
                    recurrence_result=None,
                    healthy_result=None,
                    duration_ns=time.perf_counter_ns() - start_ns,
                    error=error,
                    environment=env,
                    outcome_kind=outcome_kind,
                )

            expected_match = any(
                _match_expected(
                    anomaly,
                    expected_detector=expected_detector,
                    expected_kind=scenario.spec.expected_anomaly_kind,
                    expected_fields=scenario.spec.expected_trigger_fields,
                )
                for anomaly in anomalies
            )
            latency_sec = _latency(
                anomalies,
                scenario_input,
                expected_detector=expected_detector,
                expected_kind=scenario.spec.expected_anomaly_kind,
                expected_fields=scenario.spec.expected_trigger_fields,
            )
            if scenario.spec.prevention_expectation.derivable:
                try:
                    derivation = derive_rule_from_bundle(BundleReader(bundle_path))
                    prevention.rule_derived = True
                    prevention.check_kind = derivation.rule.check.kind
                    prevention_rule_result = derivation.rule.rule_id
                    recurrence_probe = _preflight_probe_for_rule(
                        derivation.rule, healthy=False
                    )
                    healthy_probe = _preflight_probe_for_rule(
                        derivation.rule, healthy=True
                    )
                    recurrence_code = _run_preflight(derivation.rule, recurrence_probe)
                    healthy_code = _run_preflight(derivation.rule, healthy_probe)
                    prevention.recurrence_blocked = recurrence_code == 1
                    prevention.healthy_control_passed = healthy_code == 0
                    recurrence_result = "block" if recurrence_code == 1 else "not_blocked"
                    healthy_result = "pass" if healthy_code == 0 else "fail"
                except PreventionDerivationError as exc:
                    prevention.error = str(exc)
            else:
                try:
                    derive_rule_from_bundle(BundleReader(bundle_path))
                except PreventionDerivationError as exc:
                    prevention.error = str(exc)
                    prevention.rule_derived = False

            if scenario.spec.healthy_control:
                status = "pass" if not anomalies and integrity_state == "valid_finalized" else "fail"
                if anomalies:
                    error = "healthy control emitted anomaly"
            else:
                status = "pass" if expected_match and integrity_state == "valid_finalized" else "fail"
                if not expected_match and expected_detector:
                    error = "expected detector did not emit matching anomaly"
                if scenario.spec.prevention_expectation.derivable:
                    if not prevention.rule_derived:
                        status = "fail"
                        error = prevention.error or "prevention rule was not derived"
                    if prevention.recurrence_blocked is not True:
                        status = "fail"
                        error = "recurrence was not blocked"
                    if prevention.healthy_control_passed is not True:
                        status = "fail"
                        error = "derived rule failed healthy control"
            if replay.supported and replay.attempted and replay.agreement is False:
                status = "fail"
                error = "replay detector agreement failed"
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"

        return self._result(
            scenario=scenario,
            repetition=repetition,
            status=status,
            expected_detector=expected_detector,
            observed_detector=observed_detector,
            latency_sec=latency_sec,
            anomaly_count=len(anomalies),
            bundle_path=bundle_path,
            integrity_state=integrity_state,
            traceability=traceability,
            replay=replay,
            prevention=prevention,
            prevention_rule_result=prevention_rule_result,
            recurrence_result=recurrence_result,
            healthy_result=healthy_result,
            duration_ns=time.perf_counter_ns() - start_ns,
            error=error,
            environment=env,
            outcome_kind=outcome_kind,  # type: ignore[arg-type]
        )

    def _result(
        self,
        *,
        scenario: BenchmarkScenario,
        repetition: int,
        status: BenchmarkStatus,
        expected_detector: str | None,
        observed_detector: str | None,
        latency_sec: float | None,
        anomaly_count: int,
        bundle_path: Path | None,
        integrity_state: str | None,
        traceability: bool | None,
        replay: ReplayResultSchema,
        prevention: PreventionResultSchema,
        prevention_rule_result: str | None,
        recurrence_result: str | None,
        healthy_result: str | None,
        duration_ns: int,
        error: str | None,
        environment: EnvironmentMetadata,
        outcome_kind: str,
    ) -> BenchmarkResult:
        duplicate_alert_count = 0
        if anomaly_count > 1 and expected_detector:
            duplicate_alert_count = anomaly_count - 1
        if outcome_kind == "healthy_control" and healthy_result is None:
            healthy_result = "pass" if status == "pass" else "fail"
        result = BenchmarkResult(
            scenario_id=scenario.spec.scenario_id,
            repetition=repetition,
            status=status,
            passed=status == "pass",
            outcome_kind=outcome_kind,  # type: ignore[arg-type]
            fault_injected=not scenario.spec.healthy_control,
            expected_detector=expected_detector,
            observed_detector=observed_detector,
            detection_latency_sec=latency_sec,
            latency_clock=scenario.spec.clock_mode,
            anomaly_count=anomaly_count,
            duplicate_alert_count=duplicate_alert_count,
            incident_path=str(bundle_path) if bundle_path else None,
            incident_integrity_state=integrity_state,
            trigger_to_evidence_traceability=traceability,
            replay=replay,
            replay_agreement=replay.agreement,
            prevention=prevention,
            prevention_rule_result=prevention_rule_result,
            preflight_recurrence_result=recurrence_result,
            healthy_control_result=healthy_result,
            duration_sec=round(duration_ns / 1_000_000_000, 6),
            runtime_duration_sec=round(duration_ns / 1_000_000_000, 6),
            error=error,
            environment=environment,
        )
        validate_latency_clock(result)
        return result


def _latency_summary(results: list[BenchmarkResult]) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for result in results:
        if result.detection_latency_sec is not None:
            grouped[result.scenario_id].append(result.detection_latency_sec)
    summary: dict[str, dict[str, float | None]] = {}
    for scenario_id in sorted({result.scenario_id for result in results}):
        values = sorted(grouped.get(scenario_id, []))
        if not values:
            summary[scenario_id] = {"min": None, "median": None, "max": None}
            continue
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        summary[scenario_id] = {
            "min": round(values[0], 6),
            "median": round(median, 6),
            "max": round(values[-1], 6),
        }
    return summary


def summarize_results(
    results: list[BenchmarkResult],
    *,
    output_dir: Path,
    environment: EnvironmentMetadata,
) -> BenchmarkSummary:
    counts = Counter(result.status for result in results)
    scenario_ids = sorted({result.scenario_id for result in results})
    scenario_statuses = {}
    for scenario_id in scenario_ids:
        scenario_results = [r for r in results if r.scenario_id == scenario_id]
        if all(r.status == "pass" for r in scenario_results):
            scenario_statuses[scenario_id] = "pass"
        elif all(r.status == "unsupported" for r in scenario_results):
            scenario_statuses[scenario_id] = "unsupported"
        elif any(r.status == "error" for r in scenario_results):
            scenario_statuses[scenario_id] = "error"
        else:
            scenario_statuses[scenario_id] = "fail"
    return BenchmarkSummary(
        generated_at=datetime.now(timezone.utc),
        results_path=str(output_dir / "raw_results.json"),
        report_path=str(output_dir / "report.md"),
        total_repetitions=len(results),
        scenario_count=len(scenario_ids),
        supported_scenario_count=sum(1 for s in scenario_statuses.values() if s != "unsupported"),
        unsupported_scenario_count=sum(1 for s in scenario_statuses.values() if s == "unsupported"),
        passed=counts["pass"],
        failed=counts["fail"],
        skipped=counts["skipped"],
        unsupported=counts["unsupported"],
        errors=counts["error"],
        detector_passed=sum(
            1 for result in results
            if result.status == "pass" and result.outcome_kind == "detector"
        ),
        healthy_control_passed=sum(
            1 for result in results
            if result.status == "pass" and result.outcome_kind == "healthy_control"
        ),
        artifact_rejection_passed=sum(
            1 for result in results
            if result.status == "pass" and result.outcome_kind == "artifact_rejection"
        ),
        preflight_rejection_passed=sum(
            1 for result in results
            if result.status == "pass" and result.outcome_kind == "preflight_rejection"
        ),
        scenario_statuses=scenario_statuses,
        latency_summary_sec=_latency_summary(results),
        environment=environment,
    )


def render_markdown_report(
    results: list[BenchmarkResult],
    summary: BenchmarkSummary,
) -> str:
    by_scenario: dict[str, list[BenchmarkResult]] = defaultdict(list)
    for result in results:
        by_scenario[result.scenario_id].append(result)
    lines = [
        "# BlackBoxRS ROS 2 Reliability Benchmark",
        "",
        "Synthetic/local benchmark evidence only. These results do not claim real-world precision, recall, hardware performance, or live robot safety.",
        "",
        "| Scenario | Expected detector | Repetitions | Detected | Median latency | Bundle valid | Replay agreement | Rule derived | Recurrence blocked | Healthy control |",
        "| -------- | ----------------- | ----------: | -------: | -------------: | -----------: | ---------------: | -----------: | -----------------: | --------------: |",
    ]
    for scenario_id in sorted(by_scenario):
        rows = by_scenario[scenario_id]
        reps = len(rows)
        expected = rows[0].expected_detector or "-"
        detected = sum(
            1
            for row in rows
            if row.expected_detector and row.observed_detector == row.expected_detector
        )
        median = summary.latency_summary_sec.get(scenario_id, {}).get("median")
        median_text = "-" if median is None else f"{median:.3f}s"
        bundle_valid = sum(1 for row in rows if row.incident_integrity_state == "valid_finalized")
        bundle_text = str(bundle_valid)
        if rows[0].outcome_kind == "artifact_rejection":
            rejected = sum(
                1 for row in rows
                if row.status == "pass" and row.incident_integrity_state == "corrupted"
            )
            bundle_text = f"rejected {rejected}"
        replay_ok = sum(1 for row in rows if row.replay_agreement is True)
        rule = sum(1 for row in rows if row.prevention.rule_derived)
        blocked = sum(1 for row in rows if row.prevention.recurrence_blocked is True)
        healthy = sum(
            1
            for row in rows
            if row.healthy_control_result == "pass"
        )
        lines.append(
            f"| {scenario_id} | {expected} | {reps} | {detected} | {median_text} | "
            f"{bundle_text} | {replay_ok} | {rule} | {blocked} | {healthy} |"
        )
    lines.extend(
        [
            "",
            "## Environment",
            "",
            f"- BlackBoxRS: `{summary.environment.blackboxrs_version}`",
            f"- Python: `{summary.environment.python_version}`",
            f"- Platform: `{summary.environment.platform}`",
            f"- Hostname: `{summary.environment.hostname}`",
            f"- Git commit: `{summary.environment.git_commit or 'unknown'}`",
            f"- Git dirty: `{summary.environment.git_dirty}`",
        ]
    )
    failed = [row for row in results if row.status in ("fail", "error")]
    unsupported = [row for row in results if row.status == "unsupported"]
    lines.extend(["", "## Failed repetitions", ""])
    if failed:
        for row in failed:
            lines.append(f"- {row.scenario_id} repetition {row.repetition}: {row.error or row.status}")
    else:
        lines.append("- None")
    lines.extend(["", "## Unsupported scenarios", ""])
    if unsupported:
        seen: set[str] = set()
        for row in unsupported:
            if row.scenario_id in seen:
                continue
            seen.add(row.scenario_id)
            lines.append(f"- {row.scenario_id}: {row.skipped_reason}")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Environment limitations",
            "",
            "- Detection latency uses the scenario clock recorded in each JSON result.",
            "- CPU overhead and peak memory overhead are reported as unavailable.",
            "- Deterministic preflight graph probes replace live rclpy discovery for local recurrence checks.",
            "- Unsupported taxonomy entries remain visible and are not counted as passes.",
            "",
        ]
    )
    return "\n".join(lines)


def run_benchmark(
    *,
    output_dir: Path,
    scenario_ids: list[str] | None = None,
    repetitions: int | None = None,
    include_unsupported: bool = False,
    fail_fast: bool = False,
    seed: int = 0,
    repo_root: Path | None = None,
) -> tuple[list[BenchmarkResult], BenchmarkSummary]:
    if scenario_ids:
        scenarios = [get_scenario(sid) for sid in scenario_ids]
    else:
        scenarios = list(iter_scenarios(include_unsupported=include_unsupported))
    runner = BenchmarkRunner(output_dir=output_dir, repo_root=repo_root, seed=seed)
    results = runner.run(
        scenarios,
        repetitions_override=repetitions,
        fail_fast=fail_fast,
    )
    env = _environment(repo_root or Path.cwd(), "virtual_ros_time")
    summary = summarize_results(results, output_dir=output_dir, environment=env)
    _write_json(
        output_dir / "raw_results.json",
        [result.model_dump(mode="json") for result in results],
    )
    _write_json(output_dir / "summary.json", summary.model_dump(mode="json"))
    (output_dir / "report.md").write_text(
        render_markdown_report(results, summary),
        encoding="utf-8",
    )
    return results, summary
