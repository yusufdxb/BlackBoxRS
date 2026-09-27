// Capture profile: which topics to observe, the incident window, triggers.
//
// C++ port of blackboxrs/flight/profile.py (parse_profile). A bundle embeds
// the resolved profile YAML text in its manifest, and the profile hash is the
// SHA-256 of that text, so a C++ replay and a Python replay of one bundle agree
// on which contract the evidence was captured under. `extends` is resolved by
// the Python loader before embedding; this parser refuses an unresolved one.
#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "blackboxrs/role.hpp"

namespace blackboxrs {

class ProfileError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct TopicSpec {
  std::string name;
  std::string type;
  Role role = Role::other;
  bool required = false;
  std::optional<double> expected_hz;
  std::optional<double> stale_after_sec;
  std::optional<double> store_max_hz;
  std::vector<std::string> fields;  // dotted paths; empty = whole message
};

struct BufferSpec {
  double pre_trigger_sec = 10.0;
  double post_trigger_sec = 15.0;
  std::int64_t max_records = 400'000;
  std::int64_t max_bytes = 256LL * 1024 * 1024;
};

struct SamplingSpec {
  double graph_poll_sec = 0.5;
  double system_sample_hz = 2.0;
  double health_tick_sec = 0.25;
};

struct TriggerSpec {
  bool helix_hold_asserted = true;
  bool recovery_action_stop = true;
  std::vector<std::string> recovery_actions{"STOP_AND_HOLD"};
  std::vector<std::string> recovery_statuses{"ACCEPTED"};
  bool arbiter_forced_zero = true;
  std::vector<std::string> arbiter_reasons{"HELIX_HOLD", "HELIX_STATE_STALE",
                                           "HELIX_STATE_MISSING"};
  bool node_disappeared = true;
  bool topic_stale = true;
  bool manual_marker = true;
  std::int64_t max_incidents_per_run = 20;
};

struct Profile {
  std::string name;
  std::string description;
  std::vector<TopicSpec> topics;
  std::string text;    // the YAML the profile was parsed from
  std::string sha256;  // SHA-256 of `text`
  std::string evidence_dir = "~/blackboxrs_evidence";
  std::int64_t min_free_disk_mb = 2048;
  BufferSpec buffer;
  SamplingSpec sampling;
  TriggerSpec triggers;
  std::vector<std::string> co_hosted_roles;
  std::vector<std::string> expected_nodes;

  [[nodiscard]] const TopicSpec* topic(std::string_view name) const noexcept;
};

// Parse profile YAML text. Throws ProfileError with the offending key.
[[nodiscard]] Profile parse_profile_text(const std::string& text);

}  // namespace blackboxrs
