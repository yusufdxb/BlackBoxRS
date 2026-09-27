// Typed views of message payloads.
//
// The recorded payload of a message is JSON (the durable evidence). Logic
// never reads that JSON: it reads one of the typed views below, built by
// decode_payload() from the topic's role. decode_payload() is the only place
// that knows how a stored value is interpreted, so the rules Python applies
// in several modules (as_number, "a bool is never a number", isinstance(int),
// str(value), truthiness) are implemented and tested once.
//
// A malformed field stays representable (Numeric::state, Flag, IntField):
// the arbitration model, the invariants and the recorder triggers each give
// it their own meaning, exactly as their Python counterparts do.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <variant>

#include "blackboxrs/json.hpp"
#include "blackboxrs/role.hpp"

namespace blackboxrs {

// A JSON value read as a flag. Python code reads flags two ways:
// isinstance(v, bool) (the arbiter, the monitors) and bool(v) (recorder
// triggers). Both answers are kept.
enum class Flag : std::uint8_t { missing, is_false, is_true, other_falsy, other_truthy };

[[nodiscard]] constexpr bool flag_is_bool(Flag f) noexcept {
  return f == Flag::is_false || f == Flag::is_true;
}
[[nodiscard]] constexpr bool flag_truthy(Flag f) noexcept {
  return f == Flag::is_true || f == Flag::other_truthy;
}

// An integer field read the Python way: isinstance(v, int) and not a bool.
struct IntField {
  std::optional<std::int64_t> value;  // set only for a strict integer
  Json raw;                           // the stored value (null when missing), for equality
  friend bool operator==(const IntField&, const IntField&) = default;
};

// A text field read as Python's str(d.get(key, "")). A non-string value keeps
// its JSON text (str() of a dict or a float differs in punctuation; only
// display and trigger bookkeeping use these fields).
struct TextField {
  std::string text;
  bool is_string = false;  // the stored value was a JSON string
  bool present = false;
  friend bool operator==(const TextField&, const TextField&) = default;
};

// geometry_msgs/Twist: every command source and the recorded /cmd_vel.
struct VelocityCommand {
  // linear.x, linear.y, linear.z, angular.x, angular.y, angular.z
  std::array<Numeric, 6> axes{};
  // The stored values of linear.x, linear.y, angular.z exactly as recorded
  // (a forwarding mux such as twist_mux passes them through unchanged).
  std::array<Json, 3> forwarded{};

  [[nodiscard]] const Numeric& lx() const noexcept { return axes[0]; }
  [[nodiscard]] const Numeric& ly() const noexcept { return axes[1]; }
  [[nodiscard]] const Numeric& az() const noexcept { return axes[5]; }
};

inline constexpr std::array<const char*, 6> kTwistAxes{"linear.x",  "linear.y",  "linear.z",
                                                       "angular.x", "angular.y", "angular.z"};

// helix_msgs/HelixHold: the HELIX hold (STOP) state.
struct HoldState {
  Flag hold = Flag::missing;
  IntField epoch;
  IntField seq;
  TextField fault_id;
  TextField reason;
};

// helix_msgs/ArbiterStatus.
struct ArbiterStatus {
  TextField reason;
  TextField selected_source;
  TextField hold_fault_id;
  Flag hold_active = Flag::missing;
  std::array<Json, 3> out_raw{};  // out_linear_x, out_linear_y, out_angular_z as stored
  std::array<Numeric, 3> out{};
  IntField seq;
};

// nav_msgs/Odometry, the fields the consistency checks use.
struct Odometry {
  Numeric x, y, vx, vy;  // pose.pose.position.{x,y}, twist.twist.linear.{x,y}
};

// helix_msgs/RecoveryAction.
struct RecoveryAction {
  TextField action;
  TextField status;
  TextField fault_id;
};

// Every other role: captured and counted, not interpreted.
struct OpaquePayload {};

using TypedPayload = std::variant<OpaquePayload, VelocityCommand, HoldState, ArbiterStatus,
                                  Odometry, RecoveryAction>;

// Build the typed view of a stored payload for a topic of role `role`.
[[nodiscard]] TypedPayload decode_payload(Role role, const Json& data);

[[nodiscard]] Flag read_flag(const Json* v) noexcept;
[[nodiscard]] IntField read_int(const Json* v);
[[nodiscard]] TextField read_text(const Json* v);

// Python truthiness of a JSON value.
[[nodiscard]] bool json_truthy(const Json& v) noexcept;

}  // namespace blackboxrs
