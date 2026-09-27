// ros2 run blackboxrs_ros recorder --config configs/go2_hardware.yaml
//
// SIGINT/SIGTERM (rclcpp's handlers) stop the executor; the node then stops
// taking data, drains, finalizes its bundles and joins its threads before
// the process exits. SIGUSR1 adds an operator marker. Exit code 0 when every
// bundle was finalized, 1 when evidence could not be written completely, 5
// when the configuration is invalid (nothing is subscribed then).

#include <csignal>
#include <iostream>

#include "blackboxrs_ros/recorder_node.hpp"

namespace {
extern "C" void on_sigusr1(int) {
  blackboxrs_ros::RecorderNode::marker_requested().store(true);
}
}  // namespace

int main(int argc, char** argv) {
  const auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  const std::string path = blackboxrs_ros::config_path_from_args(args);
  if (path.empty()) {
    std::cerr << "usage: recorder --config RUNTIME.yaml [--ros-args ...]\n";
    rclcpp::shutdown();
    return 2;
  }
  blackboxrs::RuntimeConfig cfg;
  try {
    cfg = blackboxrs::load_runtime_config(path);
  } catch (const blackboxrs::ConfigError& exc) {
    std::cerr << "configuration rejected, nothing started: " << exc.what() << "\n";
    rclcpp::shutdown();
    return 5;
  }
  std::signal(SIGUSR1, on_sigusr1);
  int rc = 0;
  {
    auto node = std::make_shared<blackboxrs_ros::RecorderNode>(
        cfg, blackboxrs_ros::passive_node_options({}));
    rclcpp::executors::SingleThreadedExecutor exec;
    exec.add_node(node);
    exec.spin();  // until SIGINT / SIGTERM / rclcpp::shutdown
    exec.remove_node(node);
    node->stop("signal");
    const auto m = node->metrics();
    rc =
        (m.state == "failed" || m.writer.bundles_failed != 0 || m.writer.write_errors != 0) ? 1 : 0;
  }
  rclcpp::shutdown();
  return rc;
}
