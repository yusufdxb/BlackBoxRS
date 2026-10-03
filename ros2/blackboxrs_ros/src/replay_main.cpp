// blackboxrs replay into ROS: OFFLINE analysis output, never robot input.
//
// ros2 run blackboxrs_ros replay <case.json | bundle> [--inject SPEC]... [--speed X]
//                                [--publish] [--config RUNTIME.yaml]
//
// Without --publish it creates no publisher at all and prints the result like
// the blackboxrs CLI. With --publish it publishes the causal timeline as it
// unfolds (std_msgs/String JSON on /blackboxrs/replay/timeline, paced by
// --speed) and the final result on /blackboxrs/replay/result. Replayed data is
// never republished on its original topics, and every publisher goes through
// the publish guard, so a remap onto a motion topic fails before it exists.

#include <iostream>
#include <memory>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/replay/engine.hpp"
#include "blackboxrs/replay/pacing.hpp"
#include "blackboxrs/replay/render.hpp"
#include "blackboxrs_ros/guarded_publisher.hpp"
#include "blackboxrs_ros/runtime.hpp"

int main(int argc, char** argv) {
  const auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  std::string target;
  std::vector<std::string> injects;
  double speed = 0.0;
  bool publish = false;
  std::string config_path;
  for (std::size_t i = 1; i < args.size(); ++i) {
    const std::string& a = args[i];
    if (a == "--inject" && i + 1 < args.size()) {
      injects.push_back(args[++i]);
    } else if (a == "--speed" && i + 1 < args.size()) {
      speed = std::stod(args[++i]);
    } else if (a == "--publish") {
      publish = true;
    } else if (a == "--config" && i + 1 < args.size()) {
      config_path = args[++i];
    } else if (target.empty() && a.rfind("--", 0) != 0) {
      target = a;
    } else {
      std::cerr << "unknown argument " << a << "\n";
      rclcpp::shutdown();
      return 2;
    }
  }
  if (target.empty()) {
    std::cerr << "usage: replay <case.json | bundle> [--inject SPEC]... [--speed X] [--publish]\n";
    rclcpp::shutdown();
    return 2;
  }
  namespace rp = blackboxrs::replay;
  int rc = 0;
  try {
    rp::ReplayConfig cfg;
    std::string evidence = target;
    if (target.size() > 5 && target.ends_with(".json")) {
      const rp::Case c = rp::load_case(target);
      cfg = c.config;
      evidence = c.evidence;
    }
    for (const auto& s : injects) {
      cfg.faults.push_back(rp::parse_cli_fault(s, cfg.faults.size()));
    }
    const blackboxrs::Evidence ev = blackboxrs::load_evidence(evidence, false, evidence);
    rp::ReplayOptions opts;
    if (speed > 0) {
      // Sleeps until each step's absolute deadline from one steady origin, so
      // publishing and sleep overshoot do not accumulate (replay/pacing.hpp).
      auto pacer = std::make_shared<rp::DeadlinePacer>(speed);
      opts.pacer = [pacer](blackboxrs::Nanos step) { pacer->pace(step); };
    }
    std::shared_ptr<rclcpp::Node> node;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr timeline_pub;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr result_pub;
    if (publish) {
      // The guard needs a runtime configuration for its forbidden list; the
      // built-in list applies when none is given.
      blackboxrs::RuntimeConfig guard_cfg;
      guard_cfg.forbidden_topics = blackboxrs::builtin_forbidden_topics();
      guard_cfg.forbidden_prefixes = blackboxrs::builtin_forbidden_prefixes();
      std::string ns = "/blackbox";
      if (!config_path.empty()) {
        guard_cfg = blackboxrs::load_runtime_config(config_path);
        ns = guard_cfg.node_namespace;
      }
      node = std::make_shared<rclcpp::Node>("blackboxrs_replay", ns,
                                            blackboxrs_ros::passive_node_options({}));
      timeline_pub = blackboxrs_ros::guarded_publisher<std_msgs::msg::String>(
          *node, guard_cfg, "/blackboxrs/replay/timeline", rclcpp::QoS(100));
      result_pub = blackboxrs_ros::guarded_publisher<std_msgs::msg::String>(
          *node, guard_cfg, "/blackboxrs/replay/result", rclcpp::QoS(1).transient_local());
      RCLCPP_INFO(node->get_logger(),
                  "BlackBoxRS REPLAY mode (offline evidence, publishing analysis on "
                  "/blackboxrs/replay/* only)");
      opts.observer = [timeline_pub](const rp::TimelineEntry& e) {
        std_msgs::msg::String s;
        s.data =
            blackboxrs::canonical_json({{"t_ns", e.t_ns}, {"layer", e.layer}, {"text", e.text}});
        timeline_pub->publish(s);
      };
    }
    const blackboxrs::Json result = rp::replay(ev, cfg, opts);
    std::cout << rp::render_text(result, !publish) << "\n";
    if (result_pub) {
      std_msgs::msg::String s;
      s.data = blackboxrs::canonical_json(result);
      result_pub->publish(s);
    }
    rc = static_cast<int>(rp::exit_code_for(result));
  } catch (const blackboxrs_ros::ForbiddenPublisher& exc) {
    std::cerr << "refused: " << exc.what() << "\n";
    rc = 5;
  } catch (const std::exception& exc) {
    std::cerr << "error: " << exc.what() << "\n";
    rc = 5;
  }
  rclcpp::shutdown();
  return rc;
}
