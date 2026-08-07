"""Benchmark CLI commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from blackboxrs.benchmarking.runner import render_markdown_report, run_benchmark, summarize_results
from blackboxrs.benchmarking.scenarios import iter_scenarios
from blackboxrs.benchmarking.schema import BenchmarkResult


@click.group("benchmark")
def benchmark_group() -> None:
    """Run reproducible local ROS 2 reliability benchmarks."""


@benchmark_group.command("list")
@click.option(
    "--include-unsupported",
    is_flag=True,
    default=False,
    help="Include scenarios documented as unsupported.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def benchmark_list(include_unsupported: bool, as_json: bool) -> None:
    """List benchmark scenarios."""
    scenarios = list(iter_scenarios(include_unsupported=include_unsupported))
    if as_json:
        click.echo(
            json.dumps(
                [scenario.spec.model_dump(mode="json") for scenario in scenarios],
                indent=2,
                sort_keys=True,
            )
        )
        return
    for scenario in scenarios:
        marker = "unsupported" if scenario.spec.status == "unsupported" else "supported"
        click.echo(
            f"{scenario.spec.scenario_id}  {marker}  "
            f"detector={scenario.spec.detector_expected or '-'}"
        )


@benchmark_group.command("run")
@click.option(
    "--scenario",
    "scenario_ids",
    multiple=True,
    help="Scenario id to run. Repeat to select multiple scenarios.",
)
@click.option(
    "--repetitions",
    type=int,
    default=None,
    help="Override scenario repetition counts.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False),
    default="artifacts/blackboxrs_benchmark",
    show_default=True,
)
@click.option(
    "--json-output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Copy raw machine-readable results JSON to this path.",
)
@click.option("--fail-fast", is_flag=True, default=False)
@click.option("--include-unsupported", is_flag=True, default=False)
@click.option("--seed", type=int, default=0, show_default=True)
def benchmark_run(
    scenario_ids: tuple[str, ...],
    repetitions: int | None,
    output_dir: str,
    json_output: str | None,
    fail_fast: bool,
    include_unsupported: bool,
    seed: int,
) -> None:
    """Run benchmark scenarios and write JSON plus Markdown artifacts."""
    out = Path(output_dir).expanduser()
    try:
        results, summary = run_benchmark(
            output_dir=out,
            scenario_ids=list(scenario_ids) or None,
            repetitions=repetitions,
            include_unsupported=include_unsupported,
            fail_fast=fail_fast,
            seed=seed,
            repo_root=Path.cwd(),
        )
    except KeyError as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(2)
    if json_output:
        target = Path(json_output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((out / "raw_results.json").read_text(encoding="utf-8"), encoding="utf-8")
    click.echo(f"results: {out / 'raw_results.json'}")
    click.echo(f"summary: {out / 'summary.json'}")
    click.echo(f"report: {out / 'report.md'}")
    click.echo(
        f"pass={summary.passed} fail={summary.failed} "
        f"error={summary.errors} unsupported={summary.unsupported}"
    )
    if any(not result.passed and result.status != "unsupported" for result in results):
        sys.exit(1)
    sys.exit(0)


@benchmark_group.command("report")
@click.argument("results", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write Markdown report to this path instead of stdout.",
)
def benchmark_report(results: str, output: str | None) -> None:
    """Render a concise Markdown report from raw benchmark results."""
    path = Path(results)
    data = json.loads(path.read_text(encoding="utf-8"))
    parsed = [BenchmarkResult.model_validate(item) for item in data]
    if not parsed:
        click.echo(click.style("No benchmark results in input file.", fg="red"))
        sys.exit(1)
    out_dir = path.parent
    summary = summarize_results(
        parsed,
        output_dir=out_dir,
        environment=parsed[0].environment,
    )
    text = render_markdown_report(parsed, summary)
    if output:
        Path(output).expanduser().write_text(text, encoding="utf-8")
    else:
        click.echo(text)


__all__ = ["benchmark_group"]
