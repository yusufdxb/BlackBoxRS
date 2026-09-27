"""``robot-blackbox lab ...``: Replay Lab, deterministic replay and fault injection.

    robot-blackbox lab replay <case.json | bundle>   replay one incident, print the verdict
    robot-blackbox lab verify [cases...]            run golden cases, check expectations
    robot-blackbox lab faults                       list the fault injectors

``lab replay`` exits with the verdict: 0 PASS, 1 FAIL, 3 INCOMPLETE,
4 DETECTED, 5 for malformed evidence, case or fault definitions, 6 for an
internal error (click's own usage errors are 2). ``lab verify`` exits 0 only when every case matched its
expectation and replayed identically on every repeat.

No ROS installation or robot is needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

DEFAULT_CASES = Path("examples") / "replay_lab" / "cases"


@click.group("lab")
def lab_group() -> None:
    """Replay Lab: deterministic incident replay and fault injection (no robot needed)."""


def _fail(msg: str) -> None:
    from blackboxrs.lab.verdict import EXIT_ERROR
    click.echo(click.style(f"error: {msg}", fg="red"), err=True)
    raise SystemExit(EXIT_ERROR)


def _internal(exc: BaseException) -> None:
    import traceback

    from blackboxrs.lab.verdict import EXIT_INTERNAL
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    click.echo(click.style(f"internal error (not a verdict): {type(exc).__name__}: {exc}",
                           fg="red"), err=True)
    raise SystemExit(EXIT_INTERNAL)


def _load_faults(path: str | None, inject: tuple[str, ...], start: int) -> list[Any]:
    from blackboxrs.lab.faults import parse_cli_fault, parse_fault
    out = []
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _fail(f"--faults {path}: {exc}")
        if not isinstance(raw, list):
            _fail(f"--faults {path}: expected a JSON list of fault objects")
        out += [parse_fault(f, start + i) for i, f in enumerate(raw)]
    out += [parse_cli_fault(t, start + len(out) + i) for i, t in enumerate(inject)]
    return out


def _parse_set(items: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep:
            _fail(f"--set expects key=value, got {item!r}")
        try:
            out[k.strip()] = json.loads(v)
        except ValueError:
            out[k.strip()] = v.strip()
    return out


@lab_group.command("replay")
@click.argument("target", type=click.Path(exists=True))
@click.option("--sut", type=click.Choice(["helix_arbiter", "twist_mux_legacy", "observed"]),
              default=None, help="Arbitration path to run. Default: the case's, or helix_arbiter "
                                 "for a bare bundle.")
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE",
              help="Override a reference-model setting, e.g. freshness_clock=source_timestamp.")
@click.option("--inject", multiple=True, metavar="SPEC",
              help="Add a fault: kind:key=value,... or a JSON object. Repeatable. "
                   "See `robot-blackbox lab faults`.")
@click.option("--faults", "faults_file", type=click.Path(exists=True), default=None,
              help="JSON file with a list of fault objects.")
@click.option("--from", "from_s", type=float, default=None,
              help="Replay from this time (s from evidence start).")
@click.option("--to", "to_s", type=float, default=None, help="Replay up to this time (s).")
@click.option("--speed", type=float, default=None,
              help="Pace the replay on the wall clock (1.0 = real time). Results are "
                   "identical to an unpaced replay.")
@click.option("--step", is_flag=True,
              help="Pause at every causal-timeline entry (Enter: next, q: run to the end).")
@click.option("--json", "json_out", default=None, metavar="PATH",
              help="Write the machine-readable result (canonical JSON); '-' for stdout.")
@click.option("--timeline/--no-timeline", default=True, show_default=True,
              help="Print the causal timeline.")
@click.option("--allow-partial", is_flag=True,
              help="Replay a torn or unfinalized bundle; the verdict can then be at best "
                   "INCOMPLETE.")
def lab_replay(target, sut, sets, inject, faults_file, from_s, to_s, speed, step, json_out,
               timeline, allow_partial) -> None:
    """Replay a case file or a flight bundle and print the causal timeline and verdict."""
    from dataclasses import replace

    from blackboxrs.lab.case import CaseError, is_case_file, load_case
    from blackboxrs.lab.clock import Pacer, RealtimePacer
    from blackboxrs.lab.engine import ReplayConfig, replay
    from blackboxrs.lab.evidence import EvidenceError, load_evidence
    from blackboxrs.lab.faults import FaultError
    from blackboxrs.lab.render import render_text, timeline_line
    from blackboxrs.lab.values import canonical_json
    from blackboxrs.lab.verdict import EXIT_CODES, check_expectations

    try:
        case = load_case(target) if is_case_file(target) else None
        cfg = case.config if case else ReplayConfig()
        evidence_path = case.evidence if case else Path(target)
        extra = _load_faults(faults_file, inject, len(cfg.faults))
        changes: dict[str, Any] = {}
        if sut == "observed":
            changes.update(sut_mode="observed", overrides={})
        elif sut:
            changes.update(sut_mode="reference", preset=sut)
        if sets:
            changes["overrides"] = {**(changes.get("overrides", cfg.overrides)),
                                    **_parse_set(sets)}
        if extra:
            changes["faults"] = cfg.faults + tuple(extra)
        if from_s is not None:
            changes["from_s"] = from_s
        if to_s is not None:
            changes["to_s"] = to_s
        cfg = replace(cfg, **changes)
        ev = load_evidence(evidence_path, allow_partial=allow_partial,
                           label=str(evidence_path))
        pacer = RealtimePacer(speed) if speed else Pacer()
        observer = None
        if step:
            state = {"on": True}

            def observer(entry: dict[str, Any]) -> None:
                if not state["on"]:
                    return
                click.echo(timeline_line(entry))
                if click.prompt("", default="", show_default=False,
                                prompt_suffix="").strip().lower() == "q":
                    state["on"] = False
        result = replay(ev, cfg, pacer=pacer, observer=observer)
    except (CaseError, EvidenceError, FaultError, ValueError) as exc:
        _fail(str(exc))
    except Exception as exc:  # a bug must never read as a verdict
        _internal(exc)
    text = canonical_json(result)
    if json_out == "-":
        click.echo(text)
    else:
        click.echo(render_text(result, timeline=timeline and not step))
        if json_out:
            Path(json_out).write_text(text + "\n", encoding="utf-8")
            click.echo(f"result written to {json_out}")
    if case and case.expect and json_out != "-":
        if changes:
            click.echo("expectations: not checked (the case was modified on the command line)")
        else:
            mm = check_expectations(result, case.expect)
            click.echo("expectations: " + ("match" if not mm else "MISMATCH\n  "
                                           + "\n  ".join(mm)))
    sys.exit(EXIT_CODES[result["verdict"]["result"]])


def _case_files(paths: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in (paths or (str(DEFAULT_CASES),)):
        pp = Path(p)
        if pp.is_dir():
            out += sorted(pp.glob("*.json"))
        elif pp.is_file():
            out.append(pp)
        else:
            _fail(f"{p}: no such case file or directory")
    if not out:
        _fail("no case files found")
    return out


@lab_group.command("verify")
@click.argument("paths", nargs=-1)
@click.option("--repeat", type=int, default=2, show_default=True,
              help="Replay every case this many times and require identical results.")
@click.option("--json", "json_out", default=None, metavar="PATH",
              help="Write a per-case summary (canonical JSON); '-' for stdout.")
def lab_verify(paths, repeat, json_out) -> None:
    """Run golden cases (default: examples/replay_lab/cases) and check every expectation."""
    from blackboxrs.lab.case import CaseError, load_case
    from blackboxrs.lab.engine import replay
    from blackboxrs.lab.evidence import load_evidence
    from blackboxrs.lab.values import canonical_json, digest
    from blackboxrs.lab.verdict import check_expectations

    if repeat < 1:
        _fail("--repeat must be >= 1")
    rows = []
    ok_all = True
    for f in _case_files(paths):
        try:
            case = load_case(f)
            if not case.expect:
                raise CaseError(f"{f}: case has no expect block")
            ev = load_evidence(case.evidence, label=str(case.evidence))
            runs = [canonical_json(replay(ev, case.config)) for _ in range(repeat)]
        except Exception as exc:  # any error fails the case; it is never a pass
            rows.append({"case": str(f), "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            ok_all = False
            click.echo(f"{click.style('ERROR', fg='red')}  {f}: {exc}")
            continue
        result = json.loads(runs[0])
        mm = check_expectations(result, case.expect)
        stable = len(set(runs)) == 1
        ok = not mm and stable
        ok_all &= ok
        rows.append({"case": case.name, "ok": ok, "verdict": result["verdict"]["result"],
                     "mismatches": mm, "deterministic": stable, "repeats": repeat,
                     "result_sha256": digest(result)})
        status = click.style("ok   ", fg="green") if ok else click.style("FAIL ", fg="red")
        click.echo(f"{status} {case.name:<44} {result['verdict']['result']:<10} "
                   f"{'deterministic' if stable else 'NONDETERMINISTIC'} x{repeat}  "
                   f"{digest(result)[:12]}")
        for m in mm:
            click.echo(f"        {m}")
    click.echo(f"{sum(r['ok'] for r in rows)}/{len(rows)} cases ok")
    if json_out:
        text = canonical_json({"cases": rows, "all_ok": ok_all})
        if json_out == "-":
            click.echo(text)
        else:
            Path(json_out).write_text(text + "\n", encoding="utf-8")
    sys.exit(0 if ok_all else 1)


@lab_group.command("faults")
def lab_faults() -> None:
    """List the fault injectors and their parameters."""
    from blackboxrs.lab.faults import _REQUIRED, KINDS
    for cat in ("transport", "data", "control"):
        click.echo(click.style(cat, bold=True))
        for name, spec in sorted(KINDS.items()):
            if spec.category != cat:
                continue
            click.echo(f"  {name:<17} {spec.summary}")
            for pname, p in spec.params.items():
                default = "required" if p.default is _REQUIRED else f"default {p.default!r}"
                click.echo(f"      {pname:<12} {p.kind:<6} {default:<22} {p.help}")
