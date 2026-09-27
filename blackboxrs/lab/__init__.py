"""Replay Lab: deterministic incident replay and fault injection.

Replay Lab takes recorded (or synthetic) flight-recorder evidence, replays it
on a virtual clock, optionally injects declared faults into the replayed
stream, runs a model of the motion-arbitration path and the BlackBoxRS
detectors over it, and reports a causal timeline and a verdict.

The layers are kept separate:

1. ``evidence``  load and validate a flight bundle; refuse incomplete evidence
2. ``events``    normalize records into ordered replay events
3. ``clock``     virtual monotonic replay clock and wall-clock pacing
4. ``faults``    deterministic fault injectors over the event stream
5. ``sut``       the arbitration path under test (recorded, or a reference model)
6. ``monitors``, ``liveness``, ``transport``  detectors and safety invariants
7. ``engine``    dispatch; ``timeline`` and ``verdict`` build the result

Nothing in this package reads the wall clock for a result. Real-time pacing
only sleeps between events; it never changes what is computed.

It is not a physics simulator and not a GO2 model. Recorded physical
responses (odometry) are replayed as recorded and never change under
injection.
"""

from blackboxrs.lab.engine import ReplayConfig, replay
from blackboxrs.lab.evidence import EvidenceError, load_evidence

__all__ = ["EvidenceError", "ReplayConfig", "load_evidence", "replay"]
