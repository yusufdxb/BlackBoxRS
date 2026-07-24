"""Small process-group owner for the telemetry guard.

The guard launches this module as the direct child.  The wrapper owns the
dependent process group, becomes a Linux child subreaper, and cleans up the
group when the guard disappears.  This intentionally supports one direct
dependent process group, not arbitrary services that escape their group or
cgroup.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import time
from typing import Any


_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1


def _prctl(option: int, value: int) -> None:
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        raise RuntimeError("libc is required for process lifetime ownership")
    libc = ctypes.CDLL(libc_name, use_errno=True)
    libc.prctl(option, value, 0, 0, 0)
    errno = ctypes.get_errno()
    if errno:
        raise OSError(errno, os.strerror(errno))


def _descendants(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = open(f"/proc/{entry}/stat", encoding="utf-8").read()
            close = raw.rfind(")")
            fields = raw[close + 2 :].split()
            parents[int(entry)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    found: set[int] = set()
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid not in found:
                found.add(pid)
                frontier.append(pid)
    return found


def _kill_owned_group(root_pid: int) -> None:
    descendants = _descendants(root_pid)
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not descendants and _process_gone(root_pid):
            return
        time.sleep(0.02)
        descendants = _descendants(root_pid)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in descendants | {root_pid}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def run(command: list[str]) -> int:
    if not command:
        raise ValueError("dependent command is required")
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    child: subprocess.Popen[Any] | None = None
    terminating = False

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal terminating
        if terminating:
            return
        terminating = True
        if child is not None and child.poll() is None:
            _kill_owned_group(child.pid)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    signal.signal(signal.SIGINT, terminate)
    owner_pid = int(os.environ.get("BLACKBOXRS_OWNER_PID", str(os.getppid())))
    child = subprocess.Popen(command, start_new_session=False)
    while child.poll() is None:
        if os.getppid() != owner_pid:
            terminate(signal.SIGTERM, None)
            break
        time.sleep(0.05)
    return int(child.returncode if child.returncode is not None else 128 + signal.SIGTERM)


def main() -> int:
    try:
        marker = sys.argv.index("--")
    except ValueError as exc:
        raise SystemExit("process supervisor requires -- before command") from exc
    return run(sys.argv[marker + 1 :])


if __name__ == "__main__":
    raise SystemExit(main())
