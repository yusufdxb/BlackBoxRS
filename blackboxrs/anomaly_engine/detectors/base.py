"""Abstract base class for anomaly detectors.

Every detector receives the full stream of :class:`BlackBoxEvent` instances
and decides whether a particular event warrants raising an anomaly.

Detectors can also declare two class-level attributes used by the
incident pipeline:

* ``signature_fields``: names of keys in the emitted anomaly's
  ``data`` payload that are stable enough to define incident identity.
  The fingerprint algorithm hashes only these fields per detector class,
  so semantically-equivalent failures produce the same fingerprint and
  unrelated noise does not perturb it.
* ``target_subsystem``: the *target* domain the anomaly describes
  (``ros``, ``system``, ``gpu``, ``recorder``, ...). The trigger
  promotion in the incident builder uses this to assign a more useful
  ``subsystem`` than the catch-all ``anomaly`` value, which improves
  fingerprint discrimination and makes timeline/report rendering more
  honest about where the failure lived.

Both attributes are optional. Detectors that do not override them keep
their previous behaviour: empty ``signature_fields`` (so the fingerprint
falls back to detector-class identity only) and ``target_subsystem =
"anomaly"`` (preserves v0.3 trigger shape).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from blackboxrs.core.schemas import BlackBoxEvent


class BaseDetector(ABC):
    """Abstract base for anomaly detectors.

    Subclasses must implement :attr:`name` (a unique human-readable
    identifier) and :meth:`check` (the detection logic). They may also
    override :attr:`signature_fields` and :attr:`target_subsystem` to
    feed the incident-bundle pipeline.
    """

    #: Names of keys in the emitted anomaly's ``data`` whose values
    #: define incident identity for fingerprint computation. Override
    #: in subclasses; default is empty (no signature fields).
    signature_fields: ClassVar[list[str]] = []

    #: Target subsystem this detector describes. Used by the incident
    #: builder to set the trigger's ``subsystem``. Override in subclasses
    #: when the detector has a clear domain ("ros", "system", "gpu",
    #: ...). Default ``"anomaly"`` matches v0.3 trigger shape.
    target_subsystem: ClassVar[str] = "anomaly"

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

    # -- helpers shared by all detectors ----------------------------------

    @classmethod
    def detector_metadata(cls) -> dict[str, object]:
        """Return metadata fields every emitted anomaly should carry.

        Concrete detectors call this when constructing the anomaly
        ``BlackBoxEvent`` so the incident builder has a uniform place to
        read ``signature_fields``, ``detector_class``, and
        ``target_subsystem`` from.
        """
        return {
            "signature_fields": list(cls.signature_fields),
            "detector_class": f"{cls.__module__}.{cls.__name__}",
            "target_subsystem": cls.target_subsystem,
        }
