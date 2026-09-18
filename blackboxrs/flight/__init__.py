"""Passive flight recorder for GO2 hardware experiments.

The flight recorder subscribes to a declared topic profile, keeps a bounded
rolling window of every observed message, and on a trigger persists the
window before and after the trigger as an incident bundle with a
machine-readable and a human-readable report.

It is observation only. Nothing in this package creates a ROS publisher.
"""

from blackboxrs.flight.profile import FlightProfile, TopicSpec, load_profile

__all__ = ["FlightProfile", "TopicSpec", "load_profile"]
