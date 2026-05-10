"""Config and version signature collectors for BlackBoxRS.

A signature is a deterministic hash of a structured payload describing
*how* the robot was configured (`ConfigSignature`) or *what* software
was running (`VersionSignature`) at session start. They are required
for incident reproducibility (see ``ARCHITECTURE_PIVOT.md`` §1.6/§1.7).
"""

from __future__ import annotations

from .config import ConfigSignatureCollector
from .versions import VersionSignatureCollector

__all__ = ["ConfigSignatureCollector", "VersionSignatureCollector"]
