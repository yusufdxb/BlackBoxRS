"""VersionSignature collector.

Hashes *what software was running.* Pairs with :class:`ConfigSignature`
to fully describe a session. Best-effort: any tool we cannot find on the
host (apt, pip, nvidia-smi) is silently skipped: the hash still makes
sense across runs that share the same toolchain availability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackboxrs.incident.models import VersionSignature

logger = logging.getLogger(__name__)


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a flat dict; empty on non-Linux."""
    path = Path("/etc/os-release")
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _safe_run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run *cmd*, return stdout or None on any failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _query_nvidia_driver() -> str | None:
    out = _safe_run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    if not out:
        return None
    line = out.strip().splitlines()[0] if out.strip() else None
    return line or None


def _query_ros_packages_apt() -> list[dict[str, str]]:
    """Return ros-* apt packages as ``[{name, version}]`` (best effort)."""
    out = _safe_run(["dpkg-query", "-W", "-f=${Package} ${Version}\\n"])
    if not out:
        return []
    pkgs: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        name, _, version = line.partition(" ")
        if name.startswith("ros-"):
            pkgs.append({"name": name, "version": version})
    return pkgs


def _query_pip_freeze_sha256() -> str | None:
    """Return sha256 of ``pip freeze`` output, or None."""
    out = _safe_run([sys.executable, "-m", "pip", "freeze", "--disable-pip-version-check"])
    if out is None:
        return None
    # Sort lines so casing/order in pip's resolver doesn't perturb the hash.
    lines = sorted(line.strip() for line in out.splitlines() if line.strip())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class VersionSignatureCollector:
    """Collect a :class:`VersionSignature` describing the current toolchain."""

    def collect(self) -> VersionSignature:
        from blackboxrs import __version__ as _bbrs_version

        os_release = _read_os_release()
        ros_packages = _query_ros_packages_apt()

        payload: dict[str, Any] = {
            "os": {
                "name": os_release.get("NAME"),
                "version": os_release.get("VERSION_ID") or os_release.get("VERSION"),
                "kernel": platform.release(),
            },
            "python": {
                "version": platform.python_version(),
                "executable": sys.executable,
            },
            "ros_distro": os_release.get("ROS_DISTRO") or _ros_distro_from_env(),
            "ros_packages_apt_count": len(ros_packages),
            "nvidia_driver": _query_nvidia_driver(),
            "blackboxrs_version": _bbrs_version,
            "pip_freeze_sha256": _query_pip_freeze_sha256(),
        }

        return VersionSignature(
            t=datetime.now(timezone.utc),
            hash=_hash_payload(payload),
            payload=payload,
        )


def _ros_distro_from_env() -> str | None:
    import os

    return os.environ.get("ROS_DISTRO")
