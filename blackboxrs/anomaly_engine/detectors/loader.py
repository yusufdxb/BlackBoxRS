"""Dynamic loader for user-supplied custom anomaly detectors."""

from __future__ import annotations

import importlib
import logging

from .base import BaseDetector

logger = logging.getLogger(__name__)


def load_custom_detectors(entries: list[dict]) -> list[BaseDetector]:
    """Import and instantiate custom detectors from config entries.

    Each entry must have a ``class`` key (dotted import path) and an
    optional ``params`` dict of keyword arguments for ``__init__``.

    Errors are logged as warnings and the offending entry is skipped.
    This function never raises — a broken custom detector must not
    prevent the engine from starting.
    """
    detectors: list[BaseDetector] = []

    for entry in entries:
        class_path = entry.get("class", "")
        params = entry.get("params", {})

        if not class_path:
            logger.warning("Custom detector entry missing 'class' key, skipping: %s", entry)
            continue

        dot = class_path.rfind(".")
        if dot <= 0:
            logger.warning(
                "Custom detector class path must be a dotted path "
                "(e.g. 'mypackage.module.ClassName'), got: %s",
                class_path,
            )
            continue

        module_path = class_path[:dot]
        class_name = class_path[dot + 1:]

        try:
            module = importlib.import_module(module_path)
        except Exception as exc:
            logger.warning(
                "Failed to import module '%s' for custom detector '%s': %s",
                module_path, class_path, exc,
            )
            continue

        cls = getattr(module, class_name, None)
        if cls is None:
            logger.warning(
                "Module '%s' has no attribute '%s'",
                module_path, class_name,
            )
            continue

        if not (isinstance(cls, type) and issubclass(cls, BaseDetector)):
            logger.warning(
                "Class '%s' is not a BaseDetector subclass, skipping",
                class_path,
            )
            continue

        try:
            instance = cls(**params)
        except Exception as exc:
            logger.warning(
                "Failed to instantiate custom detector '%s': %s",
                class_path, exc,
            )
            continue

        detectors.append(instance)
        logger.info("Loaded custom detector: %s (name=%s)", class_path, instance.name)

    return detectors
