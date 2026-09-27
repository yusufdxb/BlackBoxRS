// blackboxrs_recorder: passive flight recorder node.
//
// Subscribes (BEST_EFFORT, VOLATILE, KEEP_LAST(depth)) to every profile topic
// whose type support is installed, stamps each message with the recorder's
// clocks and its two DDS times, and hands it to the ROS-free Recorder
// pipeline. Its only publishers are diagnostics on /diagnostics and a JSON
// status on /blackboxrs/status, both created through the publish guard, and
// none at all when diagnostics.publish is false. It never publishes to a
// motion or control topic: that is enforced, not assumed (guarded_publisher,
// test_passive_nodes).
#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <vector>

#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>

#include "blackboxrs/recorder/recorder.hpp"
#include "blackboxrs/runtime_config.hpp"
#include "blackboxrs_ros/observed_subscription.hpp"
#include "blackboxrs_ros/runtime.hpp"

namespace blackboxrs_ros {

class RecorderNode final : public rclcpp::Node {
 public:
  RecorderNode(blackboxrs::RuntimeConfig cfg, const rclcpp::NodeOptions& options,
               std::string session_id = {});
  ~RecorderNode() override;
  RecorderNode(const RecorderNode&) = delete;
  RecorderNode& operator=(const RecorderNode&) = delete;
  RecorderNode(RecorderNode&&) = delete;
  RecorderNode& operator=(RecorderNode&&) = delete;

  // Stop taking data, drain, finalize every bundle. Idempotent; the
  // destructor calls it.
  void stop(const std::string& reason);
  void request_marker(std::string note) { recorder_->mark(std::move(note), "operator"); }

  [[nodiscard]] blackboxrs::recorder::RecorderMetrics metrics() const {
    return recorder_->metrics();
  }
  [[nodiscard]] std::vector<std::string> bundles() const { return recorder_->bundles(); }
  [[nodiscard]] const std::string& session_dir() const noexcept { return session_dir_; }
  [[nodiscard]] std::size_t subscribed_topics() const noexcept { return subscriptions_.size(); }
  [[nodiscard]] std::pair<int, int> recorder_thread_ids() const noexcept {
    return recorder_->thread_ids();
  }

  // Set from a signal handler (SIGUSR1): a marker is added at the next graph poll.
  static std::atomic<bool>& marker_requested();

 private:
  void poll_graph();
  void poll_graph_once();
  void publish_diagnostics();

  blackboxrs::RuntimeConfig cfg_;
  std::string session_dir_;
  std::unique_ptr<GraphProbe> graph_;
  std::unique_ptr<blackboxrs::recorder::Recorder> recorder_;
  std::vector<std::shared_ptr<ObservedSubscription>> subscriptions_;
  std::vector<std::unique_ptr<std::atomic<std::uint64_t>>> message_lost_;
  rclcpp::TimerBase::SharedPtr graph_timer_;
  rclcpp::TimerBase::SharedPtr diag_timer_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diag_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr status_pub_;
  bool stopped_ = false;
};

}  // namespace blackboxrs_ros
