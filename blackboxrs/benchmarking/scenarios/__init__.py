"""Built-in benchmark scenarios."""

from __future__ import annotations

from collections.abc import Iterable

from .base import BenchmarkScenario
from .builtin import SCENARIOS


def iter_scenarios(*, include_unsupported: bool = False) -> Iterable[BenchmarkScenario]:
    """Yield built-in scenarios in deterministic order."""
    for scenario in SCENARIOS:
        if scenario.spec.status == "unsupported" and not include_unsupported:
            continue
        yield scenario


def get_scenario(scenario_id: str) -> BenchmarkScenario:
    """Return a scenario by id."""
    for scenario in SCENARIOS:
        if scenario.spec.scenario_id == scenario_id:
            return scenario
    raise KeyError(f"unknown benchmark scenario: {scenario_id}")


__all__ = ["BenchmarkScenario", "get_scenario", "iter_scenarios"]
