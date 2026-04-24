"""Click-based CLI for BlackBoxRS.

Provides commands for starting/stopping the daemon, viewing status,
dumping and replaying logs, inspecting configuration, and initialising
the configuration directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, TextIO

import click
import yaml

from blackboxrs import __version__
from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.schemas import BlackBoxEvent
from blackboxrs.cli.daemon import BlackBoxDaemon
from blackboxrs.cli.formatters import (
    format_banner,
    format_event,
    format_status,
)
from blackboxrs.logging.reader import LogReader

# Glob pattern used by the writer.  Keeping it in one place so the
# read-side and write-side never drift apart.
_LOG_GLOB = "blackboxrs_*.jsonl"

# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="BlackBoxRS")
def cli() -> None:
    """BlackBoxRS -- Flight recorder for ROS 2 robots."""


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=False),
    default=None,
    help="Path to a YAML configuration file.",
)
@click.option(
    "--foreground",
    "-f",
    is_flag=True,
    default=False,
    help="Run in the foreground instead of daemonising.",
)
def start(config_path: str | None, foreground: bool) -> None:
    """Start the BlackBoxRS daemon."""
    cfg = _load_config(config_path)

    running, pid = BlackBoxDaemon.is_running()
    if running:
        click.echo(
            click.style(
                f"BlackBoxRS is already running (PID {pid}). "
                "Use 'robot-blackbox stop' first.",
                fg="yellow",
            )
        )
        raise SystemExit(1)

    if foreground:
        _start_foreground(cfg)
    else:
        _start_background(config_path)


def _start_foreground(cfg: BlackBoxConfig) -> None:
    """Launch the daemon in the current process."""
    daemon = BlackBoxDaemon(cfg)
    click.echo(format_banner(daemon.session.session_id, cfg))
    try:
        daemon.start()
        daemon.wait()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        click.echo(click.style("BlackBoxRS stopped.", fg="bright_black"))


def _start_background(config_path: str | None) -> None:
    """Spawn the daemon as a detached background process."""
    cmd = [sys.executable, "-m", "blackboxrs", "start", "--foreground"]
    if config_path:
        cmd.extend(["--config", config_path])

    log_path = Path("~/.blackboxrs/daemon.stdout_stderr.log").expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(log_path, "ab") as stdio_log:
            subprocess.Popen(
                cmd,
                stdout=stdio_log,
                stderr=stdio_log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        click.echo(click.style(f"Failed to spawn daemon: {exc}", fg="red"))
        raise SystemExit(1)

    # Give the child a moment to write its PID file
    time.sleep(0.5)

    running, pid = BlackBoxDaemon.is_running()
    if running:
        click.echo(
            click.style(f"BlackBoxRS started in background (PID {pid}).", fg="green")
        )
    else:
        click.echo(
            click.style(
                "BlackBoxRS daemon was spawned but does not appear to be running. "
                f"See {log_path} for background stdout/stderr, or try starting "
                "with --foreground to see errors directly.",
                fg="yellow",
            )
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command()
def stop() -> None:
    """Stop the running BlackBoxRS daemon."""
    running, pid = BlackBoxDaemon.is_running()
    if not running:
        click.echo(click.style("BlackBoxRS is not running.", fg="yellow"))
        raise SystemExit(1)

    try:
        BlackBoxDaemon.stop_running()
    except (RuntimeError, OSError) as exc:
        click.echo(click.style(f"Failed to stop daemon: {exc}", fg="red"))
        raise SystemExit(1)

    click.echo(click.style(f"Sent stop signal to BlackBoxRS (PID {pid}).", fg="green"))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show current daemon status and recent event summary."""
    running, pid = BlackBoxDaemon.is_running()

    # Attempt to read recent events from the log directory
    events = _read_recent_events(count=100)

    click.echo()
    click.echo(click.style("  BlackBoxRS Status", bold=True))
    click.echo(click.style("  " + "-" * 40, fg="bright_black"))
    click.echo(format_status(running, pid, events))

    # Anomaly count
    anomaly_count = sum(1 for e in events if e.source == "anomaly_engine")
    if anomaly_count:
        click.echo(
            click.style(f"  Anomalies: {anomaly_count} detected", fg="yellow", bold=True)
        )

    click.echo()


# ---------------------------------------------------------------------------
# dump-log
# ---------------------------------------------------------------------------


@cli.command("dump-log")
@click.option(
    "--source",
    "-s",
    type=click.Choice(["ros", "system", "anomaly", "recorder", "all"]),
    default="all",
    help="Filter events by source module.",
)
@click.option(
    "--severity",
    type=click.Choice(["debug", "info", "warning", "error", "critical"]),
    default=None,
    help="Show only events at this severity level.",
)
@click.option(
    "--last",
    "-n",
    type=int,
    default=50,
    show_default=True,
    help="Number of events to display.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output events as raw JSON lines.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    default=False,
    help="Follow the log in real time (like tail -f).",
)
def dump_log(
    source: str,
    severity: str | None,
    last: int,
    as_json: bool,
    follow: bool,
) -> None:
    """Display recorded events from the BlackBoxRS log files."""
    source_map = {
        "ros": "ros_monitor",
        "system": "system_monitor",
        "anomaly": "anomaly_engine",
        "recorder": "rosbag_recorder",
        "all": None,
    }
    source_filter = source_map[source]

    if follow:
        _follow_log(source_filter, severity, as_json)
        return

    events = _read_recent_events(count=last)
    events = _apply_filters(events, source_filter, severity)

    if not events:
        click.echo(click.style("No matching events found.", fg="yellow"))
        return

    # Show only the last N after filtering
    events = events[-last:]

    for ev in events:
        if as_json:
            click.echo(ev.to_jsonl())
        else:
            click.echo(format_event(ev))


def _follow_log(
    source_filter: str | None,
    severity: str | None,
    as_json: bool,
) -> None:
    """Tail the log directory, printing new events as they appear.

    Delegates to :func:`_iter_tail_events` for the rotation-aware read
    path so this function stays a thin presentation shell.
    """
    cfg = BlackBoxConfig.load()
    log_dir = Path(os.path.expanduser(cfg.log_dir))
    if not log_dir.is_dir():
        click.echo(click.style(f"Log directory not found: {log_dir}", fg="red"))
        raise SystemExit(1)

    target = _latest_log_file_in(log_dir)
    if target is None:
        click.echo(click.style("No log files found.", fg="yellow"))
        raise SystemExit(1)

    click.echo(
        click.style(
            f"Following {log_dir} (current file: {target.name}, Ctrl+C to stop)",
            fg="bright_black",
        )
    )

    try:
        for ev in _iter_tail_events(log_dir):
            if source_filter and ev.source != source_filter:
                continue
            if severity and ev.severity != severity:
                continue
            if as_json:
                click.echo(ev.to_jsonl())
            else:
                click.echo(format_event(ev))
    except KeyboardInterrupt:
        click.echo()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("log_file", type=click.Path(exists=True), required=False)
def replay(log_file: str | None) -> None:
    """Replay a log file for debugging.

    If no LOG_FILE is given, the most recent log in the default log
    directory is used.
    """
    if log_file:
        path = Path(log_file)
    else:
        path = _latest_log_file()
        if path is None:
            click.echo(click.style("No log files found.", fg="yellow"))
            raise SystemExit(1)
        click.echo(click.style(f"Replaying {path.name}", fg="bright_black"))

    events: list[BlackBoxEvent] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(BlackBoxEvent.from_jsonl(line))
            except Exception as exc:
                click.echo(
                    click.style(f"Skipped line {lineno}: {exc}", fg="bright_black")
                )

    if not events:
        click.echo(click.style("Log file is empty.", fg="yellow"))
        return

    click.echo(
        click.style(
            f"Replaying {len(events)} events "
            f"({events[0].timestamp.isoformat()} -> {events[-1].timestamp.isoformat()})",
            fg="cyan",
        )
    )
    click.echo()

    for ev in events:
        click.echo(format_event(ev))


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@cli.command("config")
def show_config() -> None:
    """Show the effective configuration as YAML."""
    cfg = BlackBoxConfig.load()
    click.echo(
        yaml.dump(
            asdict(cfg),
            default_flow_style=False,
            sort_keys=False,
        )
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


_DEFAULT_CONFIG_YAML = """\
# BlackBoxRS configuration
# See documentation for all available options.

log_dir: "~/.blackboxrs/logs"
log_rotation_mb: 50
log_max_files: 20

# Bounded capacity (in events) of every subscriber queue on the event
# bus.  Full queues drop events instead of back-pressuring producers.
event_bus_queue_maxsize: 1024

ros_monitor:
  enabled: true
  poll_interval_sec: 1.0
  track_latency: true
  topic_filters: []

system_monitor:
  enabled: true
  interval_sec: 1.0
  gpu_backend: "auto"

anomaly_engine:
  enabled: true
  thresholds:
    cpu_percent: 90.0
    memory_percent: 85.0
    gpu_temp_c: 80.0
  frequency:
    tolerance_percent: 20.0
  dead_topic:
    timeout_sec: 5.0

rosbag2:
  enabled: false
  output_dir: "~/.blackboxrs/bags"
  record_duration_sec: 30.0
  cooldown_sec: 60.0
  executable: "ros2"
  storage_id: "sqlite3"
  max_recordings_per_run: 10
  trigger_event_types:
    - "anomaly.threshold"
    - "anomaly.frequency"
    - "anomaly.dead_topic"
    - "anomaly.qos_mismatch"
  topics: []
"""


@cli.command()
def init() -> None:
    """Initialise the BlackBoxRS configuration directory.

    Creates ``~/.blackboxrs/`` with a default ``config.yaml`` and a
    ``logs/`` subdirectory.  Existing files are not overwritten.
    """
    base = Path("~/.blackboxrs").expanduser()
    config_file = base / "config.yaml"
    logs_dir = base / "logs"

    base.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if config_file.exists():
        click.echo(
            click.style(f"Config already exists: {config_file}", fg="yellow")
        )
    else:
        config_file.write_text(_DEFAULT_CONFIG_YAML, encoding="utf-8")
        click.echo(
            click.style(f"Created config: {config_file}", fg="green")
        )

    click.echo(click.style(f"Log directory:  {logs_dir}", fg="green"))
    click.echo(
        click.style(
            "BlackBoxRS initialised. Edit config.yaml to customise, "
            "then run 'robot-blackbox start'.",
            fg="bright_black",
        )
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_config(config_path: str | None) -> BlackBoxConfig:
    """Load configuration, printing an error and exiting on failure."""
    try:
        if config_path:
            return BlackBoxConfig.load(Path(config_path))
        return BlackBoxConfig.load()
    except Exception as exc:
        click.echo(click.style(f"Failed to load configuration: {exc}", fg="red"))
        raise SystemExit(1)


def _read_recent_events(count: int = 100) -> list[BlackBoxEvent]:
    """Return the last *count* events from the configured log directory.

    Streams each log file line-by-line through a bounded ``deque``
    (via :class:`LogReader.tail`) so very large logs do not balloon
    memory -- a previous implementation called ``read_text()`` on each
    file, which pulled the entire file into a string before splitting.
    """
    cfg = BlackBoxConfig.load()
    log_dir = Path(os.path.expanduser(cfg.log_dir))
    if not log_dir.is_dir():
        return []
    return LogReader(log_dir).tail(n=count)


def _apply_filters(
    events: list[BlackBoxEvent],
    source: str | None,
    severity: str | None,
) -> list[BlackBoxEvent]:
    """Filter events by source and/or severity."""
    filtered = events
    if source:
        filtered = [e for e in filtered if e.source == source]
    if severity:
        filtered = [e for e in filtered if e.severity == severity]
    return filtered


def _latest_log_file_in(log_dir: Path) -> Path | None:
    """Return the most recent BlackBoxRS log file in *log_dir*, or ``None``.

    Uses the exact glob that :class:`RotatingJsonlWriter` writes to, so
    stray ``.jsonl`` files dropped in the directory by other tools are
    ignored.  Filename ordering matches chronological ordering because
    every name embeds a sortable UTC timestamp.
    """
    if not log_dir.is_dir():
        return None
    log_files = sorted(log_dir.glob(_LOG_GLOB))
    return log_files[-1] if log_files else None


def _latest_log_file() -> Path | None:
    """Return the path to the most recent log file, or ``None``."""
    cfg = BlackBoxConfig.load()
    log_dir = Path(os.path.expanduser(cfg.log_dir))
    return _latest_log_file_in(log_dir)


def _iter_tail_events(
    log_dir: Path,
    *,
    idle_sleep: float = 0.2,
    stop_event: threading.Event | None = None,
) -> Iterator[BlackBoxEvent]:
    """Tail the most recent log file in *log_dir*, following rotations.

    Opens the newest ``blackboxrs_*.jsonl`` file, seeks to EOF, and
    yields every new :class:`BlackBoxEvent` that is appended.  When the
    writer rotates (i.e. a lexicographically newer file appears in the
    directory), the generator transparently switches to the new file
    starting from its beginning — events written to the new file before
    we caught up would otherwise be lost.

    Args:
        log_dir: Directory holding BlackBoxRS log files.
        idle_sleep: Seconds to sleep between poll attempts when the
            current file has reached EOF and no rotation has occurred.
        stop_event: Optional event used to cleanly interrupt the loop
            from another thread.  When set, the generator returns.

    Yields:
        :class:`BlackBoxEvent` instances in file order.  Malformed lines
        are silently skipped.
    """
    current = _latest_log_file_in(log_dir)
    if current is None:
        return

    fh: TextIO = open(current, "r", encoding="utf-8")
    try:
        fh.seek(0, 2)  # seek to EOF

        while stop_event is None or not stop_event.is_set():
            line = fh.readline()
            if line:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield BlackBoxEvent.from_jsonl(stripped)
                except Exception:
                    continue
                continue

            # EOF reached.  Check for rotation before sleeping.
            newest = _latest_log_file_in(log_dir)
            if newest is not None and newest != current:
                try:
                    fh.close()
                except OSError:
                    pass
                current = newest
                fh = open(current, "r", encoding="utf-8")
                # Read from the start of the new file — we don't want
                # to miss events that were written between rotation and
                # the moment we noticed.
                continue

            time.sleep(idle_sleep)
    finally:
        try:
            fh.close()
        except OSError:
            pass
