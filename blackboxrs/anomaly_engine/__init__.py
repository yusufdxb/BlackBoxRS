"""Anomaly detection subsystem for BlackBoxRS.

Re-exports the main :class:`AnomalyEngine` entry point.
"""

from .engine import AnomalyEngine

__all__ = ["AnomalyEngine"]
