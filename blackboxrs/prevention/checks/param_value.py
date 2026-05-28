"""Preflight check: ROS 2 node parameter has an expected value.

Queries the remote parameter service of *node_name* using a transient
rclpy node. When rclpy is unavailable the check returns ``skipped``
rather than ``error`` so that non-ROS CI pipelines remain clean.

Parameters
----------
node_name : str
    Fully-qualified or bare name of the target node (e.g.
    ``/my_ns/planner`` or ``planner``). Required.
parameter : str
    Parameter name to fetch (e.g. ``use_sim_time``). Required.
value_equals : any, optional
    Exact equality check (Python ``==``).
value_in : list, optional
    Membership check; pass when ``actual in value_in``.
min_value : numeric, optional
    Fail when ``actual < min_value``.
max_value : numeric, optional
    Fail when ``actual > max_value``.

Result
------
``("pass" | "fail" | "skipped" | "error", message)``

* ``"skipped"`` when rclpy is not installed or the target node does not
  expose a parameter service.
* ``"error"``   when required params are missing or an unexpected
  exception occurs.
"""

from __future__ import annotations

from typing import Any

from ._rclpy_runtime import RCLPY_AVAILABLE, transient_node


def run(params: dict[str, Any]) -> tuple[str, str]:
    """Run a param_value preflight check."""
    node_name: Any = params.get("node_name")
    parameter: Any = params.get("parameter")

    if not isinstance(node_name, str) or not node_name.strip():
        return ("error",
                "param_value requires a non-empty 'node_name' parameter.")
    if not isinstance(parameter, str) or not parameter.strip():
        return ("error",
                "param_value requires a non-empty 'parameter' parameter.")

    if not RCLPY_AVAILABLE:
        return (
            "skipped",
            "rclpy not installed; param checks unavailable on this host.",
        )

    value_equals: Any = params.get("value_equals")
    value_in: list[Any] | None = params.get("value_in")
    min_value: Any = params.get("min_value")
    max_value: Any = params.get("max_value")

    # Normalise the node name to a simple bare name for the parameter
    # client: rclpy's async_parameters_client takes a node_name string.
    fq_node = node_name.strip()

    try:
        with transient_node(name="blackbox_preflight_param_value") as node:
            return _query_param(
                node,
                fq_node,
                parameter,
                value_equals=value_equals,
                value_in=value_in,
                min_value=min_value,
                max_value=max_value,
            )
    except RuntimeError as exc:
        # transient_node raises RuntimeError when rclpy is unavailable
        # (belt-and-suspenders: RCLPY_AVAILABLE check above should catch it).
        return ("skipped", str(exc))
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {exc}")


def _query_param(
    node: Any,
    target_node: str,
    parameter: str,
    *,
    value_equals: Any,
    value_in: list[Any] | None,
    min_value: Any,
    max_value: Any,
) -> tuple[str, str]:
    """Fetch *parameter* from *target_node* and evaluate constraints."""
    # rclpy's synchronous API for remote parameters requires spinning a
    # future. We use AsyncParametersClient which is the canonical rclpy
    # approach for a short-lived helper node.
    try:
        # Import here; module-level import is avoided to keep the module
        # importable in environments where rclpy is absent (RCLPY_AVAILABLE
        # guards before we reach this point, but belt-and-suspenders).
        from rclpy.parameter import Parameter  # type: ignore[import]
        from rclpy.parameter_client import AsyncParametersClient  # type: ignore[import]
    except ImportError:
        return ("skipped",
                "rclpy not installed; param checks unavailable on this host.")

    client = AsyncParametersClient(node, target_node)

    # Wait for the service to become available.
    if not client.wait_for_service(timeout_sec=2.0):
        return (
            "skipped",
            f"{target_node} does not expose parameter services "
            f"(service not available within 2 s).",
        )

    future = client.get_parameters([parameter])
    import rclpy  # type: ignore[import]
    rclpy.spin_until_future_complete(node, future, timeout_sec=3.0)

    if not future.done():
        return (
            "error",
            f"get_parameters({parameter!r}) on {target_node} timed out.",
        )

    result: list[Parameter] = future.result()
    if not result:
        return (
            "error",
            f"No result returned for parameter {parameter!r} on {target_node}.",
        )

    param_obj = result[0]
    # ParameterType.PARAMETER_NOT_SET == 0
    if param_obj.type_ == 0:
        return (
            "fail",
            f"{target_node}: parameter {parameter!r} is not set.",
        )

    actual = param_obj.value

    # --- constraint evaluation ---
    if value_equals is not None and actual != value_equals:
        return (
            "fail",
            f"{target_node}/{parameter}={actual!r} does not equal "
            f"expected {value_equals!r}.",
        )
    if value_in is not None and actual not in value_in:
        return (
            "fail",
            f"{target_node}/{parameter}={actual!r} not in {value_in!r}.",
        )
    if min_value is not None:
        try:
            if actual < min_value:
                return (
                    "fail",
                    f"{target_node}/{parameter}={actual!r} < min {min_value}.",
                )
        except TypeError:
            return (
                "error",
                f"Cannot compare {parameter!r}={actual!r} against "
                f"min_value={min_value!r}.",
            )
    if max_value is not None:
        try:
            if actual > max_value:
                return (
                    "fail",
                    f"{target_node}/{parameter}={actual!r} > max {max_value}.",
                )
        except TypeError:
            return (
                "error",
                f"Cannot compare {parameter!r}={actual!r} against "
                f"max_value={max_value!r}.",
            )

    return (
        "pass",
        f"{target_node}/{parameter}={actual!r} satisfies all constraints.",
    )
