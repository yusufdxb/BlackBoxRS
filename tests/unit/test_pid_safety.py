"""Tests for PID-file identity verification.

The pidfile is the contact surface between ``robot-blackbox start`` and
``robot-blackbox stop``.  A naive implementation that just reads a PID
and signals it is unsafe on Linux because PIDs are recycled: if our
daemon crashed without cleaning its pidfile and the kernel later hands
that PID to an unrelated process (bash, systemd-something, another
user's editor), ``stop_running`` would happily SIGTERM that process.

These tests pin down the stronger identity contract: the pidfile must
carry enough metadata to prove the live process *is in fact* the daemon
we wrote it for, and ``is_running`` must reject any mismatch by
returning ``(False, None)`` and cleaning up the stale file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from blackboxrs.cli.daemon import BlackBoxDaemon


# Every test in this module redirects the pidfile to a tmp location so
# real daemons on the host are not touched.
@pytest.fixture
def pid_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pid_dir = tmp_path / "pid"
    pid_file = pid_dir / "blackboxrs.pid"
    monkeypatch.setattr(BlackBoxDaemon, "_PID_DIR", pid_dir)
    monkeypatch.setattr(BlackBoxDaemon, "_PID_FILE", pid_file)
    return pid_dir, pid_file


def _read_starttime_of(pid: int) -> int:
    """Read field 22 of /proc/<pid>/stat (starttime in jiffies).

    Mirrors the production helper so tests can construct valid
    identity payloads without importing private symbols.
    """
    with open(f"/proc/{pid}/stat", "rb") as fh:
        data = fh.read()
    # The command name is in parens and may contain spaces, so we have
    # to find the closing paren before splitting the rest by whitespace.
    rparen = data.rindex(b")")
    fields = data[rparen + 2 :].split()
    # After the closing paren, field 3 in the stat layout is fields[0],
    # so starttime (field 22) is fields[22 - 3] = fields[19].
    return int(fields[19])


class TestIsRunningIdentityCheck:
    """is_running() must verify the pidfile identity, not just PID liveness."""

    def test_no_pidfile_means_not_running(self, pid_paths):
        running, pid = BlackBoxDaemon.is_running()
        assert running is False
        assert pid is None

    def test_malformed_pidfile_reports_not_running(self, pid_paths):
        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("this is not a pid\n")
        running, pid = BlackBoxDaemon.is_running()
        assert running is False
        assert pid is None

    def test_identity_mismatch_pidfile_is_rejected_as_stale(self, pid_paths):
        """Simulate a PID recycled by the kernel after the daemon crashed.

        We point the pidfile at OUR own pid (guaranteed to be alive) but
        claim a fake starttime that cannot possibly match /proc.
        is_running() must detect the identity mismatch, reject, and
        clean up the pidfile so the next start isn't blocked."""
        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "starttime": 1,  # deliberately wrong
                    "cmdline": "python -m blackboxrs start --foreground",
                }
            )
        )
        running, pid = BlackBoxDaemon.is_running()
        assert running is False
        assert pid is None
        # Stale file must be cleaned up so the next start() succeeds.
        assert not pid_file.exists()

    def test_foreign_cmdline_is_rejected(self, pid_paths):
        """A live PID whose /proc/<pid>/cmdline does not mention
        blackboxrs must NOT be treated as our daemon, even if the
        starttime happens to match."""
        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        my_pid = os.getpid()
        pid_file.write_text(
            json.dumps(
                {
                    "pid": my_pid,
                    "starttime": _read_starttime_of(my_pid),
                    # pytest is running us: pretend the pidfile was
                    # written for some unrelated command.
                    "cmdline": "/usr/bin/firefox",
                }
            )
        )
        running, pid = BlackBoxDaemon.is_running()
        # We cannot verify identity → must not claim the process is
        # ours, even though the PID is alive.
        assert running is False
        assert pid is None

    def test_matching_identity_is_accepted(
        self, pid_paths, monkeypatch: pytest.MonkeyPatch
    ):
        """When pid, starttime, and cmdline all match, the pidfile is
        trusted (this is the happy path).

        Because pytest's own cmdline does not contain 'blackboxrs', we
        monkey-patch the live cmdline reader to return a daemon-like
        string.  This exercises the accept-path without requiring us
        to spawn a real daemon from within a unit test."""
        from blackboxrs.cli import daemon as daemon_mod

        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        my_pid = os.getpid()

        fake_cmdline = "python -m blackboxrs start --foreground"
        monkeypatch.setattr(
            daemon_mod, "_read_proc_cmdline",
            lambda pid: fake_cmdline if pid == my_pid else None,
        )

        pid_file.write_text(
            json.dumps(
                {
                    "pid": my_pid,
                    "starttime": _read_starttime_of(my_pid),
                    "cmdline": fake_cmdline,
                }
            )
        )
        running, pid = BlackBoxDaemon.is_running()
        assert running is True
        assert pid == my_pid

    def test_legacy_plain_integer_pidfile_is_rejected(self, pid_paths):
        """Older releases wrote bare PIDs with no identity metadata.
        Any such pidfile cannot be verified and must be treated as
        stale so we never signal a recycled PID."""
        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(f"{os.getpid()}\n")
        running, pid = BlackBoxDaemon.is_running()
        assert running is False
        assert pid is None
        assert not pid_file.exists()


class TestStopRunningRefusesUnverifiedPid:
    """stop_running() must not signal a PID it cannot prove is ours."""

    def test_stop_running_refuses_when_identity_mismatches(self, pid_paths):
        _, pid_file = pid_paths
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "starttime": 1,  # wrong
                    "cmdline": "python -m blackboxrs start --foreground",
                }
            )
        )
        with pytest.raises(RuntimeError):
            BlackBoxDaemon.stop_running()


class TestWritePidFileAtomicAndIdentified:
    """The daemon must write a JSON pidfile with identity metadata and
    do so atomically enough that a concurrent reader never sees a
    half-written file.
    """

    def test_write_pid_file_contains_identity_fields(self, pid_paths):
        pid_dir, pid_file = pid_paths
        cfg = BlackBoxConfig_minimal()
        daemon = BlackBoxDaemon(cfg)
        daemon._write_pid_file()

        payload = json.loads(pid_file.read_text())
        assert payload["pid"] == os.getpid()
        assert "starttime" in payload
        assert isinstance(payload["starttime"], int)
        assert "cmdline" in payload
        # Our current process cmdline should contain 'python' or the
        # pytest runner path: just assert it's non-empty.
        assert payload["cmdline"]

    def test_write_pid_file_is_atomic(self, pid_paths):
        """Write via a tmp + rename so readers never see a partially
        written file.  We can't observe the rename directly, but the
        final file must be complete JSON."""
        _, pid_file = pid_paths
        cfg = BlackBoxConfig_minimal()
        daemon = BlackBoxDaemon(cfg)
        daemon._write_pid_file()
        # Must be valid JSON end-to-end.
        json.loads(pid_file.read_text())
        # No leftover tmp files next to it.
        siblings = list(pid_file.parent.iterdir())
        assert siblings == [pid_file], (
            f"unexpected siblings of pidfile (tmp file leaked?): {siblings}"
        )


def BlackBoxConfig_minimal():
    """Helper: a valid BlackBoxConfig instance.  Inline import to keep
    this module's import graph small when collected."""
    from blackboxrs.core.config import BlackBoxConfig

    return BlackBoxConfig()


# Skip the whole module on non-Linux because the identity check relies
# on /proc/<pid>/stat.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PID-file identity verification relies on /proc (Linux-only)",
)
