"""Preflight check: environment variable is present (or absent) with
an optional value constraint.

This check is pure-Python and has no ROS 2 / rclpy dependency.

Parameters
----------
name : str
    Name of the environment variable to inspect. Required.
present : bool, default True
    * True: fail when the variable is missing or empty.
    * False: fail when the variable IS set (non-empty).
value_equals : str, optional
    When set, fail unless the variable's value equals this string
    exactly (case-sensitive).
value_regex : str, optional
    When set, fail unless ``re.fullmatch(value_regex, value)`` matches.
    Evaluated after ``value_equals`` when both are provided.

Result messages follow the pattern::

    ("pass",  "ENV_VAR is set to 'value'")
    ("fail",  "ENV_VAR is not set")
    ("fail",  "ENV_VAR is set (present=False)")
    ("fail",  "ENV_VAR='value' does not equal expected 'expected'")
    ("fail",  "ENV_VAR='value' does not match regex 'pattern'")
    ("error", "env_var requires a non-empty 'name' parameter")
"""

from __future__ import annotations

import os
import re
from typing import Any


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run an env_var preflight check."""
    name: Any = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return ("error", "env_var requires a non-empty 'name' parameter.")

    present: bool = bool(params.get("present", True))
    value_equals: str | None = params.get("value_equals")
    value_regex: str | None = params.get("value_regex")

    value = os.environ.get(name)
    is_set = value is not None and value != ""

    if present:
        if not is_set:
            return ("fail", f"{name} is not set.")
        # Variable is set; check optional value constraints.
        assert value is not None  # type narrowing
        if value_equals is not None and value != value_equals:
            return (
                "fail",
                f"{name}={value!r} does not equal expected {value_equals!r}.",
            )
        if value_regex is not None:
            if re.fullmatch(value_regex, value) is None:
                return (
                    "fail",
                    f"{name}={value!r} does not match regex {value_regex!r}.",
                )
        return ("pass", f"{name} is set to {value!r}.")
    else:
        # present=False: pass only when the variable is absent/empty.
        if is_set:
            return ("fail", f"{name} is set (present=False); expected unset.")
        return ("pass", f"{name} is not set, as required.")
