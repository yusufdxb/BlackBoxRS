"""ConfigSignature collector.

Hashes the *configuration surface* that determines how a ROS 2 stack
behaves. The hash is deterministic given the same inputs so two runs
that are *configured the same way* produce the same hash, regardless of
clock or session.

Inputs included by default:
- Whitelisted ``ROS_*`` / ``RMW_*`` environment variables.
- Hashes of optional attached launch / parameter / URDF files
  registered via ``robot-blackbox attach-launch`` (M4 / future).
- Sha256 of the BlackBoxRS config YAML, when present.

Inputs *not* included (intentionally):
- Hostname, session id, wall-clock time. They make every run unique
  and would defeat the point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackboxrs.incident.models import ConfigSignature

logger = logging.getLogger(__name__)


# Env vars whose values directly determine ROS / DDS behaviour.
_ENV_WHITELIST = (
    "ROS_DISTRO",
    "ROS_DOMAIN_ID",
    "ROS_LOCALHOST_ONLY",
    "ROS_NAMESPACE",
    "ROS_VERSION",
    "RMW_IMPLEMENTATION",
    "FASTRTPS_DEFAULT_PROFILES_FILE",
    "CYCLONEDDS_URI",
)


def _sha256_file(path: Path) -> str | None:
    """Return the lowercase sha256 hex of *path*, or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _hash_payload(payload: dict[str, Any]) -> str:
    """Canonical-JSON sha256 of *payload*."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfigSignatureCollector:
    """Collects a :class:`ConfigSignature` from the running environment.

    Args:
        attached_files: Optional list of paths whose sha256 should be
            recorded under ``payload['attached_files']`` (launch, URDF,
            parameter YAMLs the user has explicitly registered).
        config_yaml_path: Optional path to the BlackBoxRS YAML config.
    """

    def __init__(
        self,
        attached_files: list[Path] | None = None,
        config_yaml_path: Path | None = None,
    ) -> None:
        self._attached_files = attached_files or []
        self._config_yaml_path = config_yaml_path

    def collect(self) -> ConfigSignature:
        """Return a fresh :class:`ConfigSignature` for the current process."""
        env_subset = {
            k: os.environ[k] for k in _ENV_WHITELIST if k in os.environ
        }

        attached: list[dict[str, str]] = []
        for p in self._attached_files:
            digest = _sha256_file(p)
            if digest is not None:
                attached.append({"path": str(p), "sha256": digest})

        config_yaml_sha = None
        if self._config_yaml_path is not None and self._config_yaml_path.is_file():
            config_yaml_sha = _sha256_file(self._config_yaml_path)

        payload: dict[str, Any] = {
            "ros_distro": os.environ.get("ROS_DISTRO"),
            "ros_domain_id": _maybe_int(os.environ.get("ROS_DOMAIN_ID")),
            "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION"),
            "env_subset": env_subset,
            "attached_files": attached,
            "blackboxrs_config_yaml_sha256": config_yaml_sha,
        }

        return ConfigSignature(
            t=datetime.now(timezone.utc),
            hash=_hash_payload(payload),
            payload=payload,
        )


def _maybe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
