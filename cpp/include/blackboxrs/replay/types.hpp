// Shared replay types: topic metadata, findings, invariant states, decisions.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

#include "blackboxrs/json.hpp"
#include "blackboxrs/role.hpp"
#include "blackboxrs/time.hpp"

namespace blackboxrs::replay {

enum class Liveness : std::uint8_t { periodic, event };

// What the lab needs to know about a topic beyond its records
// (Python lab.evidence.TopicInfo).
struct TopicInfo {
  std::string name;
  std::string role_name;
  Role role = Role::other;
  std::string host;  // "payload", "robot", or a case-defined name
  Liveness liveness = Liveness::event;
  std::optional<double> stale_after_s;
};

using TopicTable = std::map<std::string, TopicInfo>;

enum class Severity : std::uint8_t { info, warning, critical };

[[nodiscard]] constexpr const char* severity_name(Severity s) noexcept {
  switch (s) {
    case Severity::info: return "info";
    case Severity::warning: return "warning";
    case Severity::critical: return "critical";
  }
  return "info";
}

// A detector or invariant observation. `data` is descriptive output only;
// nothing downstream branches on it.
struct Finding {
  ReplayTime t{};
  std::string monitor;
  std::string kind;
  Severity severity = Severity::info;
  std::string subject;
  std::string message;
  std::vector<std::string> evidence;  // replay event ids
  std::optional<std::string> invariant;
  Json data = Json::object();

  // Total order used to assign stable ids (Python Finding.sort_key).
  [[nodiscard]] auto sort_key() const { return std::tie(t, monitor, kind, subject, evidence); }
};

enum class InvariantStatus : std::uint8_t { pass, fail, incomplete, not_exercised };

[[nodiscard]] constexpr const char* status_name(InvariantStatus s) noexcept {
  switch (s) {
    case InvariantStatus::pass: return "PASS";
    case InvariantStatus::fail: return "FAIL";
    case InvariantStatus::incomplete: return "INCOMPLETE";
    case InvariantStatus::not_exercised: return "NOT_EXERCISED";
  }
  return "INCOMPLETE";
}

// One safety invariant. A missing observation is never PASS: an invariant
// that was never checked is NOT_EXERCISED, and one that could not be checked
// is INCOMPLETE.
struct InvariantState {
  std::string name;
  std::string statement;
  std::int64_t checks = 0;
  std::int64_t violations = 0;
  bool exercised = false;
  std::optional<ReplayTime> first_violation;
  std::int64_t episodes = 0;
  std::optional<std::string> incomplete_reason;

  [[nodiscard]] InvariantStatus status() const noexcept {
    if (violations > 0) {
      return InvariantStatus::fail;
    }
    if (incomplete_reason) {
      return InvariantStatus::incomplete;
    }
    if (!exercised) {
      return InvariantStatus::not_exercised;
    }
    return InvariantStatus::pass;
  }
  void record_violation(ReplayTime t) {
    ++violations;
    if (!first_violation) {
      first_violation = t;
    }
  }
};

// A robot-facing command in (vx, vy, wz).
struct Command {
  double vx = 0.0;
  double vy = 0.0;
  double wz = 0.0;
  [[nodiscard]] bool is_zero() const noexcept { return vx == 0.0 && vy == 0.0 && wz == 0.0; }
  [[nodiscard]] std::array<double, 3> as_array() const noexcept { return {vx, vy, wz}; }
  friend bool operator==(const Command&, const Command&) = default;
};

// The command as published at the robot-facing boundary. A forwarding mux can
// publish anything a source sent, NaN or a string included, so the raw value
// of each axis is kept.
using RawCommand = std::array<Json, 3>;

[[nodiscard]] RawCommand raw_of(const Command& c);
[[nodiscard]] Json raw_json(const std::optional<RawCommand>& raw);  // list or null

// Arbitration state at one instant, and what the robot-facing sink holds
// (Python lab.sut.Decision).
struct Decision {
  ReplayTime t{};
  std::string reason;
  std::string source;
  std::optional<Command> robot_cmd;  // set when every axis is a finite number
  std::optional<RawCommand> robot_raw;
  bool published = false;
  std::optional<bool> hold;
  std::string cause;  // event id, or "clock"
};

}  // namespace blackboxrs::replay
