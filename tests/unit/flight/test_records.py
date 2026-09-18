"""Message extraction and publisher-stamp handling."""

from __future__ import annotations

import json
import math

from blackboxrs.flight.records import (MAX_LIST, extract_fields, make_msg_record,
                                       publisher_stamp, to_plain)


class Msg:
    """Minimal stand-in for a generated ROS message class."""

    def __init__(self, **fields):
        self._f = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def get_fields_and_field_types(self):
        return {k: "x" for k in self._f}


def test_nested_extraction_and_missing_fields():
    msg = Msg(header=Msg(identity=Msg(id=7, api_id=1003)), parameter="")
    data, missing = extract_fields(msg, ("header.identity.id", "header.identity.api_id",
                                         "header.nope"))
    assert data == {"header": {"identity": {"id": 7, "api_id": 1003}}}
    assert missing == ["header.nope"]


def test_full_message_when_no_fields():
    data, missing = extract_fields(Msg(a=1, b=Msg(c=2.5)), ())
    assert data == {"a": 1, "b": {"c": 2.5}} and missing == []


def test_non_finite_floats_stay_visible():
    out = to_plain(Msg(x=math.nan, y=math.inf, z=-math.inf))
    assert out == {"x": "NaN", "y": "Infinity", "z": "-Infinity"}
    json.dumps(out)  # must be serialisable


def test_long_sequences_are_truncated_but_counted():
    out = to_plain(list(range(MAX_LIST + 10)))
    assert out["len"] == MAX_LIST + 10 and out["truncated"] and len(out["head"]) == MAX_LIST


def test_zero_stamp_is_not_an_emission_time():
    # HelixHold.asserted_stamp is 0 when not holding; an unset header is 0.
    assert publisher_stamp("helix_hold", {"stamp": 0.0}) == (None, None)
    assert publisher_stamp("odometry", {"header": {"stamp": {"sec": 0, "nanosec": 0}}}) == \
        (None, None)


def test_stamp_domains():
    assert publisher_stamp("arbiter_status", {"stamp": 12.5}) == (12.5, "helix_payload_wall")
    s, d = publisher_stamp("odometry", {"header": {"stamp": {"sec": 3, "nanosec": 500000000}}})
    assert (s, d) == (3.5, "robot_clock")
    # roles without a stamp field get none, never a receipt time
    assert publisher_stamp("cmd_vel_out", {"linear": {}}) == (None, None)


def test_sink_trace_json_is_decoded():
    rec = make_msg_record(topic="/helix/sink/trace", role="sink_trace",
                          msg_type="std_msgs/msg/String",
                          data={"data": json.dumps({"t_wall": 5.0, "api_id": 1003})},
                          t_mono_ns=1, t_wall_ns=2)
    assert rec["data"]["api_id"] == 1003
    assert rec["pub_stamp_s"] == 5.0 and rec["pub_stamp_domain"] == "helix_payload_wall"


def test_receipt_times_never_fill_publisher_fields():
    rec = make_msg_record(topic="/cmd_vel", role="cmd_vel_out",
                          msg_type="geometry_msgs/msg/Twist", data={}, t_mono_ns=10,
                          t_wall_ns=20)
    assert rec["dds_src_ns"] is None and rec["pub_stamp_s"] is None
