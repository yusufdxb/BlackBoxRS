"""CLI subcommands for the incident intelligence pivot.

These commands are additive: existing ``robot-blackbox start / stop /
status / dump-log / replay / config / init`` continue to work
unchanged. The new top-level group is ``incident``.

Synopsis::

    robot-blackbox incident build [--since 10m | --start ISO --end ISO]
    robot-blackbox incident show <bundle>
    robot-blackbox incident list
    robot-blackbox incident attach <bundle> <path> [--copy]
    robot-blackbox preflight [--rules-dir]
    robot-blackbox prevention adopt --from-incident <id> [--rules-dir]
    robot-blackbox prevention list [--rules-dir]
"""

from __future__ import annotations

import os
import re
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import yaml

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident, render_report
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.prevention.derivation import (
    PreventionDerivationError,
    derive_rule_from_bundle,
    derive_telemetry_health_rule,
)
from blackboxrs.prevention.rules import (
    load_rule,
    load_rules,
    save_rule,
)
from blackboxrs.prevention.runner import PreflightRunner
from blackboxrs.prevention.telemetry_guard import (
    begin_guard_invocation,
    fail_guard_invocation,
    refuse_guard_invocation,
    run_ros_telemetry_guard,
)


_DEFAULT_INCIDENTS_DIR = Path("~/.blackboxrs/incidents").expanduser()
_DEFAULT_RULES_DIR = Path("~/.blackboxrs/prevention/rules").expanduser()


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd])$")


def _parse_duration(spec: str) -> timedelta:
    """Parse short durations like ``10m``, ``2h``, ``45s``, ``1d``."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise click.BadParameter(
            f"duration must be NUMBER + s|m|h|d, got {spec!r}"
        )
    value = float(m.group("value"))
    unit = m.group("unit")
    return {
        "s": timedelta(seconds=value),
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
    }[unit]


def _parse_iso(value: str) -> datetime:
    """Parse ISO-8601 timestamp, accept Z and naive (assume UTC)."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise click.BadParameter(f"not a valid ISO-8601 datetime: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# `incident` group
# ---------------------------------------------------------------------------


@click.group("incident")
def incident_group() -> None:
    """Build, show, and manage incident bundles."""


@incident_group.command("build")
@click.option(
    "--since",
    "since_spec",
    default=None,
    help="Window length ending at 'now' (e.g. 10m, 2h).",
)
@click.option("--start", "start_iso", default=None, help="Window start (ISO 8601).")
@click.option("--end", "end_iso", default=None, help="Window end (ISO 8601).")
@click.option("--note", "note", default=None, help="Optional note attached to the bundle.")
@click.option("--tag", "tags", multiple=True, help="Tag (repeatable).")
@click.option("--title", "title", default=None, help="Override the auto-generated title.")
@click.option(
    "--incidents-dir",
    "incidents_dir",
    default=None,
    type=click.Path(file_okay=False),
    help=f"Output root (default: {_DEFAULT_INCIDENTS_DIR}).",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=False),
    default=None,
    help="Path to a YAML configuration file (default: ~/.blackboxrs/config.yaml).",
)
@click.option(
    "--log-dir",
    "log_dir_override",
    type=click.Path(file_okay=False),
    default=None,
    help="Override the log directory the builder reads events from.",
)
def incident_build(
    since_spec: str | None,
    start_iso: str | None,
    end_iso: str | None,
    note: str | None,
    tags: tuple[str, ...],
    title: str | None,
    incidents_dir: str | None,
    config_path: str | None,
    log_dir_override: str | None,
) -> None:
    """Build an incident bundle from the current log directory."""
    if since_spec and (start_iso or end_iso):
        raise click.BadParameter("--since cannot be combined with --start/--end")

    if since_spec:
        end = datetime.now(timezone.utc)
        start = end - _parse_duration(since_spec)
    elif start_iso or end_iso:
        if not (start_iso and end_iso):
            raise click.BadParameter("--start and --end must both be provided")
        start = _parse_iso(start_iso)
        end = _parse_iso(end_iso)
    else:
        # Default: last 5 minutes.
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=5)

    cfg = (
        BlackBoxConfig.load(Path(config_path).expanduser())
        if config_path
        else BlackBoxConfig.load()
    )
    if log_dir_override:
        cfg.log_dir = str(Path(log_dir_override).expanduser())
    out_dir = Path(incidents_dir).expanduser() if incidents_dir else None

    bundle_path = build_incident(
        start, end,
        config=cfg,
        incidents_dir=out_dir,
        title=title,
        notes=note,
        tags=tags,
    )

    click.echo(click.style(f"Built bundle: {bundle_path}", fg="green"))
    click.echo(f"  report: {bundle_path}/report.md")


@incident_group.command("show")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
def incident_show(bundle: str) -> None:
    """Render report.md for a bundle to stdout (re-rendered)."""
    text = render_report(Path(bundle))
    click.echo(text)


@incident_group.command("list")
@click.option(
    "--incidents-dir",
    "incidents_dir",
    default=None,
    type=click.Path(file_okay=False),
)
def incident_list(incidents_dir: str | None) -> None:
    """List bundles in the incidents directory."""
    root = Path(incidents_dir).expanduser() if incidents_dir else _DEFAULT_INCIDENTS_DIR
    if not root.is_dir():
        click.echo(click.style(f"No incidents directory at {root}", fg="yellow"))
        return

    bundles = sorted(p for p in root.iterdir() if p.is_dir())
    if not bundles:
        click.echo(click.style("No incidents found.", fg="yellow"))
        return

    for path in bundles:
        try:
            reader = BundleReader(path, strict=False)
            inc = reader.load_incident()
            click.echo(
                f"{inc.incident_id}  severity={inc.severity}  "
                f"window={inc.window_start.isoformat()}.."
                f"{inc.window_end.isoformat()}  title={inc.title!r}"
            )
        except Exception as exc:
            click.echo(f"{path.name}  (unreadable: {exc})")


@incident_group.command("attach")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.argument("path", type=click.Path(exists=True))
@click.option("--copy", "do_copy", is_flag=True, default=False,
              help="Copy the file into the bundle (default: symlink).")
def incident_attach(bundle: str, path: str, do_copy: bool) -> None:
    """Attach a file or directory to a bundle (symlink by default)."""
    src = Path(path).resolve()
    dst_dir = Path(bundle) / "attachments"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        click.echo(click.style(f"{dst} already exists; refusing to overwrite", fg="red"))
        sys.exit(1)
    if do_copy:
        import shutil

        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    else:
        os.symlink(src, dst)
    click.echo(click.style(f"Attached: {dst}", fg="green"))


# ---------------------------------------------------------------------------
# `preflight` command (top-level)
# ---------------------------------------------------------------------------


@click.command("preflight")
@click.option(
    "--rules-dir",
    "rules_dir",
    default=None,
    type=click.Path(file_okay=False),
    help=f"Prevention rules directory (default: {_DEFAULT_RULES_DIR}).",
)
def preflight_cmd(rules_dir: str | None) -> None:
    """Run preflight checks; exit 0 (pass) / 1 (block) / 2 (warn)."""
    rdir = Path(rules_dir).expanduser() if rules_dir else _DEFAULT_RULES_DIR
    rules = load_rules(rdir)
    if not rules:
        click.echo(click.style(
            f"No prevention rules in {rdir}; preflight is a no-op.",
            fg="yellow",
        ))
        sys.exit(0)

    runner = PreflightRunner(rules)
    report = runner.run()
    for r in report.results:
        colour = {
            "pass": "green",
            "warn": "yellow",
            "block": "red",
            "skipped": "bright_black",
            "error": "magenta",
        }.get(r.status, "white")
        click.echo(
            click.style(
                f"[{r.status.upper():>7}] {r.kind:<18} rule={r.rule_id} {r.message}",
                fg=colour,
            )
        )
    sys.exit(report.exit_code)


# ---------------------------------------------------------------------------
# `prevention` group
# ---------------------------------------------------------------------------


@click.group("prevention")
def prevention_group() -> None:
    """Manage prevention rules."""


@prevention_group.command("list")
@click.option("--rules-dir", "rules_dir", default=None,
              type=click.Path(file_okay=False))
def prevention_list(rules_dir: str | None) -> None:
    """List prevention rules in the rules directory."""
    rdir = Path(rules_dir).expanduser() if rules_dir else _DEFAULT_RULES_DIR
    rules = load_rules(rdir)
    if not rules:
        click.echo(click.style(f"No rules in {rdir}", fg="yellow"))
        return
    for r in rules:
        flag = "OFF " if r.disabled else "ON  "
        click.echo(
            f"{flag}{r.rule_id}  kind={r.check.kind}  name={r.check.name!r}  "
            f"severity={r.check.severity_on_fail}"
        )


@prevention_group.command("adopt")
@click.option("--from-incident", "incident_dir", required=True,
              type=click.Path(exists=True, file_okay=False),
              help="Bundle directory to derive a rule from.")
@click.option("--rules-dir", "rules_dir", default=None,
              type=click.Path(file_okay=False))
def prevention_adopt(incident_dir: str, rules_dir: str | None) -> None:
    """Adopt the recommended preflight rule from an incident bundle."""
    rdir = Path(rules_dir).expanduser() if rules_dir else _DEFAULT_RULES_DIR
    reader = BundleReader(Path(incident_dir))
    try:
        derivation = derive_rule_from_bundle(reader)
    except PreventionDerivationError as exc:
        click.echo(click.style(str(exc), fg="yellow"))
        sys.exit(1)

    target = save_rule(derivation.rule, rdir)
    click.echo(click.style(f"Adopted: {target}", fg="green"))


@prevention_group.command("adopt-telemetry-health")
@click.option("--from-incident", "incident_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--healthy-evidence", "evidence_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--rules-dir", "rules_dir", default=None,
              type=click.Path(file_okay=False, path_type=Path))
def prevention_adopt_telemetry_health(
    incident_dir: Path, evidence_path: Path, rules_dir: Path | None
) -> None:
    """Adopt a bounded runtime-health rule for one dead topic."""
    rdir = rules_dir.expanduser() if rules_dir else _DEFAULT_RULES_DIR
    reader = BundleReader(incident_dir, strict=False)
    try:
        derivation = derive_telemetry_health_rule(reader, evidence_path)
    except PreventionDerivationError as exc:
        click.echo(click.style(str(exc), fg="yellow"))
        sys.exit(1)
    target = save_rule(derivation.rule, rdir)
    click.echo(click.style(f"Adopted: {target}", fg="green"))
    click.echo(f"  derived: {derivation.reason}")
    click.echo(f"  trigger: {derivation.source_trigger.trigger_id}")
    click.echo(f"  fingerprint: {derivation.rule.rule_fingerprint}")


@prevention_group.command("guard", context_settings={"ignore_unknown_options": True})
@click.option("--rule", "rule_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--result", "result_path", default=None,
              type=click.Path(dir_okay=False, path_type=Path))
@click.option("--monitor-duration", type=float, default=None)
@click.option(
    "--context-label",
    "--context",
    "declared_context_label",
    required=True,
    help="Declared context label, not deployment identity attestation.",
)
@click.option("--trusted-rule-fingerprint", required=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def prevention_guard(
    rule_path: Path, result_path: Path | None, monitor_duration: float | None,
    declared_context_label: str, trusted_rule_fingerprint: str,
    command: tuple[str, ...],
) -> None:
    """Hold COMMAND until healthy, then supervise its health."""
    invocation = begin_guard_invocation(
        result_path,
        requested_topic=_requested_topic_from_rule(rule_path),
        declared_context_label=declared_context_label,
    )
    try:
        result = run_ros_telemetry_guard(
            load_rule(rule_path), command,
            monitor_duration_sec=monitor_duration, result_path=result_path,
            declared_context_label=declared_context_label,
            trusted_rule_fingerprint=trusted_rule_fingerprint,
            invocation=invocation,
        )
    except ValueError as exc:
        if not invocation.terminal_written:
            refuse_guard_invocation(invocation, reason=str(exc))
        click.echo(click.style(f"Telemetry guard refused: {exc}", fg="red"))
        sys.exit(1)
    except (OSError, RuntimeError) as exc:
        if not invocation.terminal_written:
            fail_guard_invocation(
                invocation,
                error_category=type(exc).__name__,
            )
        click.echo(click.style(f"Telemetry guard failed: {exc}", fg="red"))
        sys.exit(1)
    click.echo(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    sys.exit(0 if result.status == "passed" else 1)


def _requested_topic_from_rule(rule_path: Path) -> str:
    """Read only the requested topic for the pre-validation lifecycle record."""
    try:
        raw = yaml.safe_load(Path(rule_path).read_text(encoding="utf-8"))
        topic = raw["check"]["params"]["topic"]
    except (KeyError, OSError, TypeError, yaml.YAMLError):
        return "<unavailable>"
    return str(topic)


__all__ = [
    "incident_group",
    "preflight_cmd",
    "prevention_group",
]
