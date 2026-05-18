"""Subprocess-level smoke tests for the BlackBoxRS CLI.

Every test here spawns real child processes against a tmp ``HOME`` so
they never touch the user's actual ``~/.blackboxrs``.  Coverage:

* Foreground lifecycle: ``python -m blackboxrs start --foreground``
  writes an identity pidfile, flushes events to the JSONL log, and
  exits cleanly on SIGTERM.
* **Background lifecycle**: ``start`` (default, detached) + ``status``
  + ``stop`` — the common operator path.
* **Stale pidfile refusal**: a leftover pidfile pointing at a dead
  (or foreign) PID must be refused by ``status``/``stop`` and cleaned
  up, and the next ``start`` must succeed without needing manual
  intervention.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blackboxrs.core.schemas import BlackBoxEvent


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="CLI smoke test uses /proc and POSIX signals",
)


def _write_test_config(dest: Path, log_dir: Path) -> None:
    """Minimal config that keeps ROS disabled and runs the system
    monitor at a fast cadence so events appear quickly."""
    dest.write_text(
        textwrap.dedent(
            f"""\
            log_dir: "{log_dir}"
            log_rotation_mb: 50
            log_max_files: 5
            event_bus_queue_maxsize: 256

            ros_monitor:
              enabled: false

            system_monitor:
              enabled: true
              interval_sec: 0.1
              gpu_backend: none

            anomaly_engine:
              enabled: true
              thresholds:
                cpu_percent: 200.0
                memory_percent: 200.0
                gpu_temp_c: 200.0
            """
        )
    )


def _write_observer_config(dest: Path, log_dir: Path, observed_host: str) -> None:
    """Observer-mode config. ROS disabled because there's no live graph
    in this subprocess test, and system_monitor is auto-disabled by
    ``apply_runtime_role`` anyway. We exercise the role plumbing, the
    startup log line, and the bundle metadata path — not live capture."""
    dest.write_text(
        textwrap.dedent(
            f"""\
            log_dir: "{log_dir}"
            log_rotation_mb: 50
            log_max_files: 5
            event_bus_queue_maxsize: 256

            runtime:
              role: observer
              observed_host: {observed_host}

            ros_monitor:
              enabled: false

            anomaly_engine:
              enabled: true
            """
        )
    )


def _wait_for(predicate, *, timeout: float, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it returns truthy or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _read_jsonl_events(log_dir: Path) -> list[BlackBoxEvent]:
    events: list[BlackBoxEvent] = []
    for path in sorted(log_dir.glob("blackboxrs_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(BlackBoxEvent.from_jsonl(line))
            except Exception:
                continue
    return events


class TestCliForegroundLifecycle:
    def test_start_writes_identity_pidfile_logs_events_and_stops_cleanly(
        self, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        blackbox_dir = home / ".blackboxrs"
        log_dir = blackbox_dir / "logs"
        blackbox_dir.mkdir()
        log_dir.mkdir()

        config_path = tmp_path / "config.yaml"
        _write_test_config(config_path, log_dir)

        env = os.environ.copy()
        real_home = os.path.expanduser("~")
        env["HOME"] = str(home)
        env.setdefault("PYTHONUSERBASE", os.path.join(real_home, ".local"))
        # Keep child's stdout/stderr out of the test output but capture
        # for diagnostic purposes on failure.

        cmd = [
            sys.executable,
            "-m",
            "blackboxrs",
            "start",
            "--foreground",
            "--config",
            str(config_path),
        ]

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        pid_file = blackbox_dir / "blackboxrs.pid"
        try:
            assert _wait_for(
                lambda: pid_file.is_file() and pid_file.stat().st_size > 0,
                timeout=5.0,
            ), "pidfile did not appear within 5s"

            payload = json.loads(pid_file.read_text())
            # Identity payload is the new contract — must include pid,
            # starttime (Linux), and cmdline.
            assert payload["pid"] == proc.pid, (
                f"pidfile pid ({payload['pid']}) != subprocess pid ({proc.pid})"
            )
            assert isinstance(payload.get("starttime"), int)
            assert "blackboxrs" in payload.get("cmdline", "").lower()

            # Wait for at least one event to land on disk.
            assert _wait_for(
                lambda: any(log_dir.glob("blackboxrs_*.jsonl"))
                and _read_jsonl_events(log_dir),
                timeout=5.0,
            ), "no events appeared on disk within 5s"

            events = _read_jsonl_events(log_dir)
            sources = {e.source for e in events}
            assert "system_monitor" in sources, (
                f"expected system_monitor events, got sources={sources}"
            )
        finally:
            # Clean shutdown: SIGTERM to the process group.  The daemon
            # installs a SIGTERM handler that tears everything down.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
                pytest.fail(
                    "daemon did not exit within 10s of SIGTERM; "
                    "signal handler may be broken"
                )

        # After a clean shutdown the pidfile should be removed.
        assert not pid_file.exists(), (
            "pidfile was not cleaned up on shutdown"
        )
        # Exit code must indicate normal termination.  SIGTERM results
        # in returncode == 0 here because the click handler catches it
        # and calls daemon.stop() instead of letting the signal raise.
        assert proc.returncode == 0, (
            f"daemon exited with returncode={proc.returncode}; "
            f"stderr={proc.stderr.read().decode(errors='replace') if proc.stderr else ''}"
        )


# ---------------------------------------------------------------------------
# Background lifecycle — start (detached) + status + stop
# ---------------------------------------------------------------------------


def _prepare_isolated_home(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    """Build a tmp HOME with a config pointing at a tmp log dir.

    Returns ``(home, blackbox_dir, config_path, env)`` where ``env`` is
    a copy of the current environment with ``HOME`` redirected.
    """
    home = tmp_path / "home"
    home.mkdir()
    blackbox_dir = home / ".blackboxrs"
    log_dir = blackbox_dir / "logs"
    blackbox_dir.mkdir()
    log_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    _write_test_config(config_path, log_dir)

    env = os.environ.copy()
    real_home = os.path.expanduser("~")
    env["HOME"] = str(home)
    env.setdefault("PYTHONUSERBASE", os.path.join(real_home, ".local"))
    return home, blackbox_dir, config_path, env


def _run_cli(
    args: list[str], *, env: dict, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m blackboxrs <args>`` and capture stdout/stderr as text."""
    cmd = [sys.executable, "-m", "blackboxrs", *args]
    return subprocess.run(
        cmd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_pid_file(pid_file: Path, *, timeout: float) -> dict:
    """Block until the pidfile appears with a parseable JSON payload."""

    def ready() -> bool:
        if not pid_file.is_file():
            return False
        try:
            json.loads(pid_file.read_text())
        except (ValueError, OSError):
            return False
        return True

    assert _wait_for(ready, timeout=timeout), (
        f"pidfile {pid_file} did not appear with JSON payload within {timeout}s"
    )
    return json.loads(pid_file.read_text())


class TestCliBackgroundLifecycle:
    """Drive the operator path: ``start`` (detached) / ``status`` / ``stop``."""

    def test_start_status_stop_roundtrip(self, tmp_path: Path):
        _, blackbox_dir, config_path, env = _prepare_isolated_home(tmp_path)
        pid_file = blackbox_dir / "blackboxrs.pid"
        log_dir = blackbox_dir / "logs"

        # 1. start (background)
        result = _run_cli(
            ["start", "--config", str(config_path)], env=env
        )
        assert result.returncode == 0, (
            f"start failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "background" in result.stdout.lower() or (
            "pid" in result.stdout.lower()
        ), f"unexpected start output: {result.stdout!r}"

        try:
            payload = _wait_for_pid_file(pid_file, timeout=5.0)
            daemon_pid = payload["pid"]
            assert daemon_pid > 0
            # The background child is a different process from the CLI
            # invocation above — the CLI exited after starting it.
            assert isinstance(payload.get("starttime"), int)
            assert "blackboxrs" in payload.get("cmdline", "").lower()

            # 2. Wait for the daemon to start producing events so
            # status has something to summarise.
            assert _wait_for(
                lambda: any(log_dir.glob("blackboxrs_*.jsonl"))
                and _read_jsonl_events(log_dir),
                timeout=5.0,
            ), "daemon did not flush any events to disk"

            # 3. status
            status_res = _run_cli(["status"], env=env)
            assert status_res.returncode == 0, (
                f"status failed: {status_res.stderr!r}"
            )
            combined = status_res.stdout + status_res.stderr
            assert "RUNNING" in combined, (
                f"status did not report RUNNING:\n{combined!r}"
            )
            assert str(daemon_pid) in combined, (
                f"status did not include daemon PID {daemon_pid}:\n"
                f"{combined!r}"
            )

            # 4. stop
            stop_res = _run_cli(["stop"], env=env)
            assert stop_res.returncode == 0, (
                f"stop failed: {stop_res.stderr!r}"
            )
            assert "stop" in stop_res.stdout.lower(), (
                f"unexpected stop output: {stop_res.stdout!r}"
            )

            # 5. The daemon should exit shortly after SIGTERM and the
            # pidfile should be cleaned up.
            assert _wait_for(
                lambda: not pid_file.exists(),
                timeout=10.0,
            ), "pidfile was not cleaned up after stop"

            # And the underlying daemon process should be gone.
            assert _wait_for(
                lambda: not _pid_is_alive(daemon_pid),
                timeout=10.0,
            ), f"daemon pid {daemon_pid} still alive after stop"
        finally:
            # Belt-and-suspenders cleanup in case an assertion fired
            # before the orderly stop landed.
            if pid_file.exists():
                try:
                    payload = json.loads(pid_file.read_text())
                    pid = payload.get("pid")
                    if isinstance(pid, int) and _pid_is_alive(pid):
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, OSError):
                    pass

    def test_start_refuses_when_already_running(self, tmp_path: Path):
        """A second ``start`` while the daemon is up must refuse,
        not double-spawn."""
        _, blackbox_dir, config_path, env = _prepare_isolated_home(tmp_path)
        pid_file = blackbox_dir / "blackboxrs.pid"

        first = _run_cli(["start", "--config", str(config_path)], env=env)
        assert first.returncode == 0, first.stderr
        try:
            payload = _wait_for_pid_file(pid_file, timeout=5.0)
            daemon_pid = payload["pid"]

            second = _run_cli(
                ["start", "--config", str(config_path)], env=env
            )
            # Should exit non-zero and mention "already running".
            assert second.returncode != 0, (
                f"second start succeeded unexpectedly: {second.stdout!r}"
            )
            combined = second.stdout + second.stderr
            assert "already running" in combined.lower(), (
                f"refusal message missing:\n{combined!r}"
            )
            # Still only one daemon.
            payload_after = json.loads(pid_file.read_text())
            assert payload_after["pid"] == daemon_pid, (
                "pidfile changed — second start may have replaced the daemon"
            )
        finally:
            _run_cli(["stop"], env=env, timeout=10.0)
            _wait_for(lambda: not pid_file.exists(), timeout=10.0)


# ---------------------------------------------------------------------------
# Stale / foreign pidfile refusal
# ---------------------------------------------------------------------------


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _find_dead_pid() -> int:
    """Return a PID that is currently unused.

    Uses the standard Linux pid_max range and walks backwards until
    ``os.kill(pid, 0)`` raises ``ProcessLookupError``.  Not guaranteed
    to stay dead for the full test lifetime, but close enough for a
    functional smoke test of the cleanup path.
    """
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except (OSError, ValueError):
        pid_max = 32768
    # Walk from a high value down; 98% of machines have nothing near
    # pid_max at any given moment.
    for candidate in range(pid_max - 1, max(pid_max - 1000, 100), -1):
        if not _pid_is_alive(candidate):
            return candidate
    pytest.skip("could not find a dead PID for stale-pidfile test")
    raise AssertionError("unreachable")  # for type-checker


class TestCliRefusesStalePidfile:
    def test_status_reports_stopped_and_cleans_stale_pidfile(
        self, tmp_path: Path
    ):
        """A pidfile left behind pointing at a dead PID must be
        treated as stale by the CLI and cleaned up — otherwise
        ``start`` would wrongly refuse to run ever again."""
        _, blackbox_dir, _, env = _prepare_isolated_home(tmp_path)
        pid_file = blackbox_dir / "blackboxrs.pid"
        dead_pid = _find_dead_pid()

        pid_file.write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "starttime": 1,  # arbitrary — process is dead anyway
                    "cmdline": "python -m blackboxrs start --foreground",
                }
            )
        )

        status_res = _run_cli(["status"], env=env)
        combined = status_res.stdout + status_res.stderr
        assert "STOPPED" in combined, (
            f"status did not report STOPPED for stale pidfile:\n{combined!r}"
        )
        assert not pid_file.exists(), (
            "stale pidfile was not cleaned up by is_running()"
        )

    def test_stop_refuses_on_foreign_pidfile(self, tmp_path: Path):
        """A live PID whose identity does not match the recorded
        cmdline must not be signaled by ``stop``."""
        _, blackbox_dir, _, env = _prepare_isolated_home(tmp_path)
        pid_file = blackbox_dir / "blackboxrs.pid"

        # Target: our own test runner.  It is alive but its cmdline
        # does not contain "blackboxrs" and, combined with a spoofed
        # starttime, must be rejected.
        pid_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "starttime": 1,  # deliberately wrong
                    "cmdline": "/usr/bin/firefox",
                }
            )
        )

        stop_res = _run_cli(["stop"], env=env)
        # stop exits non-zero and says "not running".
        assert stop_res.returncode != 0, (
            f"stop succeeded on foreign pidfile: {stop_res.stdout!r}"
        )
        combined = stop_res.stdout + stop_res.stderr
        assert "not running" in combined.lower(), (
            f"expected 'not running' message:\n{combined!r}"
        )
        # Pidfile cleanup happened as part of the identity check.
        assert not pid_file.exists()

    def test_start_succeeds_after_stale_pidfile_cleanup(
        self, tmp_path: Path
    ):
        """The operator path: daemon crashed and left a stale pidfile.
        ``start`` must succeed on the next attempt rather than
        wedging forever."""
        _, blackbox_dir, config_path, env = _prepare_isolated_home(tmp_path)
        pid_file = blackbox_dir / "blackboxrs.pid"
        dead_pid = _find_dead_pid()
        pid_file.write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "starttime": 1,
                    "cmdline": "python -m blackboxrs start --foreground",
                }
            )
        )

        result = _run_cli(["start", "--config", str(config_path)], env=env)
        assert result.returncode == 0, (
            f"start failed after stale pidfile; output:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        try:
            payload = _wait_for_pid_file(pid_file, timeout=5.0)
            assert payload["pid"] != dead_pid, (
                "new daemon reused the stale PID — cleanup path broken"
            )
        finally:
            _run_cli(["stop"], env=env, timeout=10.0)
            _wait_for(lambda: not pid_file.exists(), timeout=10.0)


# ---------------------------------------------------------------------------
# Observer-mode lifecycle — runtime.role: observer, end-to-end
# ---------------------------------------------------------------------------


class TestCliObserverLifecycle:
    """The observer-mode pivot promises that a workstation can run the
    daemon and produce a bundle that names both the observer and the
    observed robot. These tests exercise that path against real
    subprocess invocations of the CLI so the unit-test seam
    (`build_incident` on a JSONL fixture) is not the only thing in CI
    proving observer mode."""

    _OBSERVED = "go2-edu-01"

    def _seed_dead_topic_jsonl(self, log_dir: Path) -> None:
        """Write a tiny synthetic anomaly stream the observer-mode
        builder can chew on. Mirrors what an off-board daemon would
        capture from a robot whose /scan went silent."""
        start = datetime(2026, 5, 17, 14, 22, 0, tzinfo=timezone.utc)
        events = [
            {
                "timestamp": (start + timedelta(seconds=i))
                .isoformat()
                .replace("+00:00", "Z"),
                "source": "ros_monitor",
                "event_type": "ros.frequency",
                "severity": "info",
                "data": {"topic": "/scan", "frequency_hz": 10.0,
                         "interval_ms": 100.0},
                "metadata": {"session_id": "obs_cli", "role": "observer",
                             "observed_host": self._OBSERVED},
            }
            for i in range(4)
        ]
        events.append({
            "timestamp": (start + timedelta(seconds=8))
            .isoformat()
            .replace("+00:00", "Z"),
            "source": "anomaly_engine",
            "event_type": "anomaly.dead_topic",
            "severity": "error",
            "data": {
                "detector": "DeadTopicDetector",
                "topic": "/scan", "metric": "/scan",
                "value": 0.0, "threshold": 5.0,
                "message": "Topic /scan silent for 5.0 s (timeout exceeded).",
            },
            "metadata": {
                "session_id": "obs_cli",
                "role": "observer",
                "observed_host": self._OBSERVED,
                "detector_class": (
                    "blackboxrs.anomaly_engine.detectors.dead_topic."
                    "DeadTopicDetector"
                ),
                "signature_fields": ["topic"],
                "target_subsystem": "ros",
            },
        })
        log_file = log_dir / "blackboxrs_20260517_142200_000000.jsonl"
        with open(log_file, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev))
                fh.write("\n")

    def test_start_foreground_announces_observer_mode_in_stderr(
        self, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        blackbox_dir = home / ".blackboxrs"
        log_dir = blackbox_dir / "logs"
        blackbox_dir.mkdir()
        log_dir.mkdir()

        config_path = tmp_path / "config.yaml"
        _write_observer_config(config_path, log_dir, self._OBSERVED)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env.setdefault(
            "PYTHONUSERBASE",
            os.path.join(os.path.expanduser("~"), ".local"),
        )

        cmd = [
            sys.executable, "-m", "blackboxrs",
            "start", "--foreground", "--config", str(config_path),
        ]
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        pid_file = blackbox_dir / "blackboxrs.pid"
        try:
            assert _wait_for(
                lambda: pid_file.is_file() and pid_file.stat().st_size > 0,
                timeout=5.0,
            ), "pidfile did not appear within 5s"
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5.0)
                pytest.fail(
                    "observer-mode daemon did not exit within 10s of SIGTERM"
                )

        combined = (
            stdout_bytes.decode(errors="replace")
            + stderr_bytes.decode(errors="replace")
        )
        # The startup banner must announce observer mode and the
        # observed_host. Emitted by format_banner when
        # config.runtime.is_observer is True.
        assert "observer mode" in combined, (
            f"observer-mode startup line missing; got:\n{combined}"
        )
        assert self._OBSERVED in combined, (
            f"observed_host {self._OBSERVED!r} missing from startup banner; "
            f"got:\n{combined}"
        )
        assert proc.returncode == 0, (
            f"observer-mode daemon exited with returncode={proc.returncode}; "
            f"combined={combined}"
        )
        assert not pid_file.exists(), (
            "pidfile was not cleaned up on shutdown"
        )

    def test_incident_build_in_observer_mode_emits_observer_and_observed(
        self, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        blackbox_dir = home / ".blackboxrs"
        log_dir = blackbox_dir / "logs"
        incidents_dir = blackbox_dir / "incidents"
        blackbox_dir.mkdir()
        log_dir.mkdir()
        incidents_dir.mkdir()

        config_path = tmp_path / "config.yaml"
        _write_observer_config(config_path, log_dir, self._OBSERVED)
        self._seed_dead_topic_jsonl(log_dir)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env.setdefault(
            "PYTHONUSERBASE",
            os.path.join(os.path.expanduser("~"), ".local"),
        )

        result = _run_cli(
            [
                "incident", "build",
                "--config", str(config_path),
                "--log-dir", str(log_dir),
                "--incidents-dir", str(incidents_dir),
                "--start", "2026-05-17T14:22:00+00:00",
                "--end", "2026-05-17T14:22:20+00:00",
                "--title", "observer cli smoke",
            ],
            env=env,
        )
        assert result.returncode == 0, (
            f"incident build failed in observer mode:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

        bundles = list(incidents_dir.glob("inc_*"))
        assert len(bundles) == 1, (
            f"expected exactly one bundle, got {bundles}"
        )
        bundle = bundles[0]

        incident_obj = json.loads(
            (bundle / "incident.json").read_text(encoding="utf-8")
        )
        assert incident_obj["observed_host"] == self._OBSERVED
        assert incident_obj["observer_host"], (
            "observer_host should be populated in observer-mode bundle"
        )

        report = (bundle / "report.md").read_text(encoding="utf-8")
        assert "- **Observer**:" in report
        assert f"`{self._OBSERVED}`" in report
        # The old single-line Host: header must not co-exist with the
        # two-line Observer/Observed header.
        assert "- **Host**:" not in report
