// TEST TOOL, not installed: publishes GO2-shaped TELEMETRY at the measured
// rates, with the real message types (default-valued, so real CDR sizes and
// real decode cost), to benchmark the recorder on a workstation.
//
//   load_publisher --scale 1 --seconds 30
//
// Refuses to run unless ROS_LOCALHOST_ONLY=1 and ROS_DOMAIN_ID is set and
// non-zero, and refuses if any other node already publishes one of its
// topics (a real robot, or a HELIX stack). It never publishes a command or
// control topic: only /lowstate, /sportmodestate and /utlidar/robot_odom.

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <queue>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp/serialization.hpp>
#include <rclcpp/typesupport_helpers.hpp>
#include <rosidl_typesupport_introspection_cpp/message_introspection.hpp>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Load {
  std::string topic;
  std::string type;
  double hz;
};

// Measured on a GO2 EDU with the Orin NX payload (field notes 2026-09-01/02).
const std::vector<Load> kGo2Telemetry{{"/lowstate", "unitree_go/msg/LowState", 500.0},
                                      {"/sportmodestate", "unitree_go/msg/SportModeState", 295.0},
                                      {"/utlidar/robot_odom", "nav_msgs/msg/Odometry", 151.0}};

// A default-valued message of `type`, serialized.
rclcpp::SerializedMessage default_cdr(const std::string& type) {
  namespace ti = rosidl_typesupport_introspection_cpp;
  auto lib_cpp = rclcpp::get_typesupport_library(type, "rosidl_typesupport_cpp");
  const auto* ts_cpp = rclcpp::get_typesupport_handle(type, "rosidl_typesupport_cpp", *lib_cpp);
  auto lib_in = rclcpp::get_typesupport_library(type, "rosidl_typesupport_introspection_cpp");
  const auto* ts_in =
      rclcpp::get_typesupport_handle(type, "rosidl_typesupport_introspection_cpp", *lib_in);
  const auto* mm = static_cast<const ti::MessageMembers*>(ts_in->data);
  void* buf = ::operator new (mm->size_of_, std::align_val_t{alignof(std::max_align_t)});
  mm->init_function(buf, rosidl_runtime_cpp::MessageInitialization::ALL);
  rclcpp::SerializedMessage out;
  rclcpp::SerializationBase(ts_cpp).serialize_message(buf, &out);
  mm->fini_function(buf);
  ::operator delete (buf, std::align_val_t{alignof(std::max_align_t)});
  return out;
}

}  // namespace

int main(int argc, char** argv) {
  const auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  double scale = 1.0;
  double seconds = 10.0;
  for (std::size_t i = 1; i + 1 < args.size(); ++i) {
    if (args[i] == "--scale") scale = std::stod(args[i + 1]);
    if (args[i] == "--seconds") seconds = std::stod(args[i + 1]);
  }
  const char* local = std::getenv("ROS_LOCALHOST_ONLY");
  const char* domain = std::getenv("ROS_DOMAIN_ID");
  if (local == nullptr || std::string(local) != "1" || domain == nullptr ||
      std::atoi(domain) == 0) {
    std::cerr << "refusing: set ROS_LOCALHOST_ONLY=1 and a non-zero ROS_DOMAIN_ID\n";
    rclcpp::shutdown();
    return 3;
  }
  auto node = std::make_shared<rclcpp::Node>("bbrs_load_publisher");
  std::this_thread::sleep_for(std::chrono::seconds(2));  // discovery
  for (const auto& l : kGo2Telemetry) {
    if (node->count_publishers(l.topic) != 0) {
      std::cerr << "refusing: " << l.topic
                << " already has a publisher (a robot or a stack is running)\n";
      rclcpp::shutdown();
      return 3;
    }
  }
  struct Pub {
    rclcpp::GenericPublisher::SharedPtr pub;
    rclcpp::SerializedMessage msg;
    std::chrono::nanoseconds period;
    std::uint64_t sent = 0;
  };
  std::vector<Pub> pubs;
  for (const auto& l : kGo2Telemetry) {
    Pub p;
    p.pub = node->create_generic_publisher(l.topic, l.type, rclcpp::QoS(10).best_effort());
    p.msg = default_cdr(l.type);
    p.period = std::chrono::nanoseconds(static_cast<std::int64_t>(1e9 / (l.hz * scale)));
    std::cerr << l.topic << " " << l.type << " " << p.msg.size() << " bytes at " << l.hz * scale
              << " Hz\n";
    pubs.push_back(std::move(p));
  }
  std::this_thread::sleep_for(std::chrono::seconds(1));
  using Clock = std::chrono::steady_clock;
  const auto start = Clock::now();
  const auto end =
      start + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(seconds));
  using Next = std::pair<Clock::time_point, std::size_t>;
  std::priority_queue<Next, std::vector<Next>, std::greater<>> q;
  for (std::size_t i = 0; i < pubs.size(); ++i) {
    q.push({start, i});
  }
  while (!q.empty() && rclcpp::ok()) {
    auto [at, i] = q.top();
    q.pop();
    if (at >= end) continue;
    std::this_thread::sleep_until(at);
    pubs[i].pub->publish(pubs[i].msg);
    ++pubs[i].sent;
    q.push({at + pubs[i].period, i});
  }
  std::uint64_t total = 0;
  for (std::size_t i = 0; i < pubs.size(); ++i) {
    std::cout << kGo2Telemetry[i].topic << " sent " << pubs[i].sent << "\n";
    total += pubs[i].sent;
  }
  std::cout << "total sent " << total << " in "
            << std::chrono::duration<double>(Clock::now() - start).count() << " s\n";
  rclcpp::shutdown();
  return 0;
}
