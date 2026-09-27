// The only way BlackBoxRS nodes create publishers.
//
// The topic name is resolved first (namespace and remapping rules applied,
// exactly as the publisher would resolve it) and checked against the runtime
// configuration: the built-in motion and control topics and anything outside
// /diagnostics and /blackboxrs/... are refused before a publisher exists, so a
// remap such as `-r /blackboxrs/replay/timeline:=/cmd_vel` fails at startup
// without ever advertising on the robot's control topic. The created
// publisher's own resolved name is checked again, in case the two ever differ.
#pragma once

#include <memory>
#include <stdexcept>
#include <string>

#include <rclcpp/rclcpp.hpp>

#include "blackboxrs/runtime_config.hpp"

namespace blackboxrs_ros {

class ForbiddenPublisher : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

template <class MsgT>
typename rclcpp::Publisher<MsgT>::SharedPtr guarded_publisher(rclcpp::Node& node,
                                                              const blackboxrs::RuntimeConfig& cfg,
                                                              const std::string& topic,
                                                              const rclcpp::QoS& qos) {
  std::string why;
  const std::string resolved = node.get_node_topics_interface()->resolve_topic_name(topic);
  if (!blackboxrs::publish_allowed(cfg, resolved, &why)) {
    throw ForbiddenPublisher("refusing to publish on " + resolved + ": " + why);
  }
  auto pub = node.create_publisher<MsgT>(topic, qos);
  const std::string actual = pub->get_topic_name();
  if (!blackboxrs::publish_allowed(cfg, actual, &why)) {
    pub.reset();
    throw ForbiddenPublisher("refusing to publish on " + actual + ": " + why);
  }
  return pub;
}

}  // namespace blackboxrs_ros
