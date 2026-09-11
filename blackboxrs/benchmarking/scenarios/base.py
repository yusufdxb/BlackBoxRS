"""Scenario interface for the reliability benchmark."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from blackboxrs.benchmarking.schema import ScenarioInput, ScenarioSpec
from blackboxrs.core.config import BlackBoxConfig


class BenchmarkScenario(Protocol):
    """Concrete scenario contract."""

    spec: ScenarioSpec

    def configure(self, config: BlackBoxConfig) -> BlackBoxConfig:
        """Return scenario-specific configuration."""

    def materialize(
        self,
        work_dir: Path,
        *,
        repetition: int,
        seed: int,
    ) -> ScenarioInput:
        """Create deterministic input for one repetition."""
