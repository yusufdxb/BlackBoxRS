"""Process-level adversarial tests for the telemetry guard's Linux owner.

These tests intentionally use real subprocesses, signals, forks, process
groups, and sessions.  The only fixture boundary is the dependent executable;
the production process supervisor is always run in a separate process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from blackboxrs.prevention.process_supervisor import (
    DEPENDENT_LAUNCH_FAILED,
    PROCESS_MODEL_VIOLATION,
)


_WAIT_TIMEOUT_SEC = 6.0

_DEPENDENT_SOURCE = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

mode = sys.argv[1]
record_path = Path(sys.argv[2])

def record(role):
    payload = {
        "role": role,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "pgrp": os.getpgrp(),
        "session": os.getsid(0),
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)

def sleep_forever():
    while True:
        time.sleep(1)

if mode == "short":
    record("direct")
    raise SystemExit(0)
if mode == "nonzero":
    record("direct")
    raise SystemExit(23)
if mode == "sleep":
    record("direct")
    sleep_forever()
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record("direct")
    sleep_forever()
if mode == "fork_one":
    child = os.fork()
    if child == 0:
        record("child")
        sleep_forever()
    record("direct")
    sleep_forever()
if mode == "fork_many":
    for index in range(3):
        child = os.fork()
        if child == 0:
            record(f"child_{index}")
            sleep_forever()
    record("direct")
    sleep_forever()
if mode == "new_pgrp":
    os.setpgid(0, 0)
    record("direct")
    sleep_forever()
if mode == "setsid":
    os.setsid()
    record("direct")
    sleep_forever()
if mode == "double_fork":
    first = os.fork()
    if first > 0:
        record("direct")
        time.sleep(0.2)
        os._exit(0)
    second = os.fork()
    if second > 0:
        record("first_child")
        os._exit(0)
    record("grandchild")
    sleep_forever()
if mode == "daemonize":
    first = os.fork()
    if first > 0:
        record("direct")
        time.sleep(0.2)
        os._exit(0)
    record("daemon_candidate")
    os.setsid()
    second = os.fork()
    if second > 0:
        os._exit(0)
    record("daemon")
    sleep_forever()
raise SystemExit(f"unknown mode: {mode}")
"""

_GUARD_OWNER_SOURCE = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from blackboxrs.prevention.telemetry_guard import _set_parent_death_signal

command = json.loads(os.environ["BLACKBOXRS_TEST_COMMAND"])
state_path = Path(os.environ["BLACKBOXRS_TEST_STATE"])
result_path = Path(os.environ["BLACKBOXRS_TEST_RESULT"])
mode = os.environ["BLACKBOXRS_TEST_OWNER_MODE"]
parent_pid = os.getpid()
child_env = os.environ.copy()
child_env["BLACKBOXRS_OWNER_PID"] = str(parent_pid)
wrapper = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "blackboxrs.prevention.process_supervisor",
        "--",
        *command,
    ],
    start_new_session=True,
    preexec_fn=lambda: _set_parent_death_signal(parent_pid),
    env=child_env,
)
state_path.write_text(
    json.dumps({"guard_pid": parent_pid, "wrapper_pid": wrapper.pid}),
    encoding="utf-8",
)

if mode == "wait":
    wrapper_code = wrapper.wait()
    time.sleep(0.1)
    result_path.write_text(
        json.dumps(
            {
                "status": "passed" if wrapper_code == 0 else "dependent_failed",
                "dependent_exit_code": wrapper_code,
            }
        ),
        encoding="utf-8",
    )
    raise SystemExit(0 if wrapper_code == 0 else 1)
if mode == "delayed_result":
    wrapper_code = wrapper.wait()
    time.sleep(0.2)
    result_path.write_text(
        json.dumps(
            {"status": "passed", "dependent_exit_code": wrapper_code}
        ),
        encoding="utf-8",
    )
    raise SystemExit(0)
if mode == "exception":
    time.sleep(0.2)
    raise RuntimeError("simulated guard exception after dependent launch")
if mode == "result_write_failure":
    time.sleep(0.2)
    result_path.mkdir()
    result_path.write_text("cannot replace a directory", encoding="utf-8")
if mode == "hold":
    while True:
        time.sleep(1)
raise SystemExit(f"unknown owner mode: {mode}")
"""


@pytest.fixture
def process_fixture(tmp_path: Path) -> dict[str, Path]:
    dependent = tmp_path / "dependent.py"
    owner = tmp_path / "owner.py"
    dependent.write_text(_DEPENDENT_SOURCE, encoding="utf-8")
    owner.write_text(_GUARD_OWNER_SOURCE, encoding="utf-8")
    return {
        "dependent": dependent,
        "owner": owner,
        "records": tmp_path / "pids.jsonl",
        "state": tmp_path / "owner-state.json",
        "result": tmp_path / "guard-result.json",
    }


def _command(paths: dict[str, Path], mode: str) -> list[str]:
    return [sys.executable, str(paths["dependent"]), mode, str(paths["records"])]


def _start_supervisor(
    paths: dict[str, Path], mode: str
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["BLACKBOXRS_OWNER_PID"] = str(os.getpid())
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "blackboxrs.prevention.process_supervisor",
            "--",
            *_command(paths, mode),
        ],
        start_new_session=True,
        env=env,
    )


def _start_guard_owner(
    paths: dict[str, Path], dependent_mode: str, owner_mode: str
) -> tuple[subprocess.Popen[bytes], dict[str, int]]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(Path.cwd()), existing_pythonpath)
        if part
    )
    env.update(
        {
            "BLACKBOXRS_TEST_COMMAND": json.dumps(
                _command(paths, dependent_mode)
            ),
            "BLACKBOXRS_TEST_STATE": str(paths["state"]),
            "BLACKBOXRS_TEST_RESULT": str(paths["result"]),
            "BLACKBOXRS_TEST_OWNER_MODE": owner_mode,
        }
    )
    owner = subprocess.Popen([sys.executable, str(paths["owner"])], env=env)
    _wait_until(lambda: paths["state"].exists(), "guard owner state")
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    return owner, state


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            records.append(json.loads(raw))
    return records


def _wait_for_records(path: Path, count: int) -> list[dict[str, Any]]:
    _wait_until(lambda: len(_records(path)) >= count, f"{count} PID records")
    return _records(path)


def _wait_until(predicate: Any, description: str) -> None:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {description}")


def _pid_is_live(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    close = raw.rfind(")")
    fields = raw[close + 2 :].split()
    return bool(fields and fields[0] != "Z")


def _assert_all_gone(pids: set[int]) -> None:
    _wait_until(
        lambda: all(not _pid_is_live(pid) for pid in pids),
        f"processes to exit: {sorted(pids)}",
    )


def _live_command_pids(marker: Path) -> set[int]:
    encoded_marker = os.fsencode(marker)
    found: set[int] = set()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        pid = int(proc_dir.name)
        if encoded_marker in cmdline and _pid_is_live(pid):
            found.add(pid)
    return found


def _assert_no_fixture_commands(paths: dict[str, Path]) -> None:
    _wait_until(
        lambda: not _live_command_pids(paths["dependent"]),
        f"commands for {paths['dependent']} to exit",
    )


def _terminate_guard_and_assert_cleanup(
    paths: dict[str, Path],
    *,
    signum: int,
    dependent_mode: str,
    record_count: int,
) -> tuple[int, list[dict[str, Any]], dict[str, int]]:
    owner, state = _start_guard_owner(paths, dependent_mode, "hold")
    records = _wait_for_records(paths["records"], record_count)
    os.kill(owner.pid, signum)
    owner_code = owner.wait(timeout=_WAIT_TIMEOUT_SEC)
    pids = {int(state["wrapper_pid"])} | {
        int(record["pid"]) for record in records
    }
    _assert_all_gone(pids)
    _assert_no_fixture_commands(paths)
    return owner_code, records, state


def test_01_normal_guard_completion_persists_success(
    process_fixture: dict[str, Path],
) -> None:
    owner, state = _start_guard_owner(process_fixture, "short", "wait")
    assert owner.wait(timeout=_WAIT_TIMEOUT_SEC) == 0
    result = json.loads(process_fixture["result"].read_text(encoding="utf-8"))
    assert result == {"status": "passed", "dependent_exit_code": 0}
    records = _wait_for_records(process_fixture["records"], 1)
    _assert_all_gone(
        {int(state["wrapper_pid"]), *(int(record["pid"]) for record in records)}
    )
    _assert_no_fixture_commands(process_fixture)


def test_02_guard_sigterm_cleans_dependent(
    process_fixture: dict[str, Path],
) -> None:
    owner_code, records, state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGTERM,
        dependent_mode="sleep",
        record_count=1,
    )
    assert owner_code == -signal.SIGTERM
    assert records[0]["pgrp"] == state["wrapper_pid"]


def test_03_guard_sigint_cleans_dependent(
    process_fixture: dict[str, Path],
) -> None:
    owner_code, records, state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGINT,
        dependent_mode="sleep",
        record_count=1,
    )
    assert owner_code == -signal.SIGINT
    assert records[0]["session"] == state["wrapper_pid"]


def test_04_guard_sigkill_cleans_dependent(
    process_fixture: dict[str, Path],
) -> None:
    owner_code, _records_seen, _state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGKILL,
        dependent_mode="sleep",
        record_count=1,
    )
    assert owner_code == -signal.SIGKILL


def test_05_guard_exception_after_launch_cleans_dependent(
    process_fixture: dict[str, Path],
) -> None:
    owner, state = _start_guard_owner(process_fixture, "sleep", "exception")
    records = _wait_for_records(process_fixture["records"], 1)
    assert owner.wait(timeout=_WAIT_TIMEOUT_SEC) != 0
    _assert_all_gone(
        {int(state["wrapper_pid"]), *(int(record["pid"]) for record in records)}
    )
    _assert_no_fixture_commands(process_fixture)
    assert not process_fixture["result"].exists()


def test_06_dependent_natural_exit_is_propagated(
    process_fixture: dict[str, Path],
) -> None:
    wrapper = _start_supervisor(process_fixture, "nonzero")
    records = _wait_for_records(process_fixture["records"], 1)
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == 23
    _assert_all_gone({int(record["pid"]) for record in records})
    _assert_no_fixture_commands(process_fixture)


def test_07_dependent_ignoring_sigterm_is_sigkilled(
    process_fixture: dict[str, Path],
) -> None:
    owner_code, _records_seen, _state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGTERM,
        dependent_mode="ignore_term",
        record_count=1,
    )
    assert owner_code == -signal.SIGTERM


def test_08_one_forked_child_is_cleaned_with_foreground_process(
    process_fixture: dict[str, Path],
) -> None:
    _owner_code, records, state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGTERM,
        dependent_mode="fork_one",
        record_count=2,
    )
    assert {record["role"] for record in records} == {"direct", "child"}
    assert {record["pgrp"] for record in records} == {state["wrapper_pid"]}


def test_09_multiple_descendants_are_cleaned(
    process_fixture: dict[str, Path],
) -> None:
    _owner_code, records, state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGKILL,
        dependent_mode="fork_many",
        record_count=4,
    )
    assert {record["role"] for record in records} == {
        "direct",
        "child_0",
        "child_1",
        "child_2",
    }
    assert {record["session"] for record in records} == {state["wrapper_pid"]}


def test_10_new_process_group_is_rejected_and_cleaned(
    process_fixture: dict[str, Path],
) -> None:
    wrapper = _start_supervisor(process_fixture, "new_pgrp")
    records = _wait_for_records(process_fixture["records"], 1)
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == PROCESS_MODEL_VIOLATION
    assert records[0]["pgrp"] != wrapper.pid
    _assert_all_gone({int(record["pid"]) for record in records})
    _assert_no_fixture_commands(process_fixture)


def test_11_new_session_is_rejected_and_cleaned(
    process_fixture: dict[str, Path],
) -> None:
    wrapper = _start_supervisor(process_fixture, "setsid")
    records = _wait_for_records(process_fixture["records"], 1)
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == PROCESS_MODEL_VIOLATION
    assert records[0]["session"] != wrapper.pid
    _assert_all_gone({int(record["pid"]) for record in records})
    _assert_no_fixture_commands(process_fixture)


def test_12_double_fork_background_escape_is_rejected(
    process_fixture: dict[str, Path],
) -> None:
    wrapper = _start_supervisor(process_fixture, "double_fork")
    records = _wait_for_records(process_fixture["records"], 3)
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == PROCESS_MODEL_VIOLATION
    _assert_all_gone({int(record["pid"]) for record in records})
    _assert_no_fixture_commands(process_fixture)


def test_13_daemonization_is_rejected_and_cleaned(
    process_fixture: dict[str, Path],
) -> None:
    wrapper = _start_supervisor(process_fixture, "daemonize")
    records = _wait_for_records(process_fixture["records"], 2)
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == PROCESS_MODEL_VIOLATION
    _assert_all_gone({int(record["pid"]) for record in records})
    _assert_no_fixture_commands(process_fixture)


def test_14_dependent_launch_failure_has_reserved_exit_code(
    process_fixture: dict[str, Path],
) -> None:
    env = os.environ.copy()
    env["BLACKBOXRS_OWNER_PID"] = str(os.getpid())
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "blackboxrs.prevention.process_supervisor",
            "--",
            "/definitely/missing/blackboxrs-dependent",
        ],
        start_new_session=True,
        env=env,
    )
    assert wrapper.wait(timeout=_WAIT_TIMEOUT_SEC) == DEPENDENT_LAUNCH_FAILED


def test_15_dependent_exit_precedes_result_persistence_without_leak(
    process_fixture: dict[str, Path],
) -> None:
    owner, state = _start_guard_owner(
        process_fixture, "short", "delayed_result"
    )
    records = _wait_for_records(process_fixture["records"], 1)
    assert owner.wait(timeout=_WAIT_TIMEOUT_SEC) == 0
    result = json.loads(process_fixture["result"].read_text(encoding="utf-8"))
    assert result["dependent_exit_code"] == 0
    _assert_all_gone(
        {int(state["wrapper_pid"]), *(int(record["pid"]) for record in records)}
    )
    _assert_no_fixture_commands(process_fixture)


def test_16_result_write_failure_cleans_dependent(
    process_fixture: dict[str, Path],
) -> None:
    owner, state = _start_guard_owner(
        process_fixture, "sleep", "result_write_failure"
    )
    records = _wait_for_records(process_fixture["records"], 1)
    assert owner.wait(timeout=_WAIT_TIMEOUT_SEC) != 0
    assert process_fixture["result"].is_dir()
    _assert_all_gone(
        {int(state["wrapper_pid"]), *(int(record["pid"]) for record in records)}
    )
    _assert_no_fixture_commands(process_fixture)


def test_17_oom_like_guard_sigkill_cleans_descendant_tree(
    process_fixture: dict[str, Path],
) -> None:
    owner_code, records, _state = _terminate_guard_and_assert_cleanup(
        process_fixture,
        signum=signal.SIGKILL,
        dependent_mode="fork_many",
        record_count=4,
    )
    assert owner_code == -signal.SIGKILL
    assert len({record["pid"] for record in records}) == 4
