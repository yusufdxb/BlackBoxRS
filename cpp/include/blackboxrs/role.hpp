// Topic roles known to the flight recorder profiles.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string_view>

namespace blackboxrs {

enum class Role : std::uint8_t {
  sport_request,
  sport_response,
  odometry,
  go2_state,
  operator_remote,
  cmd_vel_out,
  cmd_vel_source,
  helix_fault,
  recovery_hint,
  recovery_action,
  helix_hold,
  arbiter_status,
  sink_trace,
  helix_health,
  helix_diagnosis_text,
  phoenix_safety,
  phoenix_action,
  phoenix_observation,
  other,
};

inline constexpr std::array<std::string_view, 19> kRoleNames{"sport_request",
                                                             "sport_response",
                                                             "odometry",
                                                             "go2_state",
                                                             "operator_remote",
                                                             "cmd_vel_out",
                                                             "cmd_vel_source",
                                                             "helix_fault",
                                                             "recovery_hint",
                                                             "recovery_action",
                                                             "helix_hold",
                                                             "arbiter_status",
                                                             "sink_trace",
                                                             "helix_health",
                                                             "helix_diagnosis_text",
                                                             "phoenix_safety",
                                                             "phoenix_action",
                                                             "phoenix_observation",
                                                             "other"};

constexpr std::string_view role_name(Role r) noexcept {
  return kRoleNames[static_cast<std::size_t>(r)];
}

constexpr std::optional<Role> parse_role(std::string_view s) noexcept {
  for (std::size_t i = 0; i < kRoleNames.size(); ++i) {
    if (kRoleNames[i] == s) {
      return static_cast<Role>(i);
    }
  }
  return std::nullopt;
}

// Roles published by the GO2 main computer (robot clock); everything else in
// the go2 profiles runs on the payload computer. Python evidence.ROBOT_ROLES.
constexpr bool is_robot_role(Role r) noexcept {
  return r == Role::odometry || r == Role::go2_state || r == Role::sport_response ||
         r == Role::operator_remote;
}

// Roles downstream of the arbiter (Python engine.DOWNSTREAM_ROLES).
constexpr bool is_downstream_role(Role r) noexcept {
  return r == Role::cmd_vel_out || r == Role::arbiter_status || r == Role::sink_trace ||
         r == Role::sport_request || r == Role::sport_response;
}

}  // namespace blackboxrs
