"""Structured logging subsystem for BlackBoxRS.

Re-exports the main :class:`LoggingPipeline` entry point.
"""

from .pipeline import LoggingPipeline

__all__ = ["LoggingPipeline"]
