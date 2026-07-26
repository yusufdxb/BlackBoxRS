#!/usr/bin/env python3
"""Deterministic public demonstration of the telemetry-health guard.

This source-tree demo creates a fresh incident, evidence record, and trusted
rule through the same production adoption path used at runtime. It then
compares a graph-presence check with sustained telemetry-health enforcement.
The generated fixture is not genuine GO2 data.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.prevention.rules import PreflightCheck, make_rule, save_rule  # noqa: E402
from tests.telemetry_fixtures import (  # noqa: E402
    GRAPH_CONTEXT,
    TOPIC,
    build_telemetry_provenance_fixture,
)

PUBLISHER = REPO_ROOT / "scripts" / "telemetry_health_publisher.py"
GENUINE_SUMMARY = (
    REPO_ROOT
    / "examples"
    / "telemetry_health"
    / "genuine_go2_evidence_summary.json"
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _start_publisher(
    env: dict[str, str],
    *,
    duration_sec: float,
    silent_after_sec: float | None = None,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(PUBLISHER),
        "--topic",
        TOPIC,
        "--rate-hz",
        "18.75",
        "--duration-sec",
        str(duration_sec),
    ]
    if silent_after_sec is not None:
        command.extend(["--silent-after-sec", str(silent_after_sec)])
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _await_publisher_message(
    process: subprocess.Popen[str],
    env: dict[str, str],
) -> None:
    probe = f"""
import time
import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
rclpy.init(args=[])
node = rclpy.create_node("blackboxrs_public_demo_readiness")
seen = [False]
qos = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
subscription = node.create_subscription(
    PoseStamped,
    {TOPIC!r},
    lambda _message: seen.__setitem__(0, True),
    qos,
)
deadline = time.monotonic() + 4.0
while not seen[0] and time.monotonic() < deadline:
    rclpy.spin_once(node, timeout_sec=0.05)
node.destroy_subscription(subscription)
node.destroy_node()
rclpy.shutdown()
raise SystemExit(0 if seen[0] else 1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=6,
        check=False,
    )
    if process.poll() is not None:
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"publisher exited before readiness probe:\n{output}")
    if completed.returncode != 0:
        raise RuntimeError(f"readiness probe did not receive a message:\n{completed.stdout}")


def _guard_command(
    *,
    rule_path: Path,
    rule_fingerprint: str,
    result_path: Path,
    dependent_marker: Path,
    monitor_duration_sec: float,
) -> list[str]:
    dependent = (
        "from pathlib import Path;import time;"
        f"Path({str(dependent_marker)!r}).write_text('started',encoding='utf-8');"
        "time.sleep(30)"
    )
    return [
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
        str(monitor_duration_sec),
        "--context",
        GRAPH_CONTEXT,
        "--trusted-rule-fingerprint",
        rule_fingerprint,
        "--",
        sys.executable,
        "-c",
        dependent,
    ]


def _run_guard_case(
    *,
    name: str,
    env: dict[str, str],
    out_dir: Path,
    rule_path: Path,
    rule_fingerprint: str,
    publisher_duration_sec: float,
    monitor_duration_sec: float,
    silent_after_sec: float | None = None,
) -> tuple[dict[str, Any], subprocess.Popen[str]]:
    publisher = _start_publisher(
        env,
        duration_sec=publisher_duration_sec,
        silent_after_sec=silent_after_sec,
    )
    try:
        _await_publisher_message(publisher, env)
        result_path = out_dir / f"{name}_guard_result.json"
        marker = out_dir / f"{name}_dependent_started"
        completed = subprocess.run(
            _guard_command(
                rule_path=rule_path,
                rule_fingerprint=rule_fingerprint,
                result_path=result_path,
                dependent_marker=marker,
                monitor_duration_sec=monitor_duration_sec,
            ),
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=monitor_duration_sec + 8,
            check=False,
        )
        if not result_path.exists():
            raise RuntimeError(f"guard did not write {result_path}:\n{completed.stdout}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        dependent_pid = result.get("dependent_pid")
        result["cli_exit_code"] = completed.returncode
        result["dependent_marker_exists"] = marker.exists()
        result["dependent_pid_survives"] = bool(
            dependent_pid and Path(f"/proc/{dependent_pid}").exists()
        )
        result["publisher_alive_after_guard"] = publisher.poll() is None
        return result, publisher
    except BaseException:
        _stop_process_group(publisher)
        raise


def _run_presence_check(
    *,
    env: dict[str, str],
    rules_dir: Path,
) -> dict[str, Any]:
    rule = make_rule(
        PreflightCheck(
            name=f"topic present: {TOPIC}",
            kind="topic_present",
            params={"topic": TOPIC, "min_publishers": 1},
            severity_on_fail="block",
        ),
        rationale="Presence-only comparison for the deterministic public demo.",
    )
    save_rule(rule, rules_dir)
    completed = subprocess.run(
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
        stderr=subprocess.STDOUT,
        timeout=6,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "passed": completed.returncode == 0,
    }


def _print_result(label: str, result: dict[str, Any]) -> None:
    print(label)
    for key in (
        "status",
        "reason",
        "resolved_topic",
        "publisher_semantics",
        "dependent_started",
        "dependent_exit_code",
        "dependent_supervision_sec",
        "detection_latency_sec",
        "enforcement_latency_sec",
        "publisher_alive_after_guard",
        "dependent_pid_survives",
    ):
        print(f"  {key}: {result.get(key)}")


def run(out_dir: Path, domain_start: int) -> dict[str, Any]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    genuine = json.loads(GENUINE_SUMMARY.read_text(encoding="utf-8"))
    fixture = build_telemetry_provenance_fixture(out_dir / "provenance")
    assert fixture.rule.rule_fingerprint is not None

    print("[1/6] Genuine GO2 evidence reference")
    print("  This summary describes the private genuine bag. The live demo uses a fixture.")
    print(f"  pose_messages: {genuine['pose_topic']['message_count']}")
    print(f"  mean_rate_hz: {genuine['pose_topic']['mean_rate_hz']:.6f}")
    print(f"  combined_bag_sha256: {genuine['bag']['combined_identity_sha256']}")

    print("\n[2/6] Fresh deterministic provenance")
    print("  fixture_kind: generated public fixture, not genuine GO2 data")
    print(f"  incident_id: {fixture.incident.incident_id}")
    print(f"  trigger_id: {fixture.trigger.trigger_id}")
    print(
        "  evidence_fingerprint: "
        f"{fixture.rule.derivation['healthy_evidence_fingerprint']}"
    )
    print(f"  trusted_rule_fingerprint: {fixture.rule.rule_fingerprint}")
    print(f"  topic: {fixture.contract.topic}")
    print(f"  minimum_rate_hz: {fixture.contract.minimum_rate_hz}")
    print(f"  stale_timeout_sec: {fixture.contract.stale_timeout_sec}")

    silent_env = {
        **os.environ,
        "ROS_DOMAIN_ID": str(domain_start),
        "ROS_LOCALHOST_ONLY": "1",
    }
    print("\n[3/6] Publisher stays alive but becomes silent")
    silent, silent_publisher = _run_guard_case(
        name="publisher_present_silence",
        env=silent_env,
        out_dir=out_dir,
        rule_path=fixture.rule_path,
        rule_fingerprint=fixture.rule.rule_fingerprint,
        publisher_duration_sec=12,
        monitor_duration_sec=6,
        silent_after_sec=4,
    )
    try:
        presence = _run_presence_check(
            env=silent_env,
            rules_dir=out_dir / "presence_rules",
        )
        presence["publisher_alive"] = silent_publisher.poll() is None
    finally:
        _stop_process_group(silent_publisher)
    print(f"  presence_check_passed: {presence['passed']}")
    print(f"  presence_check_output: {presence['stdout']}")

    print("\n[4/6] Hardened guard result")
    _print_result("  publisher-present silence:", silent)

    healthy_env = {
        **os.environ,
        "ROS_DOMAIN_ID": str(domain_start + 1),
        "ROS_LOCALHOST_ONLY": "1",
    }
    print("\n[5/6] Nearby healthy 18.75 Hz condition")
    healthy, healthy_publisher = _run_guard_case(
        name="nearby_healthy",
        env=healthy_env,
        out_dir=out_dir,
        rule_path=fixture.rule_path,
        rule_fingerprint=fixture.rule.rule_fingerprint,
        publisher_duration_sec=10,
        monitor_duration_sec=1,
    )
    _stop_process_group(healthy_publisher)
    _print_result("  nearby healthy:", healthy)

    checks = {
        "presence_passed_while_publisher_silent": (
            presence["passed"] and presence["publisher_alive"]
        ),
        "silent_stream_qualified_before_failure": silent["dependent_started"] is True,
        "silent_stream_blocked": (
            silent["status"] == "blocked" and silent["reason"] == "stale"
        ),
        "silent_dependent_terminated": (
            silent["dependent_exit_code"] is not None
            and silent["dependent_pid_survives"] is False
        ),
        "silent_publisher_remained_alive": (
            silent["publisher_alive_after_guard"] is True
        ),
        "nearby_healthy_passed": (
            healthy["status"] == "passed"
            and healthy["dependent_started"] is True
            and healthy["dependent_pid_survives"] is False
        ),
        "trusted_provenance_verified": (
            silent["rule_fingerprint"] == fixture.rule.rule_fingerprint
            and silent["source_incident_id"] == fixture.incident.incident_id
        ),
    }
    summary = {
        "schema_version": "runtime-telemetry-health-public-demo-v1",
        "fixture_is_genuine_go2_data": False,
        "genuine_evidence_reference": genuine,
        "generated_provenance": {
            "incident_id": fixture.incident.incident_id,
            "trigger_id": fixture.trigger.trigger_id,
            "source_event_reference": fixture.trigger.source_event_ref,
            "evidence_fingerprint": fixture.rule.derivation[
                "healthy_evidence_fingerprint"
            ],
            "trusted_rule_fingerprint": fixture.rule.rule_fingerprint,
            "topic": fixture.contract.topic,
            "expected_type": fixture.contract.expected_type,
            "selected_thresholds": fixture.rule.derivation["selected_thresholds"],
        },
        "topic_presence_comparison": presence,
        "publisher_present_silence": silent,
        "nearby_healthy": healthy,
        "checks": checks,
        "passed": all(checks.values()),
    }
    _atomic_json(out_dir / "demo_summary.json", summary)

    print("\n[6/6] Bounded result")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}: {name}")
    print("  aggregate-topic arrival liveness plus monotonic header progress")
    print("  no payload-semantic freshness or specific-producer health claim")
    print("  linked clean-commit attack results:")
    for name, result in genuine["critical_attack_results"].items():
        print(f"    {name}: {result}")
    print(f"  output: {out_dir / 'demo_summary.json'}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--domain-start", type=int, default=180)
    args = parser.parse_args()
    if not 0 <= args.domain_start <= 231:
        parser.error("--domain-start must leave room for two ROS domains")
    try:
        summary = run(args.out.resolve(), args.domain_start)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
