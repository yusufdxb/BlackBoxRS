"""``robot-blackbox flight ...``: the passive GO2 flight recorder.

    robot-blackbox flight preflight --profile go2      zero-motion GO / NO-GO
    robot-blackbox flight record    --profile go2      capture until Ctrl-C
    robot-blackbox flight mark      "note"             manual marker (file drop)
    robot-blackbox flight replay    <bundle>           regenerate + verify report
    robot-blackbox flight rehearse                     offline synthetic rehearsal
    robot-blackbox flight show      <bundle>           print report.md

Nothing here publishes on a ROS topic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

_PROFILE_OPT = click.option("--profile", "profile_name", default="go2", show_default=True,
                            help="Built-in profile name or path to a profile YAML.")
_EVIDENCE_OPT = click.option("--evidence-dir", default=None,
                             help="Override the profile's evidence directory.")
_EXCLUDE_OPT = click.option(
    "--exclude-topic", "exclude", multiple=True,
    help="Drop a profile topic for this run (e.g. /lowstate if preflight reports the "
         "recorder load too high). Recorded in the session. Repeatable.")


def _load(profile_name: str, evidence_dir: str | None, exclude: tuple[str, ...] = ()):
    from blackboxrs.flight.profile import ProfileError, load_profile
    try:
        prof = load_profile(profile_name, evidence_dir=evidence_dir)
        return prof.without_topics(exclude) if exclude else prof
    except ProfileError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("flight")
def flight_group() -> None:
    """Passive flight recorder for GO2 experiments (observation only)."""


def run_flight_preflight(profile_name: str, evidence_dir: str | None, listen: float | None,
                         experiment: str | None, repos: tuple[str, ...], as_json: bool,
                         keep_selftest: bool = False, exclude: tuple[str, ...] = ()) -> None:
    from blackboxrs.flight.preflight import format_result, run_preflight
    prof = _load(profile_name, evidence_dir, exclude)
    result = run_preflight(prof, listen_sec=listen, experiment=experiment,
                           experiment_repos=list(repos), keep_selftest=keep_selftest,
                           excluded_topics=list(exclude))
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        colour = {"GO": "green", "GO WITH WARNINGS": "yellow", "NO-GO": "red"}[result["verdict"]]
        text = format_result(result)
        click.echo(text.replace(f"VERDICT: {result['verdict']}",
                                click.style(f"VERDICT: {result['verdict']}", fg=colour,
                                            bold=True)))
    sys.exit({"GO": 0, "GO WITH WARNINGS": 2, "NO-GO": 1}[result["verdict"]])


@flight_group.command("preflight")
@_PROFILE_OPT
@_EVIDENCE_OPT
@click.option("--listen", type=float, default=None, help="Seconds to listen (profile default).")
@click.option("--experiment", default=None, help="Experiment label stored with the result.")
@click.option("--experiment-repo", "repos", multiple=True,
              help="Repo whose SHA/dirty state to record (read-only git query). Repeatable.")
@click.option("--json", "as_json", is_flag=True, help="Print the full JSON result.")
@click.option("--keep-selftest", is_flag=True, help="Keep the self-test bundle on disk.")
@_EXCLUDE_OPT
def flight_preflight(profile_name, evidence_dir, listen, experiment, repos, as_json,
                     keep_selftest, exclude) -> None:
    """Zero-motion preflight: GO / GO WITH WARNINGS / NO-GO (exit 0 / 2 / 1)."""
    run_flight_preflight(profile_name, evidence_dir, listen, experiment, repos, as_json,
                         keep_selftest, exclude)


@flight_group.command("record")
@_PROFILE_OPT
@_EVIDENCE_OPT
@click.option("--experiment", default=None, help="Experiment label, e.g. helix-stage-E.")
@click.option("--session", "session_id", default=None, help="Session id (default: generated).")
@click.option("--experiment-repo", "repos", multiple=True,
              help="Repo whose SHA/dirty state to record (read-only git query). Repeatable.")
@click.option("--duration", type=float, default=None, help="Stop after N seconds.")
@click.option("--gpu", type=click.Choice(["auto", "jetson", "nvidia-smi", "none"]),
              default="auto", show_default=True)
@click.option("--synthetic", is_flag=True,
              help="Mark every bundle SYNTHETIC (rehearsal traffic, not hardware evidence).")
@_EXCLUDE_OPT
def flight_record(profile_name, evidence_dir, experiment, session_id, repos, duration,
                  gpu, synthetic, exclude) -> None:
    """Record until Ctrl-C (or --duration). SIGUSR1 or `flight mark` drops a marker."""
    from blackboxrs.flight.provenance import build_session
    from blackboxrs.flight.recorder import control_dir, run_recorder
    prof = _load(profile_name, evidence_dir, exclude)
    session = build_session(prof, experiment=experiment, session_id=session_id,
                            experiment_repos=list(repos), synthetic=synthetic)
    session["excluded_topics"] = list(exclude)
    sdir = prof.evidence_path / session["session_id"]
    click.echo(f"BlackBoxRS flight recorder (observation only), profile {prof.name} "
               f"sha256 {prof.sha256[:12]}")
    click.echo(f"  session   {session['session_id']}")
    click.echo(f"  evidence  {sdir}")
    click.echo(f"  window    {prof.buffer.pre_trigger_sec} s before / "
               f"{prof.buffer.post_trigger_sec} s after each trigger")
    click.echo(f"  markers   robot-blackbox flight mark \"note\"  (drops into {control_dir(prof)})")
    click.echo("  stop      Ctrl-C (an open incident is finalized as 'interrupted')")
    bundles = run_recorder(prof, session, duration_s=duration, gpu=gpu)
    click.echo(f"stopped; {len(bundles)} bundle(s):")
    for b in bundles:
        click.echo(f"  {b}")


@flight_group.command("mark")
@click.argument("note", required=False, default="operator marker")
@_PROFILE_OPT
@_EVIDENCE_OPT
def flight_mark(note, profile_name, evidence_dir) -> None:
    """Ask the running recorder to drop a marker (and open a bundle if armed)."""
    from blackboxrs.flight.recorder import request_marker
    p = request_marker(_load(profile_name, evidence_dir), note)
    click.echo(f"marker requested: {p}")


@flight_group.command("replay")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.option("--write", is_flag=True, help="Write report.replay.json/.md next to the bundle.")
@click.option("--retrigger", is_flag=True,
              help="Also re-run the trigger logic on the recorded stream.")
def flight_replay(bundle, write, retrigger) -> None:
    """Regenerate the report offline and compare it with the stored one."""
    from blackboxrs.flight.render import render_markdown
    from blackboxrs.flight.replay import regenerate, retrigger as do_retrigger
    res = regenerate(bundle)
    rep = res["report"]
    info = res["read_info"]
    click.echo(f"bundle {bundle}: status {rep.get('status')}, {rep['window']['records']} records"
               f", torn lines {info['torn_lines']}")
    if res["stored_report"]:
        click.echo("report: IDENTICAL to stored" if res["identical"] else
                   f"report: DIFFERS in {res['differs_in']}")
    else:
        click.echo("report: no stored report (unfinalized bundle); regenerated")
    code = 0 if (res["identical"] or not res["stored_report"]) else 1
    if write:
        (Path(bundle) / "report.replay.json").write_text(
            json.dumps(rep, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        (Path(bundle) / "report.replay.md").write_text(render_markdown(rep), encoding="utf-8")
    if retrigger:
        rt = do_retrigger(bundle)
        click.echo(f"retrigger: {'reproduced' if rt.get('ok') else 'NOT reproduced'} "
                   f"primary {rt.get('recorded_primary')}; replay fired "
                   f"{[r['type'] for r in rt.get('replayed', [])]}")
        code = code or (0 if rt.get("ok") else 1)
    sys.exit(code)


@flight_group.command("show")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
def flight_show(bundle) -> None:
    """Print a bundle's report.md (regenerating it for unfinalized bundles)."""
    md = Path(bundle) / "report.md"
    if md.exists():
        click.echo(md.read_text(encoding="utf-8"))
        return
    from blackboxrs.flight.render import render_markdown
    from blackboxrs.flight.replay import regenerate
    click.echo(render_markdown(regenerate(bundle)["report"]))


@flight_group.command("rehearse")
@_PROFILE_OPT
@click.option("--out", "out_dir", default=None,
              help="Output directory (default: <evidence_dir>/rehearsal).")
@click.option("--scenario", default="stopmove", show_default=True,
              help="Scenario name, or 'all' for every fault fixture.")
def flight_rehearse(profile_name, out_dir, scenario) -> None:
    """Offline rehearsal on SYNTHETIC GO2/HELIX traffic (no ROS, no DDS)."""
    from blackboxrs.flight import synthetic
    from blackboxrs.flight.replay import regenerate
    prof = _load(profile_name, None)
    out = Path(out_dir).expanduser() if out_dir else prof.evidence_path / "rehearsal"
    names = list(synthetic.VARIANTS) if scenario == "all" else [scenario]
    unknown = [n for n in names if n not in synthetic.VARIANTS]
    if unknown:
        raise click.ClickException(f"unknown scenario {unknown}; known: {list(synthetic.VARIANTS)}")
    ok = True
    for name in names:
        _, bundles = synthetic.run(prof, synthetic.scenario(name), out)
        for b in bundles:
            rep = json.loads((Path(b) / "report.json").read_text())
            again = regenerate(b)
            verdicts = {v["id"]: v["result"] for v in rep.get("verdicts", [])}
            chain = [k for k in rep["chain_order"] if rep["chain"][k]["status"] == "observed"]
            click.echo(f"[{name}] {b}")
            click.echo(f"    SYNTHETIC={rep['synthetic']} status={rep['status']} "
                       f"trigger={rep['trigger']['type']} replay_identical={again['identical']}")
            click.echo(f"    chain observed: {' -> '.join(chain) or 'none'}")
            od = rep["motion"]["odometry"]
            click.echo(f"    odometry: {od.get('status')} stop_latency_s={od.get('stop_latency_s')}"
                       f" stop_distance_m={od.get('stop_distance_m')}")
            if verdicts:
                click.echo(f"    verdicts: {verdicts}")
            ok = ok and again["identical"]
        if not bundles:
            click.echo(f"[{name}] no bundle produced")
            ok = False
    sys.exit(0 if ok else 1)
