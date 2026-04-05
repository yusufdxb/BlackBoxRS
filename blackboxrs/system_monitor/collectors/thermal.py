"""Thermal zone collector for BlackBoxRS.

Reads temperature data from Linux sysfs thermal zones at
``/sys/devices/virtual/thermal/thermal_zone*``.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_THERMAL_BASE = Path("/sys/devices/virtual/thermal")


class ThermalCollector:
    """Collects temperature readings from thermal zones."""

    def collect(self) -> list[dict]:
        """Return a list of temperature readings from all available thermal zones.

        Each entry is a dictionary with:

        - ``zone`` — the zone directory name (e.g. ``"thermal_zone0"``).
        - ``type`` — the zone type string reported by the kernel
          (e.g. ``"x86_pkg_temp"``).
        - ``temp_c`` — temperature in degrees Celsius.

        Returns:
            A list of dicts.  Zones that cannot be read are silently skipped.
            Returns an empty list if the sysfs thermal path does not exist.
        """
        if not _THERMAL_BASE.exists():
            return []

        readings: list[dict] = []

        try:
            for zone_dir in sorted(_THERMAL_BASE.glob("thermal_zone*")):
                entry = self._read_zone(zone_dir)
                if entry is not None:
                    readings.append(entry)
        except Exception:
            logger.exception("Failed to enumerate thermal zones")

        return readings

    @staticmethod
    def _read_zone(zone_dir: Path) -> dict | None:
        """Read a single thermal zone directory.

        Args:
            zone_dir: Path to the ``thermal_zoneN`` directory.

        Returns:
            A dict with ``zone``, ``type``, and ``temp_c``, or ``None``
            if the required files cannot be read.
        """
        type_path = zone_dir / "type"
        temp_path = zone_dir / "temp"

        if not type_path.exists() or not temp_path.exists():
            return None

        try:
            zone_type = type_path.read_text().strip()
            raw_temp = temp_path.read_text().strip()
            temp_c = round(float(raw_temp) / 1000.0, 1)

            return {
                "zone": zone_dir.name,
                "type": zone_type,
                "temp_c": temp_c,
            }
        except (ValueError, OSError):
            logger.debug("Skipping unreadable zone %s", zone_dir.name, exc_info=True)
            return None
