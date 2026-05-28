"""Unit tests for the param_value preflight check.

rclpy is not available in the test environment.  The tests exercise:

1. The "rclpy not installed" early-exit path.
2. Error paths (missing / bad params).
3. The full _query_param logic via monkeypatching transient_node to inject
   a stub node whose AsyncParametersClient returns deterministic results.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


from blackboxrs.prevention.checks import param_value as _pv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_param_obj(value, type_=1):
    """Build a minimal rclpy Parameter-lookalike."""
    return SimpleNamespace(value=value, type_=type_)


def _make_stub_client(*, service_up: bool, params: list):
    """Return a fake AsyncParametersClient class."""
    future = MagicMock()
    future.done.return_value = True
    future.result.return_value = params

    client_instance = MagicMock()
    client_instance.wait_for_service.return_value = service_up
    client_instance.get_parameters.return_value = future

    class _StubClient:
        def __init__(self, node, target_node):
            pass
        def wait_for_service(self, timeout_sec=2.0):
            return service_up
        def get_parameters(self, names):
            return future

    return _StubClient


def _patch_rclpy_available(available: bool):
    return patch.object(_pv, "RCLPY_AVAILABLE", available)


# ---------------------------------------------------------------------------
# rclpy not available
# ---------------------------------------------------------------------------


def test_skipped_when_rclpy_not_available():
    with _patch_rclpy_available(False):
        status, msg = _pv.run({"node_name": "/my/node", "parameter": "speed"})
    assert status == "skipped"
    assert "rclpy" in msg.lower()


# ---------------------------------------------------------------------------
# Parameter validation errors
# ---------------------------------------------------------------------------


def test_error_on_missing_node_name():
    status, msg = _pv.run({"parameter": "use_sim_time"})
    assert status == "error"
    assert "node_name" in msg


def test_error_on_missing_parameter():
    status, msg = _pv.run({"node_name": "/planner"})
    assert status == "error"
    assert "parameter" in msg


# ---------------------------------------------------------------------------
# Stub-based functional tests (rclpy available)
# ---------------------------------------------------------------------------


@contextmanager
def _stub_transient(param_objs, *, service_up=True):
    """Patch transient_node + rclpy imports so _query_param runs without rclpy."""
    stub_node = MagicMock()

    StubClient = _make_stub_client(service_up=service_up, params=param_objs)

    with (
        _patch_rclpy_available(True),
        patch.object(_pv, "transient_node") as mock_tn,
        patch("builtins.__import__", side_effect=_make_import_side_effect(StubClient)),
    ):
        @contextmanager
        def _cm(name=""):
            yield stub_node

        mock_tn.side_effect = _cm
        yield stub_node


def _make_import_side_effect(stub_client_cls):
    """Return an __import__ side effect that injects stub classes for rclpy."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") \
        else __import__

    def _side_effect(name, *args, **kwargs):
        if name == "rclpy.parameter_client":
            ns = SimpleNamespace(AsyncParametersClient=stub_client_cls)
            return ns
        if name == "rclpy.parameter":
            ns = SimpleNamespace(Parameter=object)
            return ns
        if name == "rclpy":
            rclpy_stub = MagicMock()
            rclpy_stub.spin_until_future_complete = lambda node, fut, timeout_sec=3.0: None
            return rclpy_stub
        return real_import(name, *args, **kwargs)

    return _side_effect


def test_pass_value_equals_match():
    param_obj = _make_param_obj(True, type_=5)
    stub_client = _make_stub_client(service_up=True, params=[param_obj])

    with (
        _patch_rclpy_available(True),
        patch.object(_pv, "transient_node") as mock_tn,
    ):
        node_stub = MagicMock()

        @contextmanager
        def _cm(name=""):
            yield node_stub

        mock_tn.side_effect = _cm

        with patch.dict("sys.modules", {
            "rclpy": _make_rclpy_stub(stub_client),
            "rclpy.parameter": SimpleNamespace(Parameter=object),
            "rclpy.parameter_client": SimpleNamespace(
                AsyncParametersClient=stub_client
            ),
        }):
            status, msg = _pv.run({
                "node_name": "/planner",
                "parameter": "use_sim_time",
                "value_equals": True,
            })
    assert status == "pass"
    assert "use_sim_time" in msg


def _make_rclpy_stub(stub_client):
    rclpy_stub = MagicMock()
    rclpy_stub.spin_until_future_complete = lambda node, fut, timeout_sec=3.0: None
    return rclpy_stub


def test_fail_value_equals_mismatch():
    param_obj = _make_param_obj(False, type_=5)
    stub_client = _make_stub_client(service_up=True, params=[param_obj])

    with (
        _patch_rclpy_available(True),
        patch.object(_pv, "transient_node") as mock_tn,
        patch.dict("sys.modules", {
            "rclpy": _make_rclpy_stub(stub_client),
            "rclpy.parameter": SimpleNamespace(Parameter=object),
            "rclpy.parameter_client": SimpleNamespace(
                AsyncParametersClient=stub_client
            ),
        }),
    ):
        node_stub = MagicMock()

        @contextmanager
        def _cm(name=""):
            yield node_stub

        mock_tn.side_effect = _cm

        status, msg = _pv.run({
            "node_name": "/planner",
            "parameter": "use_sim_time",
            "value_equals": True,
        })
    assert status == "fail"
    assert "False" in msg or "does not equal" in msg


def test_skipped_when_service_not_up():
    stub_client = _make_stub_client(service_up=False, params=[])

    with (
        _patch_rclpy_available(True),
        patch.object(_pv, "transient_node") as mock_tn,
        patch.dict("sys.modules", {
            "rclpy": _make_rclpy_stub(stub_client),
            "rclpy.parameter": SimpleNamespace(Parameter=object),
            "rclpy.parameter_client": SimpleNamespace(
                AsyncParametersClient=stub_client
            ),
        }),
    ):
        node_stub = MagicMock()

        @contextmanager
        def _cm(name=""):
            yield node_stub

        mock_tn.side_effect = _cm

        status, msg = _pv.run({
            "node_name": "/planner",
            "parameter": "speed",
        })
    assert status == "skipped"
    assert "parameter service" in msg.lower() or "not available" in msg.lower()


def test_fail_when_param_not_set():
    param_obj = _make_param_obj(None, type_=0)  # PARAMETER_NOT_SET
    stub_client = _make_stub_client(service_up=True, params=[param_obj])

    with (
        _patch_rclpy_available(True),
        patch.object(_pv, "transient_node") as mock_tn,
        patch.dict("sys.modules", {
            "rclpy": _make_rclpy_stub(stub_client),
            "rclpy.parameter": SimpleNamespace(Parameter=object),
            "rclpy.parameter_client": SimpleNamespace(
                AsyncParametersClient=stub_client
            ),
        }),
    ):
        node_stub = MagicMock()

        @contextmanager
        def _cm(name=""):
            yield node_stub

        mock_tn.side_effect = _cm

        status, msg = _pv.run({
            "node_name": "/planner",
            "parameter": "max_vel",
        })
    assert status == "fail"
    assert "not set" in msg.lower()
