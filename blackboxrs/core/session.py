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
        hostname: The hostname of the machine running BlackBoxRS.
        start_time: UTC datetime when the session was created.
    """

    def __init__(self) -> None:
        self.session_id: str = uuid.uuid4().hex[:12]
        self.hostname: str = socket.gethostname()
        self.start_time: datetime = Clock.now()

    def metadata(self) -> dict[str, str]:
        """Return standard metadata dict for embedding in events.

        Returns:
            A dictionary containing ``session_id``, ``hostname``, and
            ``start_time`` (ISO 8601 formatted).
        """
        return {
            "session_id": self.session_id,
            "hostname": self.hostname,
            "start_time": Clock.format_iso(self.start_time),
        }

    def __repr__(self) -> str:
        return (
            f"Session(id={self.session_id!r}, "
            f"host={self.hostname!r}, "
            f"started={Clock.format_iso(self.start_time)!r})"
        )
