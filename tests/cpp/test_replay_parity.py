"""Every replay the C++ engine produces must equal the Python reference, byte for byte.

The whole result document is compared as canonical JSON (sorted keys, Python
float formatting): verdict, invariant statuses and counts, every finding with
its evidence and related faults, the injection log, the causal timeline and
its text, the liveness, clock and transport summaries.
"""

from __future__ import annotations

import pytest

from .conftest import CASES, EVIDENCE, cpp_replay_json, first_difference, needs_cpp, python_replay_json

pytestmark = needs_cpp

GOLDEN = sorted(p.name for p in CASES.glob("*.json"))

# Fault combinations beyond the golden cases: every injector kind at least
# once, on both bundles and all three systems under test.
NOMINAL = str(EVIDENCE / "nominal_motion")
STOP = str(EVIDENCE / "clean_stop")
EXTRA = [
    (NOMINAL, "helix_arbiter", ("drop:topic=/nav/cmd_vel,from_s=2.5,every_n=3",)),
    (NOMINAL, "twist_mux_legacy", ("gap:topic=/nav/cmd_vel,from_s=3.0,to_s=3.7",)),
    (NOMINAL, "helix_arbiter", ("delay:topic=/helix/hold,from_s=2.0,to_s=4.0,delay_s=0.35",)),
    (STOP, "helix_arbiter", ("duplicate:topic=/nav/cmd_vel,every_n=2,lag_s=0.001",)),
    (STOP, "twist_mux_legacy", ("reorder:topic=/helix/hold,from_s=2.5,to_s=4.5",)),
    (STOP, "helix_arbiter", ("stale_redelivery:topic=/nav/cmd_vel,at_s=4.2,age_s=1.5",)),
    (NOMINAL, "helix_arbiter", ("clock_skew:host=robot,offset_s=-0.9",
                                "timestamp_jump:topic=/nav/cmd_vel,at_s=3.0,jump_s=0.45")),
    (NOMINAL, "twist_mux_legacy", ("inf:topic=/nav/cmd_vel,field=angular.z,sign=-1,from_s=3.0,to_s=3.3",)),
    (NOMINAL, "helix_arbiter", ("malformed:topic=/nav/cmd_vel,fields=linear.y,remove=true,from_s=3.0,to_s=3.3",)),
    (STOP, "observed", ("set_value:topic=/cmd_vel,field=linear.x,value=0.3,from_s=4.0,to_s=4.6",)),
    (NOMINAL, "helix_arbiter", ("freeze:topic=/utlidar/robot_odom,field=pose.pose.position.y,from_s=2.0",)),
    (NOMINAL, "helix_arbiter", ("step:topic=/utlidar/robot_odom,field=pose.pose.position.y,at_s=3.1,delta=-0.8",)),
    (NOMINAL, "helix_arbiter", ("node_exit:node=/helix_arbiter,at_s=2.0",)),
    (STOP, "twist_mux_legacy", ('{"kind":"inject_stream","topic":"/teleop/cmd_vel","from_s":3.2,'
                                '"to_s":5.0,"rate_hz":25,"data":{"linear":{"x":0.3,"y":0.0,"z":0.0},'
                                '"angular":{"x":0.0,"y":0.0,"z":0.2}}}',)),
    (STOP, "helix_arbiter", ("drop:host=payload,from_s=4.4,to_s=4.9",
                             "nan:topic=/nav/cmd_vel,field=angular.z,from_s=1.0,to_s=1.2")),
]


@pytest.mark.parametrize("case", GOLDEN)
def test_golden_case_is_byte_identical(case):
    target = str(CASES / case)
    py, cpp = python_replay_json(target), cpp_replay_json(target)
    assert py == cpp, first_difference(py, cpp)


@pytest.mark.parametrize("bundle,sut,faults", EXTRA,
                         ids=[f"{i}-{f[0].split(':')[0].strip('{')[:20]}" for i, (_, _, f) in enumerate(EXTRA)])
def test_fault_combinations_are_byte_identical(bundle, sut, faults):
    py = python_replay_json(bundle, faults, sut)
    args = ["--sut", sut]
    for f in faults:
        args += ["--inject", f]
    cpp = cpp_replay_json(bundle, *args)
    assert py == cpp, first_difference(py, cpp)


def test_every_injector_kind_is_exercised():
    from blackboxrs.lab.faults import KINDS
    used = {f.split(":")[0].lstrip("{") for _, _, fs in EXTRA for f in fs}
    used |= {"inject_stream"}
    import json
    for case in CASES.glob("*.json"):
        used |= {f["kind"] for f in json.loads(case.read_text()).get("faults", [])}
    assert set(KINDS) <= used, sorted(set(KINDS) - used)
