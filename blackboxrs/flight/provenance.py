"""Session provenance: who recorded what, where, with which code and config."""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackboxrs import __version__
from blackboxrs.flight.profile import FlightProfile


def git_state(path: str | Path) -> dict[str, Any]:
    """Read-only git query: HEAD SHA and whether the tree is dirty."""
    p = str(Path(path).expanduser())
    out: dict[str, Any] = {"path": p, "sha": None, "dirty": None}
    try:
        sha = subprocess.run(["git", "-C", p, "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        if sha.returncode == 0:
            out["sha"] = sha.stdout.strip()
            st = subprocess.run(["git", "-C", p, "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, timeout=10)
            out["dirty"] = bool(st.stdout.strip()) if st.returncode == 0 else None
        else:
            out["error"] = sha.stderr.strip()[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)
    return out


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return None


def rmw_identifier() -> str | None:
    try:
        import rclpy  # noqa: F401
        from rclpy.utilities import get_rmw_implementation_identifier
        return get_rmw_implementation_identifier()
    except Exception:  # noqa: BLE001
        return None


def build_session(
    profile: FlightProfile,
    *,
    experiment: str | None = None,
    session_id: str | None = None,
    experiment_repos: list[str] | None = None,
    synthetic: bool = False,
    use_sim_time: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    host = socket.gethostname()
    sid = session_id or f"{now.strftime('%Y%m%dT%H%M%SZ')}_{host}_{profile.name}_{uuid.uuid4().hex[:6]}"
    pkg_root = Path(__file__).resolve().parents[2]
    bb = git_state(pkg_root)
    return {
        "session_id": sid,
        "experiment": experiment,
        "synthetic": synthetic,
        "hostname": host,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "jetson_model": _read("/proc/device-tree/model"),
        "l4t_release": _read("/etc/nv_tegra_release"),
        "python": sys.version.split()[0],
        "ros_distro": os.environ.get("ROS_DISTRO"),
        "rmw_implementation": rmw_identifier() or os.environ.get("RMW_IMPLEMENTATION"),
        "rmw_env": os.environ.get("RMW_IMPLEMENTATION"),
        "ros_domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
        "ros_localhost_only": os.environ.get("ROS_LOCALHOST_ONLY"),
        "cyclonedds_uri": os.environ.get("CYCLONEDDS_URI"),
        "use_sim_time": use_sim_time,
        "blackboxrs_version": __version__,
        "blackboxrs_git_sha": bb.get("sha"),
        "blackboxrs_git_dirty": bb.get("dirty"),
        "experiment_repos": [git_state(p) for p in experiment_repos or []],
        "profile_name": profile.name,
        "profile_sha256": profile.sha256,
        "started_wall_ns": time.time_ns(),
        "started_mono_ns": time.monotonic_ns(),
        "clock_note": ("t_mono_ns is CLOCK_MONOTONIC on the recorder host; t_wall_ns is its "
                       "CLOCK_REALTIME; both are read together per record"),
    }
