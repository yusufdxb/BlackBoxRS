// Runtime configuration of the live nodes (schema blackboxrs.runtime.v1).
//
// One YAML file selects the capture profile and sets everything the live
// runtime needs: capture mode, evidence location, queue and writer sizes,
// monitor thresholds, diagnostics, the forbidden-publish list and what
// preflight expects of the environment. It is validated completely before
// any node creates a subscription: an unknown key, a wrong type or an
// out-of-range value is an error that names the key. The file's SHA-256 is
// recorded in every manifest, so evidence says which configuration made it.
//
// The forbidden-publish list can be extended, never shortened: the built-in
// motion and control topics are always forbidden.
#pragma once

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "blackboxrs/json.hpp"
#include "blackboxrs/profile.hpp"

namespace blackboxrs {

class ConfigError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

inline constexpr const char* kRuntimeSchema = "blackboxrs.runtime.v1";

enum class CaptureMode : std::uint8_t { triggered, continuous };

struct RuntimeConfig {
  std::string path;
  std::string sha256;  // of the file text
  std::string text;

  // profile
  std::string profile_path;  // resolved (relative to the config file)
  Profile profile;

  // capture
  CaptureMode capture_mode = CaptureMode::triggered;
  std::string evidence_dir;  // expanded (~)
  std::optional<std::string> session_id;
  std::optional<std::string> experiment;
  std::int64_t hard_disk_floor_mb = 256;

  // queues and writer
  std::size_t ingest_capacity = 65'536;
  std::size_t control_reserve = 256;
  std::size_t writer_capacity = 65'536;
  std::chrono::milliseconds drain_deadline{3000};
  std::chrono::milliseconds fsync_every{1000};
  std::size_t chunk_records = 1024;
  std::size_t chunk_bytes = 1U << 20U;

  // subscriptions (BEST_EFFORT + VOLATILE always; only the depth is set)
  std::size_t subscription_depth = 200;

  // monitor
  bool monitor_enabled = true;
  double stop_grace_s = 0.05;
  double fresh_grace_s = 0.05;
  double clock_step_threshold_s = 0.2;
  double clock_offset_info_s = 0.5;
  double monitor_period_s = 0.02;
  std::map<std::string, double> command_sources;  // topic -> freshness window
  std::optional<std::string> findings_file;       // JSONL of online findings

  // diagnostics
  bool diagnostics_publish = true;
  double diagnostics_period_s = 1.0;

  // safety
  std::vector<std::string> forbidden_topics;    // built-ins + configured
  std::vector<std::string> forbidden_prefixes;  // built-ins + configured

  // ROS environment preflight expects
  std::string node_namespace = "/blackbox";
  std::optional<std::string> expect_rmw;
  std::optional<int> expect_domain_id;
  double preflight_listen_s = 3.0;
  double preflight_min_rate_fraction = 0.5;

  [[nodiscard]] Json to_json() const;
};

// Topics the runtime never publishes to, whatever the configuration says.
[[nodiscard]] const std::vector<std::string>& builtin_forbidden_topics();
[[nodiscard]] const std::vector<std::string>& builtin_forbidden_prefixes();
// Topics the runtime may publish to at all (diagnostics and its own namespace).
[[nodiscard]] bool publish_allowed(const RuntimeConfig& cfg, const std::string& resolved_topic,
                                   std::string* why = nullptr);

// Load and validate. Throws ConfigError naming the offending key.
[[nodiscard]] RuntimeConfig load_runtime_config(const std::string& path);

[[nodiscard]] std::string expand_user(const std::string& path);

}  // namespace blackboxrs
