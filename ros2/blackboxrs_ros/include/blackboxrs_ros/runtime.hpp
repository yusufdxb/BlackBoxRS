// Shared by the BlackBoxRS nodes: passive node options, the ROS graph probe,
// session identity and manifest context.
#pragma once

#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "blackboxrs/json.hpp"
#include "blackboxrs/recorder/recorder.hpp"
#include "blackboxrs/runtime_config.hpp"

namespace blackboxrs_ros {

// Node options for an observation-only node: no parameter services, no
// parameter-event publisher, no /rosout publisher. With diagnostics disabled
// such a node has no publishers at all.
[[nodiscard]] rclcpp::NodeOptions passive_node_options(const std::vector<std::string>& ros_args);

// "--config PATH" from the non-ROS arguments, or the BLACKBOXRS_CONFIG
// environment variable. Empty when neither is given.
[[nodiscard]] std::string config_path_from_args(const std::vector<std::string>& args);

// Per-topic graph state as the Python recorder reports it (flight/recorder.py
// _poll_graph): absent, no_publishers, type_mismatch, subscribed,
// type_unavailable; publishers and their types and QoS; left_graph.
class GraphProbe {
 public:
  GraphProbe(const blackboxrs::Profile& profile, std::string own_namespace);
  void mark_type_unavailable(const std::string& topic, const std::string& why);
  [[nodiscard]] blackboxrs::recorder::GraphSnapshot probe(rclcpp::Node& node);
  [[nodiscard]] const std::map<std::string, blackboxrs::Json>& status() const { return status_; }
  // Publishers of `topic` other than BlackBoxRS's own nodes.
  [[nodiscard]] static std::vector<rclcpp::TopicEndpointInfo> publishers(
      rclcpp::Node& node, const std::string& topic, const std::string& own_namespace);

 private:
  const blackboxrs::Profile& profile_;
  std::string own_ns_;
  std::map<std::string, blackboxrs::Json> status_;
};

[[nodiscard]] std::string qos_string(const rclcpp::QoS& q);

// UTC session id, e.g. 20260927T184520Z_<host>.
[[nodiscard]] std::string make_session_id();
[[nodiscard]] std::string hostname();

// Session and manifest context for a recorder run.
[[nodiscard]] blackboxrs::recorder::ManifestContext manifest_context(
    const blackboxrs::RuntimeConfig& cfg, const std::string& session_id, const std::string& mode,
    bool use_sim_time);

[[nodiscard]] blackboxrs::Json build_json();

}  // namespace blackboxrs_ros
