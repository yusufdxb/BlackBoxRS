"""Abstract base class for anomaly detectors.

Every detector receives the full stream of :class:`BlackBoxEvent` instances
and decides whether a particular event warrants raising an anomaly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from blackboxrs.core.schemas import BlackBoxEvent


class BaseDetector(ABC):
    """Abstract base for anomaly detectors.

    Subclasses must implement :attr:`name` (a unique human-readable
    identifier) and :meth:`check` (the detection logic).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return a unique, human-readable identifier for this detector."""

    @abstractmethod
    def check(self, event: BlackBoxEvent) -> BlackBoxEvent | None:
        """Evaluate an event and optionally emit an anomaly.

        Args:
            event: The incoming pipeline event to evaluate.

        Returns:
            A new :class:`BlackBoxEvent` (created via
            :meth:`BlackBoxEvent.anomaly_event`) if an anomaly is
            detected, otherwise ``None``.
        """
