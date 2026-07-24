#!/usr/bin/env python3
"""Run the causal local ROS 2 telemetry-health comparison matrix."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.prevention.rules import (  # noqa: E402
    PreflightCheck,
    load_rule,
    make_rule,
    save_rule,
)
from blackboxrs.prevention.telemetry_health import (  # noqa: E402
    compute_evidence_fingerprint,
    load_telemetry_evidence,
)


PUBLISHER = REPO_ROOT / "scripts" / "telemetry_health_publisher.py"
TOPIC = "/utlidar/robot_pose"


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1.0)
    except ProcessLookupError:
        process.wait(timeout=1.0)


def _start_publisher(
    domain: int,
    arguments: list[str],
    *,
    topic: str = TOPIC,
) -> subprocess.Popen[Any]:
    env = {**os.environ, "ROS_DOMAIN_ID": str(domain)}
    return subprocess.Popen(
        [
            sys.executable,
            str(PUBLISHER),
            "--topic",
            topic,
            "--duration-sec",
            "20",
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _start_wrong_type_publisher(domain: int) -> subprocess.Popen[Any]:
    env = {**os.environ, "ROS_DOMAIN_ID": str(domain)}
    return subprocess.Popen(
        [
            "ros2",
            "topic",
            "pub",
            "-r",
            "18",
            TOPIC,
            "std_msgs/msg/String",
            "{data: wrong_type}",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _dependent_command() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(20)"]


def _run_guard(
    *,
    name: str,
    domain: int,
    rule_path: Path,
    out_dir: Path,
    publisher_arguments: list[str] | None = None,
    wrong_type: bool = False,
    extra_publishers: list[tuple[str, list[str]]] | None = None,
    monitor_duration: float = 3.5,
    publisher_settle_sec: float = 0.05,
) -> dict[str, Any]:
    processes: list[subprocess.Popen[Any]] = []
    try:
        processes.append(
            _start_wrong_type_publisher(domain)
            if wrong_type
            else _start_publisher(domain, publisher_arguments or [])
        )
        for topic, arguments in extra_publishers or []:
            processes.append(_start_publisher(domain, arguments, topic=topic))
        time.sleep(publisher_settle_sec)
        result_path = out_dir / "guard_results" / f"{name}.json"
        env = {**os.environ, "ROS_DOMAIN_ID": str(domain)}
        command = [
            sys.executable,
            "-m",
            "blackboxrs",
            "prevention",
            "guard",
            "--rule",
            str(rule_path),
            "--result",
            str(result_path),
            "--monitor-duration",
            str(monitor_duration),
            "--context",
            rule.graph_context if hasattr(rule, "graph_context") else rule.check.params["graph_context"],
            "--trusted-rule-fingerprint",
            rule.rule_fingerprint or "",
            "--",
            *_dependent_command(),
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=monitor_duration + 5.0,
            check=False,
        )
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"status": "blocked", "reason": "guard_refused"}
        result["cli_exit_code"] = completed.returncode
        result["publisher_alive_after_guard"] = processes[0].poll() is None
        return result
    finally:
        for process in processes:
            _terminate(process)


def _run_preflight_then_dependent(
    *,
    name: str,
    domain: int,
    rules_dir: Path,
    out_dir: Path,
    publisher_arguments: list[str],
    wait_until_bad_sec: float,
) -> dict[str, Any]:
    publisher = _start_publisher(domain, publisher_arguments)
    try:
        time.sleep(wait_until_bad_sec)
        env = {**os.environ, "ROS_DOMAIN_ID": str(domain)}
        preflight = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackboxrs",
                "preflight",
                "--rules-dir",
                str(rules_dir),
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
        marker = out_dir / "dependent_markers" / f"{name}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        dependent = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(marker)!r}).write_text('started', encoding='utf-8')"
                ),
            ],
            check=False,
        )
        return {
            "preflight_exit_code": preflight.returncode,
            "preflight_stdout": preflight.stdout.strip(),
            "publisher_alive": publisher.poll() is None,
            "dependent_started": dependent.returncode == 0 and marker.exists(),
        }
    finally:
        _terminate(publisher)


def _make_presence_rule(rules_dir: Path) -> Path:
    rule = make_rule(
        PreflightCheck(
            name=f"topic present: {TOPIC}",
            kind="topic_present",
            params={"topic": TOPIC, "min_publishers": 1},
            severity_on_fail="block",
        ),
        rationale="Presence-only causal comparison.",
    )
    return save_rule(rule, rules_dir)


def _insufficient_evidence_control(
    *,
    evidence_path: Path,
    incident_dir: Path,
    out_dir: Path,
) -> dict[str, Any]:
    evidence = load_telemetry_evidence(evidence_path)
    insufficient_stats = evidence.statistics.model_copy(
        update={"message_count": 10}
    )
    insufficient = evidence.model_copy(
        update={
            "statistics": insufficient_stats,
            "evidence_fingerprint": None,
        }
    )
    insufficient = insufficient.model_copy(
        update={"evidence_fingerprint": compute_evidence_fingerprint(insufficient)}
    )
    path = out_dir / "insufficient_healthy_evidence.json"
    path.write_text(
        json.dumps(insufficient.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "blackboxrs",
            "prevention",
            "adopt-telemetry-health",
            "--from-incident",
            str(incident_dir),
            "--healthy-evidence",
            str(path),
            "--rules-dir",
            str(out_dir / "insufficient_rules"),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "refused": completed.returncode != 0,
        "stdout": completed.stdout.strip(),
    }


def _tampered_rule_control(
    *,
    rule_path: Path,
    out_dir: Path,
    domain: int,
) -> dict[str, Any]:
    data = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    data["check"]["params"]["minimum_rate_hz"] = 1.0
    tampered_path = out_dir / "tampered_rule.yaml"
    tampered_path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )
    publisher = _start_publisher(domain, ["--rate-hz", "18.75"])
    try:
        time.sleep(0.05)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackboxrs",
                "prevention",
                "guard",
                "--rule",
                str(tampered_path),
                "--monitor-duration",
                "1",
                "--context",
                data["check"]["params"]["graph_context"],
                "--trusted-rule-fingerprint",
                rule.rule_fingerprint or "",
                "--",
                *_dependent_command(),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "ROS_DOMAIN_ID": str(domain)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "refused": completed.returncode != 0,
            "stdout": completed.stdout.strip(),
        }
    finally:
        _terminate(publisher)


def run(
    *,
    rule_path: Path,
    evidence_path: Path,
    incident_dir: Path,
    out_dir: Path,
    domain_start: int,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    empty_rules = out_dir / "no_rules"
    empty_rules.mkdir(exist_ok=True)
    presence_rules = out_dir / "presence_rules"
    _make_presence_rule(presence_rules)
    domain = domain_start

    baseline_specs = {
        "healthy": (["--rate-hz", "18.75"], 0.1),
        "silent_after_start": (
            ["--rate-hz", "18.75", "--silent-after-sec", "0.5"],
            0.8,
        ),
        "slow": (["--rate-hz", "10.0"], 0.2),
        "frozen_timestamp": (
            ["--rate-hz", "18.75", "--freeze-after-sec", "0.2"],
            0.5,
        ),
    }
    baseline: dict[str, Any] = {}
    presence: dict[str, Any] = {}
    for name, (publisher_args, wait_sec) in baseline_specs.items():
        baseline[name] = _run_preflight_then_dependent(
            name=f"no_rule_{name}",
            domain=domain,
            rules_dir=empty_rules,
            out_dir=out_dir,
            publisher_arguments=publisher_args,
            wait_until_bad_sec=wait_sec,
        )
        domain += 1
        presence[name] = _run_preflight_then_dependent(
            name=f"presence_{name}",
            domain=domain,
            rules_dir=presence_rules,
            out_dir=out_dir,
            publisher_arguments=publisher_args,
            wait_until_bad_sec=wait_sec,
        )
        domain += 1

    valid_specs: dict[str, dict[str, Any]] = {
        "healthy_median_rate": {
            "publisher_arguments": ["--rate-hz", "18.756"],
        },
        "near_above_minimum": {
            "publisher_arguments": ["--rate-hz", "16.0"],
        },
        "short_gap_below_stale_timeout": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--pause-at-sec",
                "2.6",
                "--pause-duration-sec",
                "0.05",
            ],
        },
        "startup_delay_inside_grace": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--startup-delay-sec",
                "0.45",
            ],
            "publisher_settle_sec": 0.02,
            "monitor_duration": 3.8,
        },
        "genuine_scale_jitter": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--jitter-sec",
                "0.007,-0.005,0.004,-0.006,0.002,-0.002",
            ],
        },
        "unrelated_silent_topic": {
            "publisher_arguments": ["--rate-hz", "18.75"],
            "extra_publishers": [
                (
                    "/unrelated/pose",
                    ["--rate-hz", "18.75", "--max-messages", "1"],
                )
            ],
        },
    }
    valid: dict[str, Any] = {}
    for name, kwargs in valid_specs.items():
        valid[name] = _run_guard(
            name=name,
            domain=domain,
            rule_path=rule_path,
            out_dir=out_dir,
            **kwargs,
        )
        domain += 1

    invalid_specs: dict[str, dict[str, Any]] = {
        "publisher_zero_messages": {
            "publisher_arguments": ["--rate-hz", "18.75", "--max-messages", "0"],
        },
        "one_message_then_stop": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--startup-delay-sec",
                "0.2",
                "--max-messages",
                "1",
            ],
            "publisher_settle_sec": 0.02,
        },
        "below_minimum_rate": {
            "publisher_arguments": ["--rate-hz", "10.0"],
        },
        "silent_after_qualification": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--silent-after-sec",
                "2.7",
            ],
            "monitor_duration": 4.0,
        },
        "frozen_timestamp": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--freeze-after-sec",
                "2.5",
            ],
            "monitor_duration": 4.0,
        },
        "incompatible_qos": {
            "publisher_arguments": [
                "--rate-hz",
                "18.75",
                "--best-effort",
            ],
        },
        "wrong_type": {"wrong_type": True},
    }
    invalid: dict[str, Any] = {}
    for name, kwargs in invalid_specs.items():
        invalid[name] = _run_guard(
            name=name,
            domain=domain,
            rule_path=rule_path,
            out_dir=out_dir,
            **kwargs,
        )
        domain += 1

    removed = _run_preflight_then_dependent(
        name="rule_removed_silent",
        domain=domain,
        rules_dir=empty_rules,
        out_dir=out_dir,
        publisher_arguments=[
            "--rate-hz",
            "18.75",
            "--silent-after-sec",
            "0.5",
        ],
        wait_until_bad_sec=0.8,
    )
    domain += 1
    restart_failure = _run_guard(
        name="publisher_stops_before_restart",
        domain=domain,
        rule_path=rule_path,
        out_dir=out_dir,
        publisher_arguments=[
            "--rate-hz",
            "18.75",
            "--duration-sec",
            "2.7",
        ],
        monitor_duration=4.0,
    )
    recovery = _run_guard(
        name="fresh_guard_after_publisher_restart",
        domain=domain,
        rule_path=rule_path,
        out_dir=out_dir,
        publisher_arguments=["--rate-hz", "18.75"],
    )
    domain += 1
    insufficient = _insufficient_evidence_control(
        evidence_path=evidence_path,
        incident_dir=incident_dir,
        out_dir=out_dir,
    )
    tampered = _tampered_rule_control(
        rule_path=rule_path,
        out_dir=out_dir,
        domain=domain,
    )

    valid_count = len(valid)
    invalid_count = len(invalid)
    false_blocks = sum(result["status"] != "passed" for result in valid.values())
    false_admits = sum(result["status"] == "passed" for result in invalid.values())
    true_blocks = invalid_count - false_admits
    detection_latencies = [
        result["detection_latency_sec"]
        for result in invalid.values()
        if result["detection_latency_sec"] is not None
    ]
    enforcement_latencies = [
        result["enforcement_latency_sec"]
        for result in invalid.values()
        if result["enforcement_latency_sec"] is not None
    ]
    summary = {
        "schema_version": "telemetry-health-experiment-v1",
        "rule": load_rule(rule_path).model_dump(mode="json"),
        "baseline_no_rule": baseline,
        "topic_presence_rule": presence,
        "telemetry_health_valid": valid,
        "telemetry_health_invalid": invalid,
        "rule_removed": removed,
        "publisher_restart_recovery": {
            "failed_guard_is_latched": restart_failure,
            "fresh_guard_after_restart": recovery,
        },
        "insufficient_evidence": insufficient,
        "tampered_rule": tampered,
        "metrics": {
            "valid_case_count": valid_count,
            "invalid_case_count": invalid_count,
            "true_blocks": true_blocks,
            "false_blocks": false_blocks,
            "false_admits": false_admits,
            "true_block_rate": true_blocks / invalid_count,
            "false_block_rate": false_blocks / valid_count,
            "false_admit_rate": false_admits / invalid_count,
            "detection_latency_sec": {
                "minimum": min(detection_latencies),
                "maximum": max(detection_latencies),
                "mean": sum(detection_latencies) / len(detection_latencies),
            },
            "enforcement_latency_sec": {
                "minimum": min(enforcement_latencies),
                "maximum": max(enforcement_latencies),
                "mean": sum(enforcement_latencies) / len(enforcement_latencies),
            },
        },
    }
    target = out_dir / "experiment_summary.json"
    target.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--healthy-evidence", type=Path, required=True)
    parser.add_argument("--incident", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--domain-start", type=int, default=170)
    args = parser.parse_args()
    summary = run(
        rule_path=args.rule,
        evidence_path=args.healthy_evidence,
        incident_dir=args.incident,
        out_dir=args.out,
        domain_start=args.domain_start,
    )
    print(json.dumps(summary["metrics"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
