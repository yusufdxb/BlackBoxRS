"""Replay clock and pacing.

``ReplayClock`` is the only notion of "now" inside a replay. It is virtual:
it moves to the time of the next event or tick and refuses to move
backwards. Detectors, the arbitration model and the verdict read it; none of
them read the wall clock.

Pacing is separate. A pacer is told how far the virtual clock advanced and
may sleep for that long (scaled by a speed factor) so a person can watch a
replay unfold. It is given the sleep function, so tests substitute a fake
one, and nothing it does feeds back into the replay: the result of a paced
run is identical to an unpaced one (``tests/unit/lab/test_determinism.py``).
"""

from __future__ import annotations

import time
from typing import Callable


class ClockError(RuntimeError):
    pass


class ReplayClock:
    def __init__(self, start_ns: int = 0) -> None:
        self._now = start_ns

    @property
    def now_ns(self) -> int:
        return self._now

    def advance_to(self, t_ns: int) -> int:
        """Move to ``t_ns``; return the step taken. Never moves backwards."""
        if t_ns < self._now:
            raise ClockError(f"replay clock cannot go back from {self._now} to {t_ns}")
        step, self._now = t_ns - self._now, t_ns
        return step


class Pacer:
    """As fast as possible: never sleeps."""

    mode = "fast"

    def wait(self, step_ns: int) -> None:
        pass


class RealtimePacer(Pacer):
    """Sleep ``step / speed`` of wall time per virtual step. speed 1.0 = real time."""

    mode = "realtime"

    def __init__(self, speed: float, sleep: Callable[[float], None] = time.sleep) -> None:
        if not speed > 0:
            raise ValueError("speed must be > 0")
        self.speed = speed
        self._sleep = sleep
        self.slept_s = 0.0

    def wait(self, step_ns: int) -> None:
        if step_ns > 0:
            dt = step_ns / 1e9 / self.speed
            self.slept_s += dt
            self._sleep(dt)
