"""Unit tests for the custom_python preflight check."""

from __future__ import annotations

import sys
import types


from blackboxrs.prevention.checks.custom_python import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_module(name: str, **attrs):
    """Inject a synthetic module into sys.modules for the duration of a test."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _deregister_module(name: str):
    sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# error: missing / malformed callable param
# ---------------------------------------------------------------------------


def test_error_on_missing_callable():
    status, msg = run({})
    assert status == "error"
    assert "callable" in msg.lower()


def test_error_on_empty_callable():
    status, msg = run({"callable": "  "})
    assert status == "error"


def test_error_on_malformed_callable_no_separator():
    # Single token with no dot or colon
    status, msg = run({"callable": "just_a_name"})
    assert status == "error"
    assert "malformed" in msg.lower() or "separator" in msg.lower()


# ---------------------------------------------------------------------------
# module not found
# ---------------------------------------------------------------------------


def test_error_on_module_not_found():
    status, msg = run({"callable": "no_such_package.no_module:func"})
    assert status == "error"
    assert "no_such_package" in msg or "import" in msg.lower()


# ---------------------------------------------------------------------------
# valid callable (colon form)
# ---------------------------------------------------------------------------


def test_pass_via_colon_form():
    mod_name = "_bb_test_custom_py_pass"
    _register_module(mod_name, check_ok=lambda: ("pass", "everything is fine"))
    try:
        status, msg = run({"callable": f"{mod_name}:check_ok"})
        assert status == "pass"
        assert "fine" in msg
    finally:
        _deregister_module(mod_name)


# ---------------------------------------------------------------------------
# valid callable (dotted form)
# ---------------------------------------------------------------------------


def test_pass_via_dotted_form():
    mod_name = "_bb_test_custom_py_dotted"
    _register_module(mod_name, my_check=lambda: ("pass", "dotted form works"))
    try:
        status, msg = run({"callable": f"{mod_name}.my_check"})
        assert status == "pass"
        assert "dotted" in msg
    finally:
        _deregister_module(mod_name)


# ---------------------------------------------------------------------------
# fail returned by callable
# ---------------------------------------------------------------------------


def test_fail_propagated_from_callable():
    mod_name = "_bb_test_custom_py_fail"
    _register_module(mod_name, bad_check=lambda: ("fail", "resource not ready"))
    try:
        status, msg = run({"callable": f"{mod_name}:bad_check"})
        assert status == "fail"
        assert "not ready" in msg
    finally:
        _deregister_module(mod_name)


# ---------------------------------------------------------------------------
# wrong return shape
# ---------------------------------------------------------------------------


def test_error_on_wrong_return_shape():
    mod_name = "_bb_test_custom_py_shape"
    _register_module(mod_name, bad_return=lambda: "just a string")
    try:
        status, msg = run({"callable": f"{mod_name}:bad_return"})
        assert status == "error"
        assert "tuple" in msg.lower() or "expected" in msg.lower()
    finally:
        _deregister_module(mod_name)


def test_error_on_invalid_status_value():
    mod_name = "_bb_test_custom_py_status"
    _register_module(mod_name, bad_status=lambda: ("unknown_status", "msg"))
    try:
        status, msg = run({"callable": f"{mod_name}:bad_status"})
        assert status == "error"
        assert "unknown_status" in msg
    finally:
        _deregister_module(mod_name)


# ---------------------------------------------------------------------------
# callable raises an exception
# ---------------------------------------------------------------------------


def test_error_on_callable_exception():
    mod_name = "_bb_test_custom_py_exc"
    def _raiser():
        raise RuntimeError("simulated failure")
    _register_module(mod_name, raiser=_raiser)
    try:
        status, msg = run({"callable": f"{mod_name}:raiser"})
        assert status == "error"
        assert "RuntimeError" in msg or "simulated failure" in msg
    finally:
        _deregister_module(mod_name)


# ---------------------------------------------------------------------------
# args / kwargs forwarding
# ---------------------------------------------------------------------------


def test_args_kwargs_forwarded():
    mod_name = "_bb_test_custom_py_args"
    def _echo(a, b=0):
        return ("pass", f"a={a} b={b}")
    _register_module(mod_name, echo=_echo)
    try:
        status, msg = run({
            "callable": f"{mod_name}:echo",
            "args": [42],
            "kwargs": {"b": 7},
        })
        assert status == "pass"
        assert "a=42" in msg
        assert "b=7" in msg
    finally:
        _deregister_module(mod_name)
