#include "blackboxrs/payload.hpp"

namespace blackboxrs {

bool json_truthy(const Json& v) noexcept {
  switch (v.type()) {
    case Json::value_t::null:
    case Json::value_t::discarded: return false;
    case Json::value_t::boolean: return v.get<bool>();
    case Json::value_t::number_integer: return v.get<std::int64_t>() != 0;
    case Json::value_t::number_unsigned: return v.get<std::uint64_t>() != 0;
    case Json::value_t::number_float:
      return v.get<double>() != 0.0;  // NaN is truthy in Python, and NaN != 0.0
    case Json::value_t::string: return !v.get_ref<const std::string&>().empty();
    case Json::value_t::array:
    case Json::value_t::object:
    case Json::value_t::binary: return !v.empty();
  }
  return false;
}

Flag read_flag(const Json* v) noexcept {
  if (v == nullptr) {
    return Flag::missing;
  }
  if (v->is_boolean()) {
    return v->get<bool>() ? Flag::is_true : Flag::is_false;
  }
  return json_truthy(*v) ? Flag::other_truthy : Flag::other_falsy;
}

IntField read_int(const Json* v) {
  IntField f;
  if (v == nullptr) {
    return f;
  }
  f.raw = *v;
  if (v->is_number_integer() && !v->is_number_unsigned()) {
    f.value = v->get<std::int64_t>();
  } else if (v->is_number_unsigned()) {
    const auto u = v->get<std::uint64_t>();
    if (u <= static_cast<std::uint64_t>(INT64_MAX)) {
      f.value = static_cast<std::int64_t>(u);
    }
  }
  return f;
}

TextField read_text(const Json* v) {
  TextField t;
  if (v == nullptr) {
    return t;
  }
  t.present = true;
  if (v->is_string()) {
    t.text = v->get<std::string>();
    t.is_string = true;
  } else if (v->is_null()) {
    t.text = "None";
  } else if (v->is_boolean()) {
    t.text = v->get<bool>() ? "True" : "False";
  } else {
    t.text = v->dump();
  }
  return t;
}

namespace {

VelocityCommand decode_twist(const Json& d) {
  VelocityCommand c;
  for (std::size_t i = 0; i < kTwistAxes.size(); ++i) {
    c.axes[i] = as_number(get_path(d, kTwistAxes[i]));
  }
  const Json* lx = get_path(d, "linear.x");
  const Json* ly = get_path(d, "linear.y");
  const Json* az = get_path(d, "angular.z");
  c.forwarded = {lx != nullptr ? *lx : Json(), ly != nullptr ? *ly : Json(),
                 az != nullptr ? *az : Json()};
  return c;
}

const Json* field(const Json& d, const char* key) {
  if (!d.is_object()) {
    return nullptr;
  }
  const auto it = d.find(key);
  return it == d.end() ? nullptr : &*it;
}

HoldState decode_hold(const Json& d) {
  HoldState h;
  h.hold = read_flag(field(d, "hold"));
  h.epoch = read_int(field(d, "epoch"));
  h.seq = read_int(field(d, "seq"));
  h.fault_id = read_text(field(d, "fault_id"));
  h.reason = read_text(field(d, "reason"));
  return h;
}

ArbiterStatus decode_status(const Json& d) {
  ArbiterStatus s;
  s.reason = read_text(field(d, "reason"));
  s.selected_source = read_text(field(d, "selected_source"));
  s.hold_fault_id = read_text(field(d, "hold_fault_id"));
  s.hold_active = read_flag(field(d, "hold_active"));
  const std::array<const char*, 3> keys{"out_linear_x", "out_linear_y", "out_angular_z"};
  for (std::size_t i = 0; i < keys.size(); ++i) {
    const Json* v = field(d, keys[i]);
    s.out_raw[i] = v != nullptr ? *v : Json();
    s.out[i] = as_number(v);
  }
  s.seq = read_int(field(d, "seq"));
  return s;
}

Odometry decode_odometry(const Json& d) {
  return {as_number(get_path(d, "pose.pose.position.x")),
          as_number(get_path(d, "pose.pose.position.y")),
          as_number(get_path(d, "twist.twist.linear.x")),
          as_number(get_path(d, "twist.twist.linear.y"))};
}

RecoveryAction decode_recovery(const Json& d) {
  return {read_text(field(d, "action")), read_text(field(d, "status")),
          read_text(field(d, "fault_id"))};
}

}  // namespace

TypedPayload decode_payload(Role role, const Json& data) {
  switch (role) {
    case Role::cmd_vel_source:
    case Role::cmd_vel_out: return decode_twist(data);
    case Role::helix_hold: return decode_hold(data);
    case Role::arbiter_status: return decode_status(data);
    case Role::odometry: return decode_odometry(data);
    case Role::recovery_action: return decode_recovery(data);
    default: return OpaquePayload{};
  }
}

}  // namespace blackboxrs
