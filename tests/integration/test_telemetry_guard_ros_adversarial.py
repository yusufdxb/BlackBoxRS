"""Process-level ROS graph attacks against the telemetry-health guard."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

from tests.telemetry_fixtures import (
    GRAPH_CONTEXT,
    TOPIC,
    TelemetryProvenanceFixture,
    build_telemetry_provenance_fixture,
)


pytest.importorskip("rclpy", reason="process-level ROS tests require rclpy")
pytest.importorskip(
    "geometry_msgs.msg", reason="process-level ROS tests require geometry_msgs"
)
pytest.importorskip("std_msgs.msg", reason="wrong-type ROS test requires std_msgs")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHER = _REPO_ROOT / "scripts" / "telemetry_health_publisher.py"


@dataclass(frozen=True)
class _GuardOutcome:
    process: subprocess.CompletedProcess[str]
    result: dict[str, object] | None
    dependent_marker: Path


@pytest.fixture(scope="module")
def telemetry_provenance(
    tmp_path_factory: pytest.TempPathFactory,
) -> TelemetryProvenanceFixture:
    return build_telemetry_provenance_fixture(
        tmp_path_factory.mktemp("telemetry_ros_provenance")
    )


@pytest.fixture
def ros_env(request: pytest.FixtureRequest) -> dict[str, str]:
    """Give every test an isolated, localhost-only DDS domain."""
    domain_seed = f"{os.getpid()}:{request.node.nodeid}".encode()
    domain_id = 20 + zlib.crc32(domain_seed) % 180
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(domain_id)
    env["ROS_LOCALHOST_ONLY"] = "1"
    return env


@pytest.fixture
def publishers() -> list[subprocess.Popen[str]]:
    running: list[subprocess.Popen[str]] = []
    try:
        yield running
    finally:
        for process in running:
            _stop_process_group(process)


def _start_pose_publisher(
    publishers: list[subprocess.Popen[str]],
    env: dict[str, str],
    *,
    topic: str = TOPIC,
    duration_sec: float = 6.0,
    silent_after_sec: float | None = None,
    best_effort: bool = False,
    depth: int = 1,
    transient_local: bool = False,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(_PUBLISHER),
        "--topic",
        topic,
        "--rate-hz",
        "20",
        "--duration-sec",
        str(duration_sec),
        "--depth",
        str(depth),
    ]
    if silent_after_sec is not None:
        command.extend(["--silent-after-sec", str(silent_after_sec)])
    if best_effort:
        command.append("--best-effort")
    if transient_local:
        command.append("--transient-local")
    process = subprocess.Popen(
        command,
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    publishers.append(process)
    return process


def _start_string_publisher(
    publishers: list[subprocess.Popen[str]],
    env: dict[str, str],
) -> subprocess.Popen[str]:
    source = (
        "import time,rclpy;"
        "from std_msgs.msg import String;"
        "rclpy.init(args=[]);"
        "node=rclpy.create_node('telemetry_wrong_type_publisher');"
        f"pub=node.create_publisher(String,{TOPIC!r},1);"
        "start=time.monotonic();"
        "\nwhile time.monotonic()-start < 6.0:"
        "\n msg=String();msg.data='spoof';pub.publish(msg);"
        "rclpy.spin_once(node,timeout_sec=0.001);time.sleep(0.05)"
        "\nnode.destroy_node();rclpy.shutdown()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    publishers.append(process)
    return process


def _start_marker_controlled_publisher(
    publishers: list[subprocess.Popen[str]],
    env: dict[str, str],
    *,
    marker: Path,
    silent_after_marker_sec: float,
) -> subprocess.Popen[str]:
    """Publish until a fixed monotonic offset after dependent launch."""
    source = f"""
import time
from pathlib import Path

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

marker = Path({str(marker)!r})
rclpy.init(args=[])
node = rclpy.create_node("telemetry_marker_controlled_publisher")
qos = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
publisher = node.create_publisher(PoseStamped, {TOPIC!r}, qos)
started = time.monotonic()
next_publish = started
marker_seen = None
while time.monotonic() - started < 8.0:
    now = time.monotonic()
    rclpy.spin_once(node, timeout_sec=0.001)
    if marker_seen is None and marker.exists():
        marker_seen = now
    silent = (
        marker_seen is not None
        and now - marker_seen >= {silent_after_marker_sec!r}
    )
    if not silent and now >= next_publish:
        message = PoseStamped()
        message.header.stamp = node.get_clock().now().to_msg()
        message.header.frame_id = "odom"
        message.pose.orientation.w = 1.0
        publisher.publish(message)
        next_publish += 0.05
node.destroy_node()
rclpy.shutdown()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    publishers.append(process)
    return process


def _run_guard(
    tmp_path: Path,
    fixture: TelemetryProvenanceFixture,
    env: dict[str, str],
    *,
    context: str = GRAPH_CONTEXT,
    monitor_duration_sec: float | None = 0.35,
    extra_dependent_args: Sequence[str] = (),
    dependent_command: Sequence[str] | None = None,
    preseed_success: bool = False,
) -> _GuardOutcome:
    result_path = tmp_path / "guard-result.json"
    if preseed_success:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            '{"run_id":"old-success","status":"passed","sentinel":"stale"}\n',
            encoding="utf-8",
        )
    marker = tmp_path / "dependent-started"
    dependent_source = (
        "from pathlib import Path;import time;"
        f"Path({str(marker)!r}).write_text('started',encoding='utf-8');"
        "time.sleep(30)"
    )
    command = [
        sys.executable,
        "-m",
        "blackboxrs",
        "prevention",
        "guard",
        "--rule",
        str(fixture.rule_path),
        "--result",
        str(result_path),
        "--context-label",
        context,
        "--trusted-rule-fingerprint",
        str(fixture.rule.rule_fingerprint),
    ]
    if monitor_duration_sec is not None:
        command.extend(["--monitor-duration", str(monitor_duration_sec)])
    command.extend(
        [
        "--",
        *(
            dependent_command
            if dependent_command is not None
            else (sys.executable, "-c", dependent_source)
        ),
        *extra_dependent_args,
        ]
    )
    process = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=12,
        check=False,
    )
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.exists()
        else None
    )
    if result_path.parent.exists():
        assert not list(
            result_path.parent.glob(f".{result_path.name}.*.tmp")
        )
    return _GuardOutcome(process=process, result=result, dependent_marker=marker)


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


def _await_publisher_start(
    process: subprocess.Popen[str],
    env: dict[str, str],
    *,
    topic: str = TOPIC,
    message_type: str = "pose",
    best_effort: bool = False,
) -> None:
    """Wait until a separate DDS subscriber receives one real message."""
    if message_type == "pose":
        message_import = "from geometry_msgs.msg import PoseStamped as Message"
    elif message_type == "string":
        message_import = "from std_msgs.msg import String as Message"
    else:
        raise ValueError(message_type)
    reliability = "BEST_EFFORT" if best_effort else "RELIABLE"
    probe = f"""
import time
import rclpy
{message_import}
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
rclpy.init(args=[])
node = rclpy.create_node("telemetry_publisher_readiness_probe")
seen = [False]
qos = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.{reliability},
    durability=DurabilityPolicy.VOLATILE,
)
subscription = node.create_subscription(
    Message,
    {topic!r},
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
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=6.0,
        check=False,
    )
    assert process.poll() is None, process.stdout.read() if process.stdout else ""
    assert completed.returncode == 0, completed.stdout


def _assert_blocked_without_dependent(outcome: _GuardOutcome) -> dict[str, object]:
    assert outcome.process.returncode == 1, outcome.process.stdout
    assert outcome.result is not None, outcome.process.stdout
    assert outcome.result["status"] == "blocked"
    assert outcome.result["dependent_started"] is False
    assert not outcome.dependent_marker.exists()
    return outcome.result


def test_global_remapping_cannot_redirect_contract_subscription(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env, topic="/spoof_pose")
    _await_publisher_start(publisher, ros_env, topic="/spoof_pose")

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        extra_dependent_args=(
            "--ros-args",
            "-r",
            f"{TOPIC}:=/spoof_pose",
        ),
    )

    result = _assert_blocked_without_dependent(outcome)
    assert result["reason"] == "no_publisher"
    assert result["resolved_topic"] == TOPIC
    structural = result["structural"]
    assert isinstance(structural, dict)
    assert structural["resolved_topic"] == TOPIC
    assert structural["publisher_count"] == 0


def test_correct_topic_type_compatible_qos_and_declared_label_starts_dependent(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(tmp_path, telemetry_provenance, ros_env)

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["dependent_started"] is True
    assert outcome.result["resolved_topic"] == TOPIC
    assert outcome.result["publisher_semantics"] == "aggregate_topic"
    assert outcome.dependent_marker.exists()


def test_same_basename_in_wrong_namespace_does_not_qualify(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(
        publishers, ros_env, topic="/wrong_namespace/robot_pose"
    )
    _await_publisher_start(
        publisher,
        ros_env,
        topic="/wrong_namespace/robot_pose",
    )

    result = _assert_blocked_without_dependent(
        _run_guard(tmp_path, telemetry_provenance, ros_env)
    )

    assert result["reason"] == "no_publisher"
    assert result["resolved_topic"] == TOPIC


def test_correct_traffic_under_mismatched_declared_context_label_is_refused(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        context="go2_simulation_wrong_context",
        preseed_success=True,
    )

    assert outcome.process.returncode == 1
    assert (
        "Declared context label does not match the telemetry contract"
        in outcome.process.stdout
    )
    assert outcome.result is not None
    assert outcome.result["status"] == "refused"
    assert outcome.result["run_id"] != "old-success"
    assert not outcome.dependent_marker.exists()


def test_same_topic_with_wrong_type_is_blocked(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_string_publisher(publishers, ros_env)
    _await_publisher_start(
        publisher,
        ros_env,
        message_type="string",
    )

    result = _assert_blocked_without_dependent(
        _run_guard(tmp_path, telemetry_provenance, ros_env)
    )

    assert result["reason"] == "wrong_type_or_incompatible_qos"
    assert result["structural"]["publisher_count"] >= 1
    assert "std_msgs/msg/String" in result["structural"]["observed_types"]


def test_same_topic_and_type_with_incompatible_qos_is_blocked(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env, best_effort=True)
    _await_publisher_start(publisher, ros_env, best_effort=True)

    result = _assert_blocked_without_dependent(
        _run_guard(tmp_path, telemetry_provenance, ros_env)
    )

    assert result["reason"] == "wrong_type_or_incompatible_qos"
    assert result["structural"]["publisher_count"] >= 1
    assert result["structural"]["compatible_publisher_count"] == 0


@pytest.mark.parametrize(
    ("depth", "transient_local"),
    [(10, False), (1, True)],
)
def test_compatible_qos_depth_or_durability_difference_is_admitted(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
    depth: int,
    transient_local: bool,
) -> None:
    publisher = _start_pose_publisher(
        publishers,
        ros_env,
        depth=depth,
        transient_local=transient_local,
    )
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(tmp_path, telemetry_provenance, ros_env)

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["structural"]["compatible_publisher_count"] >= 1


def test_one_healthy_and_one_stale_publisher_passes_on_aggregate_traffic(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    """Aggregate health passes without claiming the stale producer is healthy."""
    healthy = _start_pose_publisher(publishers, ros_env)
    stale = _start_pose_publisher(publishers, ros_env, silent_after_sec=0.7)
    _await_publisher_start(healthy, ros_env)
    assert stale.poll() is None

    outcome = _run_guard(tmp_path, telemetry_provenance, ros_env)

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["publisher_semantics"] == "aggregate_topic"
    assert outcome.result["structural"]["publisher_count"] == 2
    assert outcome.dependent_marker.exists()


def test_all_publishers_stale_blocks_aggregate_topic(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    first = _start_pose_publisher(publishers, ros_env, silent_after_sec=0.7)
    second = _start_pose_publisher(publishers, ros_env, silent_after_sec=0.7)
    _await_publisher_start(first, ros_env)
    assert second.poll() is None

    result = _assert_blocked_without_dependent(
        _run_guard(tmp_path, telemetry_provenance, ros_env)
    )

    assert result["reason"] in {"stale", "frozen_timestamp"}
    assert result["publisher_semantics"] == "aggregate_topic"
    assert result["structural"]["publisher_count"] == 2


def test_one_publisher_disappears_but_aggregate_traffic_remains_healthy(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    healthy = _start_pose_publisher(publishers, ros_env)
    disappearing = _start_pose_publisher(publishers, ros_env, duration_sec=0.8)
    _await_publisher_start(healthy, ros_env)
    assert disappearing.poll() is None

    outcome = _run_guard(tmp_path, telemetry_provenance, ros_env)

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["publisher_semantics"] == "aggregate_topic"
    assert outcome.result["structural"]["publisher_count"] == 1
    assert outcome.dependent_marker.exists()


def test_monitor_duration_begins_after_two_second_qualification(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=1.0,
    )

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    result = outcome.result
    assert result["dependent_launch_offset_sec"] >= 1.9
    assert result["dependent_supervision_sec"] >= 1.0
    assert result["guard_runtime_sec"] >= 2.9
    for field in (
        "guard_started_at",
        "qualification_started_at",
        "qualification_completed_at",
        "dependent_launched_at",
        "supervision_started_at",
        "supervision_ended_at",
        "dependent_exited_at",
        "enforcement_at",
        "completed_at",
    ):
        assert result[field] is not None


def test_zero_monitor_duration_starts_then_immediately_stops_dependent(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=0.0,
    )

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["dependent_started"] is True
    assert outcome.result["dependent_supervision_sec"] >= 0.0


def test_short_dependent_natural_exit_is_reported(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)
    marker = tmp_path / "dependent-started"
    source = (
        "from pathlib import Path;import time;"
        f"Path({str(marker)!r}).write_text('started',encoding='utf-8');"
        "time.sleep(0.1)"
    )

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=5.0,
        dependent_command=(sys.executable, "-c", source),
    )

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["dependent_exit_code"] == 0
    assert outcome.result["enforcement_at"] is None


def test_nonzero_dependent_exit_is_reported(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)
    marker = tmp_path / "dependent-started"
    source = (
        "from pathlib import Path;"
        f"Path({str(marker)!r}).write_text('started',encoding='utf-8');"
        "raise SystemExit(23)"
    )

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=5.0,
        dependent_command=(sys.executable, "-c", source),
    )

    assert outcome.process.returncode == 1
    assert outcome.result is not None
    assert outcome.result["status"] == "dependent_failed"
    assert outcome.result["reason"] == "dependent_exit"
    assert outcome.result["dependent_exit_code"] == 23


def test_preseeded_pass_is_replaced_by_dependent_launch_failure(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=1.0,
        dependent_command=(str(tmp_path / "does-not-exist"),),
        preseed_success=True,
    )

    assert outcome.process.returncode == 1
    assert outcome.result is not None
    assert outcome.result["status"] == "dependent_failed"
    assert outcome.result["dependent_exit_code"] == 127
    assert outcome.result["run_id"] != "old-success"


def test_repeated_launches_with_active_dds_threads_do_not_hang(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(
        publishers,
        ros_env,
        duration_sec=15.0,
    )
    _await_publisher_start(publisher, ros_env)

    run_ids: set[str] = set()
    for index in range(3):
        outcome = _run_guard(
            tmp_path / f"launch-{index}",
            telemetry_provenance,
            ros_env,
            monitor_duration_sec=0.05,
        )
        assert outcome.process.returncode == 0, outcome.process.stdout
        assert outcome.result is not None
        assert outcome.result["status"] == "passed"
        assert outcome.result["dependent_started"] is True
        run_ids.add(str(outcome.result["run_id"]))

    assert len(run_ids) == 3


def test_no_monitor_duration_waits_for_natural_exit(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
) -> None:
    publisher = _start_pose_publisher(publishers, ros_env)
    _await_publisher_start(publisher, ros_env)
    marker = tmp_path / "dependent-started"
    source = (
        "from pathlib import Path;import time;"
        f"Path({str(marker)!r}).write_text('started',encoding='utf-8');"
        "time.sleep(0.25)"
    )

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=None,
        dependent_command=(sys.executable, "-c", source),
    )

    assert outcome.process.returncode == 0, outcome.process.stdout
    assert outcome.result is not None
    assert outcome.result["status"] == "passed"
    assert outcome.result["dependent_supervision_sec"] >= 0.2


@pytest.mark.parametrize(
    ("silent_after_marker_sec", "expected_status"),
    [(0.25, "blocked"), (0.95, "passed")],
)
def test_runtime_failure_boundary_uses_supervision_deadline(
    tmp_path: Path,
    telemetry_provenance: TelemetryProvenanceFixture,
    ros_env: dict[str, str],
    publishers: list[subprocess.Popen[str]],
    silent_after_marker_sec: float,
    expected_status: str,
) -> None:
    marker = tmp_path / "dependent-started"
    publisher = _start_marker_controlled_publisher(
        publishers,
        ros_env,
        marker=marker,
        silent_after_marker_sec=silent_after_marker_sec,
    )
    _await_publisher_start(publisher, ros_env)

    outcome = _run_guard(
        tmp_path,
        telemetry_provenance,
        ros_env,
        monitor_duration_sec=1.0,
    )

    assert outcome.result is not None, outcome.process.stdout
    assert outcome.result["status"] == expected_status
    if expected_status == "blocked":
        assert outcome.process.returncode == 1
        assert outcome.result["reason"] in {"stale", "frozen_timestamp"}
        assert outcome.result["dependent_supervision_sec"] >= 0.3
        assert outcome.result["dependent_supervision_sec"] < 1.0
    else:
        assert outcome.process.returncode == 0, outcome.process.stdout
        assert outcome.result["dependent_supervision_sec"] >= 1.0
