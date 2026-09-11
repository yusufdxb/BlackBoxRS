"""Session manager for BlackBoxRS.

Tracks a unique session identifier, hostname, and start time so that
every event emitted during a run can be correlated back to the same session.
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime

from blackboxrs.core.clock import Clock


class Session:
    """Represents a single BlackBoxRS monitoring session.

    A session is created once at startup and provides standard metadata
    that is attached to every emitted event.

    Attributes:
        session_id: A short unique identifier for this session.
        hostname: The hostname of the machine running BlackBoxRS (the
            observer in observer mode, the robot in onboard mode).
        observed_host: Free-form label for the host being watched.
            ``None`` in onboard mode (because the local hostname *is*
            the observed host); set to the operator-supplied robot
            label in observer mode so bundles can name both sides.
        role: ``"onboard"`` or ``"observer"``: mirrors
            ``RuntimeConfig.role`` so downstream code can tell them
            apart without reaching back to the config.
        start_time: UTC datetime when the session was created.
    """

    def __init__(
        self,
        *,
        observed_host: str | None = None,
        role: str = "onboard",
    ) -> None:
        self.session_id: str = uuid.uuid4().hex[:12]
        self.hostname: str = socket.gethostname()
        self.observed_host: str | None = observed_host
        self.role: str = role
        self.start_time: datetime = Clock.now()

    def metadata(self) -> dict[str, str]:
        """Return standard metadata dict for embedding in events.

        Always includes ``session_id``, ``hostname``, and
        ``start_time``. Adds ``observed_host`` and ``role`` when in
        observer mode, so downstream tools can correlate without
        special-casing onboard bundles.
        """
        meta: dict[str, str] = {
            "session_id": self.session_id,
            "hostname": self.hostname,
            "start_time": Clock.format_iso(self.start_time),
        }
        if self.role == "observer":
            meta["role"] = self.role
            if self.observed_host:
                meta["observed_host"] = self.observed_host
        return meta

    def __repr__(self) -> str:
        suffix = ""
        if self.role == "observer":
            target = self.observed_host or "<unspecified>"
            suffix = f", observing={target!r}"
        return (
            f"Session(id={self.session_id!r}, "
            f"host={self.hostname!r}"
            f"{suffix}, "
            f"started={Clock.format_iso(self.start_time)!r})"
        )
