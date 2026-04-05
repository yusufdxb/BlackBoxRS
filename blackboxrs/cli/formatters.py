"""Terminal output formatting for BlackBoxRS CLI.

Provides color-coded, human-readable rendering of events, status
information, and startup banners.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from blackboxrs.core.config import BlackBoxConfig
    from blackboxrs.core.schemas import BlackBoxEvent

# ---------------------------------------------------------------------------
# Severity colour map (click colour names)
# ---------------------------------------------------------------------------

SEVERITY_COLORS: dict[str, str] = {
    "debug": "bright_black",
    "info": "green",
    "warning": "yellow",
    "error": "red",
    "critical": "bright_red",
}

# ---------------------------------------------------------------------------
# Event formatting
# ---------------------------------------------------------------------------


def format_event(event: BlackBoxEvent, colorize: bool = True) -> str:
    """Format a single event for terminal display.

    Output layout::

        [2026-04-05T12:30:00+00:00] [INFO] [ros_monitor] topic_frequency: topic=/cmd_vel frequency_hz=10.0

    Args:
        event: The event to format.
        colorize: Whether to apply ANSI colour codes based on severity.

    Returns:
        A single-line formatted string.
    """
    ts = event.timestamp.isoformat()
    sev = event.severity.upper()
    source = event.source
    etype = event.event_type

    # Build key=value payload string
    pairs = " ".join(f"{k}={v}" for k, v in event.data.items()) if event.data else ""
    line = f"[{ts}] [{sev:8s}] [{source}] {etype}"
    if pairs:
        line += f": {pairs}"

    if colorize:
        colour = SEVERITY_COLORS.get(event.severity, "white")
        return click.style(line, fg=colour)
    return line


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------


def format_status(
    running: bool,
    pid: int | None,
    events: list[BlackBoxEvent],
) -> str:
    """Format daemon status for terminal display.

    Shows whether the daemon is running, its PID, and a summary of the
    most recent events grouped by severity.

    Args:
        running: Whether the daemon process is alive.
        pid: The daemon PID if known, else ``None``.
        events: Recent events to summarise.

    Returns:
        A multi-line formatted status string.
    """
    lines: list[str] = []

    # Header
    if running:
        status_text = click.style("RUNNING", fg="green", bold=True)
        lines.append(f"  Status:  {status_text}  (PID {pid})")
    else:
        status_text = click.style("STOPPED", fg="red", bold=True)
        lines.append(f"  Status:  {status_text}")

    if not events:
        lines.append("  Events:  (none)")
        return "\n".join(lines)

    # Count events by severity
    severity_counts: dict[str, int] = {}
    for ev in events:
        severity_counts[ev.severity] = severity_counts.get(ev.severity, 0) + 1

    counts_str = "  ".join(
        click.style(f"{sev}={count}", fg=SEVERITY_COLORS.get(sev, "white"))
        for sev, count in sorted(severity_counts.items())
    )
    lines.append(f"  Events:  {len(events)} total  ({counts_str})")

    # Count by source
    source_counts: dict[str, int] = {}
    for ev in events:
        source_counts[ev.source] = source_counts.get(ev.source, 0) + 1
    sources_str = "  ".join(f"{src}={count}" for src, count in sorted(source_counts.items()))
    lines.append(f"  Sources: {sources_str}")

    # Latest event
    latest = events[-1]
    lines.append(f"  Latest:  {format_event(latest, colorize=True)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

_BANNER_ART = r"""
  ____  _            _    ____            ____  ____
 | __ )| | __ _  ___| | _| __ )  _____  |  _ \/ ___|
 |  _ \| |/ _` |/ __| |/ /  _ \ / _ \ \/ / |_) \___ \
 | |_) | | (_| | (__|   <| |_) | (_) >  <|  _ < ___) |
 |____/|_|\__,_|\___|_|\_\____/ \___/_/\_\_| \_\____/
"""


def format_banner(session_id: str, config: BlackBoxConfig) -> str:
    """Build the startup banner displayed when the daemon launches.

    Includes an ASCII art header, the session identifier, enabled
    components, and the log directory.

    Args:
        session_id: Unique session identifier for this run.
        config: The active configuration.

    Returns:
        A multi-line banner string.
    """
    lines: list[str] = []
    lines.append(click.style(_BANNER_ART, fg="cyan", bold=True))
    lines.append(click.style("  Flight recorder for ROS 2 robots", fg="bright_black"))
    lines.append("")
    lines.append(f"  Session:    {click.style(session_id, fg='bright_cyan', bold=True)}")

    components: list[str] = []
    if config.ros_monitor.enabled:
        components.append(click.style("ros_monitor", fg="green"))
    if config.system_monitor.enabled:
        components.append(click.style("system_monitor", fg="green"))
    if config.anomaly_engine.enabled:
        components.append(click.style("anomaly_engine", fg="green"))
    components.append(click.style("logging", fg="green"))  # always on

    lines.append(f"  Components: {', '.join(components)}")
    lines.append(f"  Log dir:    {config.log_dir}")
    lines.append("")

    return "\n".join(lines)
