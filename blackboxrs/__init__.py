"""BlackBoxRS — Incident intelligence and prevention for ROS 2 robots."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("blackboxrs")
except PackageNotFoundError:  # pragma: no cover — source tree without install
    # Editable working tree with no install. Fall back to a sentinel so
    # consumers (e.g. version_signature collector) get a string instead
    # of a crash.
    __version__ = "0+unknown"
