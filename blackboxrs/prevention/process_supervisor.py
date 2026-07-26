"""Linux process-lifetime owner for the telemetry guard.

Supported process model
-----------------------
The telemetry guard starts this wrapper as a new session and process-group
leader.  The wrapper supports one synchronous foreground command and all of
its descendants while they remain in that owned session and process group.
It becomes a child subreaper so normal forked descendants cannot outlive a
completed or terminated foreground command.

Creating another process group or session, double-forking into the background,
and daemonizing are outside the supported model.  When those behaviours are
observable below this subreaper, the wrapper rejects the command and kills the
remaining tree.  Linux cgroup ownership is not used, so this is deliberately
not a claim of universal cleanup for hostile processes that evade ``/proc``
observation or leave the wrapper's process namespace.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1
PROCESS_MODEL_VIOLATION = 125
DEPENDENT_LAUNCH_FAILED = 127
_POLL_INTERVAL_SEC = 0.02
_TERM_GRACE_SEC = 1.0
_KILL_GRACE_SEC = 1.0


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    ppid: int
    pgrp: int
    session: int
    state: str


def _prctl(option: int, value: int) -> None:
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        raise RuntimeError("libc is required for process lifetime ownership")
    libc = ctypes.CDLL(libc_name, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _process_table() -> dict[int, _ProcessInfo]:
    processes: dict[int, _ProcessInfo] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as proc_stat:
                raw = proc_stat.read()
            close = raw.rfind(")")
            fields = raw[close + 2 :].split()
            pid = int(entry)
            processes[pid] = _ProcessInfo(
                pid=pid,
                state=fields[0],
                ppid=int(fields[1]),
                pgrp=int(fields[2]),
                session=int(fields[3]),
            )
        except (OSError, ValueError, IndexError):
            continue
    return processes


def _descendants(
    root_pid: int, processes: dict[int, _ProcessInfo] | None = None
) -> set[int]:
    table = processes if processes is not None else _process_table()
    found: set[int] = set()
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, info in table.items():
            if info.ppid == parent and pid not in found:
                found.add(pid)
                frontier.append(pid)
    return found


def _live_descendants(root_pid: int) -> tuple[set[int], dict[int, _ProcessInfo]]:
    table = _process_table()
    descendants = _descendants(root_pid, table)
    live = {
        pid
        for pid in descendants
        if (info := table.get(pid)) is not None and info.state != "Z"
    }
    return live, table


def _signal_processes(pids: set[int], signum: int) -> None:
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _reap_adopted_children() -> None:
    while True:
        try:
            waited_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == 0:
            return


def _terminate_owned_tree(root_pid: int) -> None:
    """Terminate every descendant still observable below this subreaper."""

    live, _table = _live_descendants(root_pid)
    _signal_processes(live, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SEC
    while time.monotonic() < deadline:
        live, _table = _live_descendants(root_pid)
        if not live:
            _reap_adopted_children()
            return
        # Include descendants forked during shutdown.
        _signal_processes(live, signal.SIGTERM)
        time.sleep(_POLL_INTERVAL_SEC)

    live, _table = _live_descendants(root_pid)
    _signal_processes(live, signal.SIGKILL)
    deadline = time.monotonic() + _KILL_GRACE_SEC
    while time.monotonic() < deadline:
        live, _table = _live_descendants(root_pid)
        if not live:
            break
        _signal_processes(live, signal.SIGKILL)
        time.sleep(_POLL_INTERVAL_SEC)
    _reap_adopted_children()


def _normalise_returncode(returncode: int) -> int:
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def _has_process_model_violation(
    supervisor_pid: int, owned_pgrp: int, owned_session: int
) -> bool:
    descendants, table = _live_descendants(supervisor_pid)
    return any(
        (info := table.get(pid)) is not None
        and (info.pgrp != owned_pgrp or info.session != owned_session)
        for pid in descendants
    )


def _set_child_parent_death_signal(supervisor_pid: int) -> None:
    _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != supervisor_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _report_dependent_started(child_pid: int) -> None:
    """Acknowledge the actual dependent launch to the guard over an inherited pipe."""
    raw_fd = os.environ.pop("BLACKBOXRS_READY_FD", None)
    if raw_fd is None:
        return
    ready_fd = int(raw_fd)
    try:
        os.write(ready_fd, f"{child_pid}\n".encode("ascii"))
    finally:
        os.close(ready_fd)


def run(command: list[str]) -> int:
    if not command:
        raise ValueError("dependent command is required")
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    supervisor_pid = os.getpid()
    owned_pgrp = os.getpgrp()
    owned_session = os.getsid(0)
    child: subprocess.Popen[Any] | None = None
    termination_signal: int | None = None

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal termination_signal
        if termination_signal is None:
            termination_signal = _signum

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    signal.signal(signal.SIGINT, terminate)
    owner_pid = int(os.environ.get("BLACKBOXRS_OWNER_PID", str(os.getppid())))
    if os.getppid() != owner_pid:
        return 128 + signal.SIGTERM
    try:
        child = subprocess.Popen(
            command,
            start_new_session=False,
            preexec_fn=lambda: _set_child_parent_death_signal(supervisor_pid),
        )
    except OSError:
        return DEPENDENT_LAUNCH_FAILED
    _report_dependent_started(child.pid)

    while True:
        child_code = child.poll()
        if termination_signal is not None:
            _terminate_owned_tree(supervisor_pid)
            child.poll()
            return 128 + termination_signal
        if os.getppid() != owner_pid:
            _terminate_owned_tree(supervisor_pid)
            child.poll()
            return 128 + signal.SIGTERM
        if _has_process_model_violation(
            supervisor_pid, owned_pgrp, owned_session
        ):
            _terminate_owned_tree(supervisor_pid)
            child.poll()
            return PROCESS_MODEL_VIOLATION
        if child_code is not None:
            live, _table = _live_descendants(supervisor_pid)
            if live:
                # A foreground command that exits while descendants continue is
                # daemon/background behaviour, not successful completion.
                _terminate_owned_tree(supervisor_pid)
                return PROCESS_MODEL_VIOLATION
            _reap_adopted_children()
            return _normalise_returncode(int(child_code))
        time.sleep(_POLL_INTERVAL_SEC)


def main() -> int:
    try:
        marker = sys.argv.index("--")
    except ValueError as exc:
        raise SystemExit("process supervisor requires -- before command") from exc
    return run(sys.argv[marker + 1 :])


if __name__ == "__main__":
    raise SystemExit(main())
