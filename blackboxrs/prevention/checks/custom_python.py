"""Preflight check: invoke a user-supplied Python callable.

SECURITY / TRUST BOUNDARY
--------------------------
This check executes **arbitrary Python code** supplied by the operator.
It is gated on the operator controlling the rules directory
(``~/.blackboxrs/prevention/rules/`` by default).  BlackBoxRS does not
sandbox the callable in any way.  Do **not** load rules from untrusted
sources when ``custom_python`` checks are present.

This module intentionally imports nothing from the rest of
``blackboxrs`` to avoid import cycles, because user callables may
themselves import blackboxrs sub-packages.

Parameters
----------
callable : str
    Import path to the function.  Accepted forms:

    * ``pkg.module:func``   (preferred, explicit)
    * ``pkg.module.func``   (dotted; last segment is the attribute)

    The callable must accept ``(*args, **kwargs)`` where ``args`` and
    ``kwargs`` come from the parameters below, and must return a
    ``(status, message)`` tuple with ``status`` in
    ``{"pass", "fail", "skipped", "error"}``.

args : list, optional
    Positional arguments forwarded to the callable.
kwargs : dict, optional
    Keyword arguments forwarded to the callable.

Result
------
``("pass" | "fail" | "skipped" | "error", message)``

Failure modes:

* ``("error", ...)`` when ``callable`` param is missing or malformed.
* ``("error", ...)`` when the module cannot be imported.
* ``("error", ...)`` when the attribute is not found.
* ``("error", ...)`` when the callable raises an exception.
* ``("error", ...)`` when the return value is not a 2-tuple with a
  valid status string.
"""

from __future__ import annotations

import importlib
from typing import Any

_VALID_STATUSES = frozenset({"pass", "fail", "skipped", "error"})


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a custom_python preflight check."""
    callable_path: Any = params.get("callable")
    if not isinstance(callable_path, str) or not callable_path.strip():
        return (
            "error",
            "custom_python requires a non-empty 'callable' parameter "
            "(e.g. 'my_pkg.checks:verify_ready').",
        )

    args: list[Any] = list(params.get("args") or [])
    kwargs: dict[str, Any] = dict(params.get("kwargs") or {})

    # --- resolve the callable ---
    fn, err = _resolve(callable_path.strip())
    if fn is None:
        return ("error", err or f"Could not resolve {callable_path!r}.")

    # --- invoke ---
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        return (
            "error",
            f"{callable_path} raised {type(exc).__name__}: {exc}",
        )

    # --- validate return shape ---
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not isinstance(result[0], str)
        or not isinstance(result[1], str)
    ):
        return (
            "error",
            f"{callable_path} returned {result!r}; expected (status: str, "
            f"message: str) tuple.",
        )

    status, message = result
    if status not in _VALID_STATUSES:
        return (
            "error",
            f"{callable_path} returned invalid status {status!r}; "
            f"must be one of {sorted(_VALID_STATUSES)}.",
        )

    return (status, message)


def _resolve(path: str) -> tuple[Any | None, str]:
    """Return ``(callable, "")`` or ``(None, error_message)``."""
    # Prefer the colon form: "pkg.module:func"
    if ":" in path:
        module_path, _, attr = path.partition(":")
        if not module_path or not attr:
            return (
                None,
                f"Malformed callable path {path!r}; expected 'module:attr'.",
            )
    else:
        # Dotted form: last segment is the attribute.
        if "." not in path:
            return (
                None,
                f"Malformed callable path {path!r}; no module separator found. "
                f"Use 'pkg.module:func' or 'pkg.module.func'.",
            )
        module_path, _, attr = path.rpartition(".")

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        return (None, f"Cannot import module {module_path!r}: {exc}")
    except Exception as exc:
        return (None, f"Error importing {module_path!r}: {type(exc).__name__}: {exc}")

    fn = getattr(module, attr, None)
    if fn is None:
        return (
            None,
            f"Module {module_path!r} has no attribute {attr!r}.",
        )
    if not callable(fn):
        return (
            None,
            f"{module_path}.{attr} is not callable (got {type(fn).__name__}).",
        )

    return (fn, "")
