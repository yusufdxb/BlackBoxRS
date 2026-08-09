// Copyright 2026 Yusuf Guenena
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
// THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#include "blackbox_capture/recorder.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <iomanip>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/metrics.hpp"
#include "blackbox_capture/payload_arena.hpp"
#include "blackbox_capture/ring_buffer.hpp"
#include "blackbox_capture/segment_writer.hpp"
#include "blackbox_capture/topic_registry.hpp"
#include "blackbox_capture/trigger_engine.hpp"
#include "rcl/time.h"
#include "rclcpp/callback_group.hpp"
#include "rclcpp/create_timer.hpp"
#include "rclcpp/exceptions.hpp"
#include "rclcpp/generic_subscription.hpp"
#include "rclcpp/qos.hpp"
#include "rclcpp/qos_event.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "std_msgs/msg/string.hpp"

namespace blackbox_capture
{
namespace
{

using namespace std::chrono_literals;

uint64_t monotonic_now_ns() noexcept
{
  return static_cast<uint64_t>(
    std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch())
    .count());
}

int64_t system_now_ns() noexcept
{
  return static_cast<int64_t>(
    std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch())
    .count());
}

std::string json_escape(std::string_view value)
{
  std::ostringstream stream;
  for (const char raw_character : value) {
    const auto character = static_cast<unsigned char>(raw_character);
    switch (character) {
      case '"':
        stream << "\\\"";
        break;
      case '\\':
        stream << "\\\\";
        break;
      case '\b':
        stream << "\\b";
        break;
      case '\f':
        stream << "\\f";
        break;
      case '\n':
        stream << "\\n";
        break;
      case '\r':
        stream << "\\r";
        break;
      case '\t':
        stream << "\\t";
        break;
      default:
        if (character < 0x20U) {
          stream << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                 << static_cast<unsigned int>(character) << std::dec;
        } else {
          stream << static_cast<char>(character);
        }
    }
  }
  return stream.str();
}

std::filesystem::path expand_user_path(const std::string & value)
{
  if (value.empty() || value.front() != '~') {
    return value;
  }
  if (value.size() > 1U && value[1] != '/') {
    throw std::invalid_argument("only current-user '~/' paths are supported");
  }
  const char * home = std::getenv("HOME");
  if (home == nullptr || home[0] == '\0') {
    throw std::invalid_argument("HOME is unavailable for storage path expansion");
  }
  if (value.size() == 1U) {
    return std::filesystem::path(home);
  }
  return std::filesystem::path(home) / value.substr(2U);
}

template<typename Target>
Target checked_positive(int64_t value, const char * name)
{
  if (value <= 0 || static_cast<uint64_t>(value) >
    static_cast<uint64_t>(std::numeric_limits<Target>::max()))
  {
    throw std::invalid_argument(std::string(name) + " is outside the supported range");
  }
  return static_cast<Target>(value);
}

uint64_t checked_seconds_ns(double value, const char * name)
{
  constexpr double kNanosecondsPerSecond = 1.0e9;
  const double limit = static_cast<double>(std::numeric_limits<uint64_t>::max()) /
    kNanosecondsPerSecond;
  if (!std::isfinite(value) || value <= 0.0 || value > limit) {
    throw std::invalid_argument(std::string(name) + " is outside the supported range");
  }
  return static_cast<uint64_t>(value * kNanosecondsPerSecond);
}

uint64_t checked_add(uint64_t left, uint64_t right, const char * name)
{
  if (right > UINT64_MAX - left) {
    throw std::invalid_argument(std::string(name) + " overflows");
  }
  return left + right;
}

uint64_t checked_multiply(uint64_t left, uint64_t right, const char * name)
{
  if (left != 0U && right > UINT64_MAX / left) {
    throw std::invalid_argument(std::string(name) + " overflows");
  }
  return left * right;
}

template<std::size_t Size>
bool copy_fixed(std::array<char, Size> & output, std::string_view input) noexcept
{
  if (input.size() >= Size) {
    return false;
  }
  std::memcpy(output.data(), input.data(), input.size());
  output[input.size()] = '\0';
  return true;
}

const char * state_name(uint8_t state) noexcept
{
  switch (state) {
    case 0:
      return "STARTING";
    case 1:
      return "NORMAL";
    case 2:
      return "HIGH_WATERMARK";
    case 3:
      return "SHEDDING";
    case 4:
      return "STORAGE_FAULT";
    case 5:
      return "DRAINING";
    case 6:
      return "STOPPED_CLEAN";
    case 7:
      return "STOPPED_INCOMPLETE";
    default:
      return "INVARIANT_FAULT";
  }
}

std::string make_session_id()
{
  const auto epoch_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::system_clock::now().time_since_epoch())
    .count();
  std::ostringstream stream;
  stream << epoch_ns << '_' << static_cast<int64_t>(::getpid());
  return stream.str();
}

bool write_all(int descriptor, std::string_view content) noexcept
{
  std::size_t offset = 0U;
  while (offset < content.size()) {
    const ssize_t result =
      ::write(descriptor, content.data() + offset, content.size() - offset);
    if (result > 0) {
      offset += static_cast<std::size_t>(result);
      continue;
    }
    if (result < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
  return true;
}

bool write_atomic_text(const std::filesystem::path & path, std::string_view content)
{
  const std::filesystem::path partial = path.string() + ".partial";
  const int descriptor = ::open(partial.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);
  if (descriptor < 0) {
    return false;
  }
  const bool written = write_all(descriptor, content);
  const bool synced = written && ::fsync(descriptor) == 0;
  const bool closed = ::close(descriptor) == 0;
  if (!synced || !closed || ::rename(partial.c_str(), path.c_str()) != 0) {
    (void)::unlink(partial.c_str());
    return false;
  }
  const int directory = ::open(path.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (directory < 0) {
    return false;
  }
  const bool directory_synced = ::fsync(directory) == 0;
  const bool directory_closed = ::close(directory) == 0;
  return directory_synced && directory_closed;
}

}  // namespace

class RecorderNode::Impl
{
public:
  explicit Impl(RecorderNode & node)
  : node_(node)
  {
    load_parameters();
    initialize_core();
    start();
  }

  ~Impl() {(void)drain_and_stop(drain_timeout_);}

  Impl(const Impl &) = delete;
  Impl & operator=(const Impl &) = delete;

  void request_stop() noexcept
  {
    accepting_.store(false, std::memory_order_release);
    stop_requested_.store(true, std::memory_order_release);
  }

  bool drain_and_stop_configured() noexcept
  {
    return drain_and_stop(drain_timeout_);
  }

  bool drain_and_stop(std::chrono::milliseconds timeout) noexcept
  {
    bool expected = false;
    if (!stop_started_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
      std::unique_lock<std::mutex> lock(stop_mutex_);
      stop_cv_.wait(
        lock, [this]() {
          return stop_finished_.load(std::memory_order_acquire);
        });
      return stop_clean_.load(std::memory_order_acquire);
    }

    request_stop();
    state_.store(kDraining, std::memory_order_release);

    // The callback group is mutually exclusive, but a composed executor may
    // still be executing its current callback when another thread requests a
    // stop. Closing admission first and taking the producer token establishes
    // a quiescent point before callback-owned containers are cleared.
    while (producer_active_.test_and_set(std::memory_order_acquire)) {
      std::this_thread::yield();
    }
    if (discovery_timer_) {
      discovery_timer_->cancel();
    }
    if (trigger_timer_) {
      trigger_timer_->cancel();
    }
    if (status_timer_) {
      status_timer_->cancel();
    }
    subscriptions_.clear();
    producer_active_.clear(std::memory_order_release);

    graph_running_.store(false, std::memory_order_release);
    if (graph_thread_.joinable()) {
      graph_thread_.join();
    }

    const uint64_t timeout_ns = static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(timeout).count());
    drain_deadline_ns_.store(monotonic_now_ns() + timeout_ns, std::memory_order_release);
    writer_running_.store(false, std::memory_order_release);
    writer_cv_.notify_all();
    if (writer_thread_.joinable()) {
      writer_thread_.join();
    }
    drain_reclaimed();

    const bool clean = writer_clean_.load(std::memory_order_acquire) && event_ring_->empty();
    stop_clean_.store(clean, std::memory_order_release);
    state_.store(clean ? kStoppedClean : kStoppedIncomplete, std::memory_order_release);
    publish_status();
    try {
      const std::string final_status = status_json();
      RCLCPP_INFO(node_.get_logger(), "FINAL_STATUS %s", final_status.c_str());
    } catch (...) {
      status_publish_failures_.fetch_add(1U, std::memory_order_relaxed);
    }
    {
      std::lock_guard<std::mutex> lock(stop_mutex_);
      stop_finished_.store(true, std::memory_order_release);
    }
    stop_cv_.notify_all();
    return clean;
  }

  std::string status_json() const
  {
    const MetricsSnapshot metrics = metrics_->aggregate_snapshot();
    std::ostringstream stream;
    stream << '{'
           << "\"schema_version\":\"blackboxrs.capture_status.v1\","
           << "\"state\":\"" << state_name(state_.load(std::memory_order_acquire)) << "\","
           << "\"session_id\":\"" << json_escape(session_id_) << "\","
           << "\"backend\":\"cpp\","
           << "\"role\":\"" << json_escape(runtime_role_) << "\","
           << "\"observed_host\":\"" << json_escape(observed_host_) << "\","
           << "\"received\":" << metrics.received << ','
           << "\"admitted\":" << metrics.admitted << ','
           << "\"committed\":" << metrics.committed << ','
           << "\"durable\":" << metrics.durable << ','
           << "\"dropped\":" << metrics.dropped << ','
           << "\"dropped_bytes\":" << metrics.dropped_bytes << ','
           << "\"queue_depth\":" << event_ring_->size() << ','
           << "\"queue_capacity\":" << event_ring_->capacity() << ','
           << "\"queue_peak\":" << metrics.peak_queue_depth << ','
           << "\"storage_errors\":" << metrics.storage_errors << ','
           << "\"clock_anomalies\":" << metrics.clock_anomalies << ','
           << "\"status_publish_failures\":"
           << status_publish_failures_.load(std::memory_order_acquire) << ','
           << "\"graph_wait_faults\":"
           << graph_wait_faults_.load(std::memory_order_acquire) << ','
           << "\"graph_coverage_faults\":"
           << graph_coverage_faults_.load(std::memory_order_acquire) << ','
           << "\"graph_snapshot_failures\":"
           << graph_snapshot_failures_.load(std::memory_order_acquire) << ','
           << "\"node_snapshot_failures\":"
           << node_snapshot_failures_.load(std::memory_order_acquire) << ','
           << "\"endpoint_query_failures\":"
           << endpoint_query_failures_.load(std::memory_order_acquire) << ','
           << "\"subscription_failures\":"
           << subscription_failures_.load(std::memory_order_acquire) << ','
           << "\"runtime_callback_faults\":"
           << runtime_callback_faults_.load(std::memory_order_acquire) << ','
           << "\"rmw_messages_lost\":"
           << rmw_messages_lost_.load(std::memory_order_acquire) << ','
           << "\"rmw_event_callbacks_unavailable\":"
           << rmw_event_callbacks_unavailable_.load(std::memory_order_acquire) << ','
           << "\"incompatible_qos_events\":"
           << incompatible_qos_events_.load(std::memory_order_acquire) << ','
           << "\"ambiguous_topic_types\":"
           << ambiguous_topic_types_.load(std::memory_order_acquire) << ','
           << "\"best_effort_topics\":"
           << best_effort_topics_.load(std::memory_order_acquire) << ','
           << "\"topic_coverage_truncated\":"
           << (topic_coverage_truncated_.load(std::memory_order_acquire) ? "true" : "false")
           << ','
           << "\"node_coverage_truncated\":"
           << (node_coverage_truncated_.load(std::memory_order_acquire) ? "true" : "false")
           << ','
           << "\"incident_manifest_errors\":"
           << incident_manifest_errors_.load(std::memory_order_acquire) << ','
           << "\"rolling_segments\":"
           << rolling_segment_count_status_.load(std::memory_order_acquire) << ','
           << "\"rolling_segment_bytes\":"
           << rolling_segment_bytes_status_.load(std::memory_order_acquire) << ','
           << "\"retention_evicted_segments\":"
           << retention_evicted_segments_status_.load(std::memory_order_acquire) << ','
           << "\"retention_evicted_events\":"
           << retention_evicted_events_status_.load(std::memory_order_acquire) << ','
           << "\"retention_evicted_bytes\":"
           << retention_evicted_bytes_status_.load(std::memory_order_acquire) << ','
           << "\"retention_max_segments\":" << retention_max_segments_ << ','
           << "\"retention_max_bytes\":" << retention_max_bytes_ << ','
           << "\"drop_breakdown\":";
    append_drop_breakdown(stream);
    stream << ','
           << "\"last_sequence\":" << sequence_.load(std::memory_order_acquire) << ','
           << "\"capture_memory_budget_bytes\":" << capture_memory_budget_bytes_ << ','
           << "\"configured_memory_budget_bytes\":" << configured_memory_budget_bytes_ << ','
           << "\"capture_started_monotonic_ns\":" << capture_started_monotonic_ns_ << ','
           << "\"capture_observed_monotonic_ns\":" << monotonic_now_ns() << ','
           << "\"delivery_scope\":\"callback_received\","
           << "\"graph_scope\":\"" << (discover_all_ ? "all_bounded" : "configured") << "\""
           << '}';
    return stream.str();
  }

private:
  enum State : uint8_t
  {
    kStarting = 0,
    kNormal = 1,
    kHighWatermark = 2,
    kShedding = 3,
    kStorageFault = 4,
    kDraining = 5,
    kStoppedClean = 6,
    kStoppedIncomplete = 7,
    kInvariantFault = 8,
  };

  struct ProducerGuard
  {
    explicit ProducerGuard(std::atomic_flag & flag)
    : flag_(flag)
    {
      acquired = !flag_.test_and_set(std::memory_order_acquire);
    }
    ~ProducerGuard()
    {
      if (acquired) {
        flag_.clear(std::memory_order_release);
      }
    }
    std::atomic_flag & flag_;
    bool acquired{false};
  };

  struct SubscriptionState
  {
    std::string type;
    uint32_t topic_id{0};
    uint64_t qos_signature{0};
    bool best_effort{false};
    std::shared_ptr<rclcpp::GenericSubscription> subscription;
  };

  struct GraphTopic
  {
    std::string type;
    std::size_t publishers{0};
    std::size_t subscribers{0};
    uint64_t qos_signature{0};
  };

  struct TopicCommand
  {
    uint32_t topic_id{0};
    std::array<char, 256> topic{};
    std::array<char, 256> type{};
    std::array<char, 16> serialization{};
    std::array<char, 768> qos{};
  };

  struct IncidentRecord
  {
    std::filesystem::path path;
    uint64_t trigger_sequence{0};
  };

  void load_parameters()
  {
    runtime_role_ = node_.declare_parameter<std::string>("runtime.role", "onboard");
    observed_host_ = node_.declare_parameter<std::string>("runtime.observed_host", "");
    if (runtime_role_ != "onboard" && runtime_role_ != "observer") {
      throw std::invalid_argument("runtime.role must be onboard or observer");
    }

    configured_topics_ = node_.declare_parameter<std::vector<std::string>>(
      "capture.topics", std::vector<std::string>{});
    discover_all_ = node_.declare_parameter<bool>("capture.discover_all", false);
    excluded_topics_ = node_.declare_parameter<std::vector<std::string>>(
      "capture.exclude_topics",
      std::vector<std::string>{"/rosout", "/parameter_events", "/blackbox/capture_status"});
    high_priority_topics_ = node_.declare_parameter<std::vector<std::string>>(
      "capture.high_priority_topics", std::vector<std::string>{"/tf_static"});

    discovery_period_ = std::chrono::milliseconds(
      checked_positive<int64_t>(
        node_.declare_parameter<int64_t>("capture.discovery_period_ms", 100),
        "capture.discovery_period_ms"));
    max_topics_ = checked_positive<uint32_t>(
      node_.declare_parameter<int64_t>("capture.max_topics", 1024), "capture.max_topics");
    max_graph_nodes_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("capture.max_graph_nodes", 2048),
      "capture.max_graph_nodes");
    topic_string_bytes_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("capture.topic_string_bytes", 262144),
      "capture.topic_string_bytes");
    max_payload_bytes_ = checked_positive<uint32_t>(
      node_.declare_parameter<int64_t>("capture.max_payload_bytes", 4194304),
      "capture.max_payload_bytes");
    subscription_depth_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("capture.subscription_depth", 1000),
      "capture.subscription_depth");
    resolve_topic_configuration();
    if (configured_topics_.size() > static_cast<std::size_t>(max_topics_)) {
      throw std::invalid_argument("capture.topics exceeds capture.max_topics");
    }

    event_capacity_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("buffer.event_capacity", 16384),
      "buffer.event_capacity");
    control_reserve_ = static_cast<std::size_t>(
      node_.declare_parameter<int64_t>("buffer.control_reserve", 256));
    if (control_reserve_ >= event_capacity_) {
      throw std::invalid_argument("buffer.control_reserve must be smaller than capacity");
    }
    payload_block_size_ = checked_positive<uint32_t>(
      node_.declare_parameter<int64_t>("buffer.payload_block_size", 4096),
      "buffer.payload_block_size");
    payload_block_count_ = checked_positive<uint32_t>(
      node_.declare_parameter<int64_t>("buffer.payload_block_count", 16384),
      "buffer.payload_block_count");
    configured_memory_budget_bytes_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("buffer.memory_budget_bytes", 134217728),
      "buffer.memory_budget_bytes");
    high_watermark_ratio_ = node_.declare_parameter<double>("buffer.high_watermark_ratio", 0.8);
    if (!(high_watermark_ratio_ > 0.0 && high_watermark_ratio_ < 1.0)) {
      throw std::invalid_argument("buffer.high_watermark_ratio must be between zero and one");
    }

    output_directory_ = expand_user_path(
      node_.declare_parameter<std::string>("storage.output_directory", "~/.blackboxrs/native"));
    segment_max_bytes_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("storage.segment_max_bytes", 268435456),
      "storage.segment_max_bytes");
    segment_max_events_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("storage.segment_max_events", 1000000),
      "storage.segment_max_events");
    segment_max_duration_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("storage.segment_max_duration_sec", 5.0),
      "storage.segment_max_duration_sec");
    chunk_size_bytes_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("storage.chunk_size_bytes", 1048576),
      "storage.chunk_size_bytes");
    retention_max_bytes_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("storage.retention_max_bytes", 2147483648LL),
      "storage.retention_max_bytes");
    retention_max_segments_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("storage.retention_max_segments", 256),
      "storage.retention_max_segments");
    max_incidents_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("storage.max_incidents", 20),
      "storage.max_incidents");
    total_max_bytes_ = checked_positive<uint64_t>(
      node_.declare_parameter<int64_t>("storage.total_max_bytes", 53687091200LL),
      "storage.total_max_bytes");
    max_sessions_ = checked_positive<std::size_t>(
      node_.declare_parameter<int64_t>("storage.max_sessions", 10),
      "storage.max_sessions");
    if (retention_max_bytes_ > UINT64_MAX / (max_incidents_ + 1U)) {
      throw std::invalid_argument("storage retention and incident limits overflow");
    }
    reserved_session_bytes_ =
      retention_max_bytes_ * (static_cast<uint64_t>(max_incidents_) + 1U);
    if (segment_max_bytes_ > UINT64_MAX - reserved_session_bytes_) {
      throw std::invalid_argument("storage session reserve overflows");
    }
    reserved_session_bytes_ += segment_max_bytes_;
    if (total_max_bytes_ < reserved_session_bytes_) {
      throw std::invalid_argument(
              "storage.total_max_bytes is smaller than the configured session reserve");
    }
    flush_period_ = std::chrono::milliseconds(
      checked_positive<int64_t>(
        node_.declare_parameter<int64_t>("storage.flush_period_ms", 250),
        "storage.flush_period_ms"));
    failure_delay_ = std::chrono::milliseconds(
      std::max<int64_t>(
        0, node_.declare_parameter<int64_t>(
          "storage.failure_injection_delay_ms", 0)));
    failure_after_bytes_ = node_.declare_parameter<int64_t>(
      "storage.failure_injection_fail_after_bytes", -1);

    dead_topic_timeout_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("trigger.dead_topic_timeout_sec", 2.0),
      "trigger.dead_topic_timeout_sec");
    clock_forward_jump_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("trigger.clock_forward_jump_sec", 1.0),
      "trigger.clock_forward_jump_sec");
    clock_backward_jump_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("trigger.clock_backward_jump_sec", 0.001),
      "trigger.clock_backward_jump_sec");
    if (clock_forward_jump_ns_ > static_cast<uint64_t>(INT64_MAX) ||
      clock_backward_jump_ns_ > static_cast<uint64_t>(INT64_MAX))
    {
      throw std::invalid_argument("clock jump thresholds exceed signed ROS duration range");
    }
    rate_window_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("trigger.rate_window_sec", 5.0),
      "trigger.rate_window_sec");
    rate_deviation_ratio_ =
      node_.declare_parameter<double>("trigger.rate_deviation_ratio", 0.5);
    if (!std::isfinite(rate_deviation_ratio_) || rate_deviation_ratio_ <= 0.0 ||
      rate_deviation_ratio_ >= 1.0)
    {
      throw std::invalid_argument("trigger.rate_deviation_ratio must be between zero and one");
    }
    const auto rate_specs = node_.declare_parameter<std::vector<std::string>>(
      "trigger.topic_rates", std::vector<std::string>{});
    parse_expected_rates(rate_specs);
    history_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("buffer.history_seconds", 30.0),
      "buffer.history_seconds");
    post_trigger_ns_ = checked_seconds_ns(
      node_.declare_parameter<double>("buffer.post_trigger_seconds", 10.0),
      "buffer.post_trigger_seconds");

    status_period_ = std::chrono::milliseconds(
      checked_positive<int64_t>(
        node_.declare_parameter<int64_t>("status.publish_period_ms", 1000),
        "status.publish_period_ms"));
    drain_timeout_ = std::chrono::milliseconds(
      checked_positive<int64_t>(
        node_.declare_parameter<int64_t>("shutdown.drain_timeout_ms", 5000),
        "shutdown.drain_timeout_ms"));
  }

  void parse_expected_rates(const std::vector<std::string> & specifications)
  {
    for (const std::string & specification : specifications) {
      const std::size_t separator = specification.rfind('=');
      if (separator == std::string::npos || separator == 0U) {
        throw std::invalid_argument("trigger.topic_rates entries must use /topic=hz");
      }
      const double rate = std::stod(specification.substr(separator + 1U));
      if (!(rate > 0.0) || !std::isfinite(rate)) {
        throw std::invalid_argument("trigger.topic_rates contains an invalid frequency");
      }
      const std::string topic = node_.get_node_topics_interface()->resolve_topic_name(
        specification.substr(0U, separator));
      expected_rates_[topic] = rate;
    }
  }

  void resolve_topic_configuration()
  {
    auto resolve_all = [this](std::vector<std::string> & topics) {
        for (std::string & topic : topics) {
          topic = node_.get_node_topics_interface()->resolve_topic_name(topic);
        }
        std::sort(topics.begin(), topics.end());
        topics.erase(std::unique(topics.begin(), topics.end()), topics.end());
      };
    resolve_all(configured_topics_);
    resolve_all(high_priority_topics_);
  }

  void initialize_core()
  {
    session_id_ = make_session_id();
    capture_started_monotonic_ns_ = monotonic_now_ns();
    capture_started_system_ns_ = system_now_ns();
    callback_group_ = node_.create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    event_ring_ = std::make_unique<SpscRingBuffer<Event>>(event_capacity_, control_reserve_);
    reclaim_ring_ =
      std::make_unique<SpscRingBuffer<PayloadHandle>>(event_capacity_ + 1U, 0U);
    topic_commands_ =
      std::make_unique<SpscRingBuffer<TopicCommand>>(static_cast<std::size_t>(max_topics_) + 1U);
    arena_ = std::make_unique<PayloadArena>(
      PayloadArenaConfig{
        payload_block_size_, payload_block_count_, max_payload_bytes_});
    registry_ = std::make_unique<TopicRegistry>(max_topics_, topic_string_bytes_);
    metrics_ = std::make_unique<CaptureMetrics>(max_topics_);
    triggers_ = std::make_unique<TriggerEngine>(max_topics_);
    emitted_drop_counts_.resize(
      (static_cast<std::size_t>(max_topics_) + 1U) *
      static_cast<std::size_t>(DropReason::kCount),
      0U);
    best_effort_topic_ids_.resize(static_cast<std::size_t>(max_topics_) + 1U, 0U);
    active_incident_segments_.reserve(retention_max_segments_);

    SegmentWriterOptions writer_options{};
    writer_options.output_directory = output_directory_;
    writer_options.session_id = session_id_;
    writer_options.max_segment_bytes = segment_max_bytes_;
    writer_options.max_segment_events = segment_max_events_;
    writer_options.chunk_size_bytes = chunk_size_bytes_;
    writer_options.max_payload_bytes = max_payload_bytes_;
    writer_options.max_topics = max_topics_;
    writer_options.max_closed_segment_records = retention_max_segments_ + 1U;
    writer_options.session_monotonic_anchor_ns = capture_started_monotonic_ns_;
    writer_options.session_system_anchor_ns = capture_started_system_ns_;
    writer_options.failure_injection.delay_per_write =
      std::chrono::duration_cast<std::chrono::microseconds>(failure_delay_);
    if (failure_after_bytes_ >= 0) {
      writer_options.failure_injection.fail_after_bytes =
        static_cast<uint64_t>(failure_after_bytes_);
      writer_options.failure_injection.failure_errno = ENOSPC;
    }
    writer_ = std::make_unique<SegmentWriter>(std::move(writer_options));

    capture_memory_budget_bytes_ = static_cast<uint64_t>(arena_->memory_bytes());
    for (const uint64_t bytes : {
        static_cast<uint64_t>(event_ring_->memory_bytes()),
        static_cast<uint64_t>(reclaim_ring_->memory_bytes()),
        static_cast<uint64_t>(topic_commands_->memory_bytes()),
        static_cast<uint64_t>(registry_->memory_bytes()),
        static_cast<uint64_t>(metrics_->memory_bytes()),
        static_cast<uint64_t>(triggers_->memory_bytes()),
      })
    {
      capture_memory_budget_bytes_ = checked_add(
        capture_memory_budget_bytes_, bytes, "capture memory estimate");
    }
    const uint64_t writer_scratch = checked_add(
      checked_multiply(chunk_size_bytes_, 2U, "writer chunk scratch"),
      checked_multiply(max_payload_bytes_, 2U, "writer payload scratch"),
      "writer scratch");
    const uint64_t bounded_graph_state = checked_add(
      checked_multiply(max_topics_, 2048U, "topic graph state"),
      checked_multiply(max_graph_nodes_, 640U, "node graph state"),
      "graph state");
    const uint64_t segment_state = checked_multiply(
      retention_max_segments_ + 1U, 1024U, "segment state");
    capture_memory_budget_bytes_ = checked_add(
      capture_memory_budget_bytes_, writer_scratch, "capture memory estimate");
    capture_memory_budget_bytes_ = checked_add(
      capture_memory_budget_bytes_, bounded_graph_state, "capture memory estimate");
    capture_memory_budget_bytes_ = checked_add(
      capture_memory_budget_bytes_, segment_state, "capture memory estimate");
    if (capture_memory_budget_bytes_ > configured_memory_budget_bytes_) {
      throw std::invalid_argument(
              "buffer.memory_budget_bytes is smaller than the capture-owned memory estimate");
    }
  }

  void start()
  {
    prune_sessions_for_new_capture();
    const CaptureStatus open_status = writer_->open();
    if (!open_status.ok()) {
      throw std::runtime_error("native segment writer failed to open: " + open_status.message);
    }
    status_publisher_ = node_.create_publisher<std_msgs::msg::String>(
      "/blackbox/capture_status", rclcpp::QoS(1).reliable().transient_local());

    install_clock_callback();
    discovery_timer_ = node_.create_wall_timer(
      discovery_period_,
      [this]() {callback_boundary("discovery", [this]() {discovery_tick();});},
      callback_group_);
    trigger_timer_ = node_.create_wall_timer(
      100ms, [this]() {callback_boundary("trigger", [this]() {trigger_tick();});},
      callback_group_);
    status_timer_ =
      node_.create_wall_timer(
      status_period_, [this]() {callback_boundary("status", [this]() {status_tick();});},
      callback_group_);

    try {
      writer_running_.store(true, std::memory_order_release);
      writer_thread_ = std::thread([this]() noexcept {writer_thread_entry();});
      refresh_graph(true);
      graph_running_.store(true, std::memory_order_release);
      graph_thread_ = std::thread([this]() {graph_wait_loop();});
      state_.store(kNormal, std::memory_order_release);
      publish_status();
      const std::filesystem::path session_directory = "capture_" + session_id_;
      const std::string current_session =
        "{\"schema_version\":\"blackboxrs.current_capture.v1\",\"session_id\":\"" +
        json_escape(session_id_) + "\",\"path\":\"" +
        json_escape(session_directory.string()) + "\"}\n";
      if (!write_atomic_text(output_directory_ / "current_session.json", current_session)) {
        throw std::runtime_error("failed to publish current native session pointer");
      }
    } catch (...) {
      accepting_.store(false, std::memory_order_release);
      graph_running_.store(false, std::memory_order_release);
      if (graph_thread_.joinable()) {
        graph_thread_.join();
      }
      drain_deadline_ns_.store(monotonic_now_ns(), std::memory_order_release);
      writer_running_.store(false, std::memory_order_release);
      writer_cv_.notify_all();
      if (writer_thread_.joinable()) {
        writer_thread_.join();
      } else {
        (void)writer_->close();
      }
      drain_reclaimed();
      throw;
    }
    RCLCPP_INFO(
      node_.get_logger(),
      "READY session=%s budget_bytes=%" PRIu64 " topics=%zu output=%s",
      session_id_.c_str(),
      capture_memory_budget_bytes_,
      configured_topics_.size(), output_directory_.string().c_str());
  }

  void prune_sessions_for_new_capture()
  {
    std::error_code error;
    std::filesystem::create_directories(output_directory_, error);
    if (error) {
      throw std::runtime_error("failed to create native output directory: " + error.message());
    }
    struct StoredSession
    {
      std::filesystem::path path;
      std::filesystem::file_time_type modified;
      uint64_t logical_bytes{0};
    };
    std::vector<StoredSession> sessions;
    uint64_t total_bytes = 0U;
    for (const auto & entry : std::filesystem::directory_iterator(output_directory_)) {
      if (!entry.is_directory() || entry.path().filename().string().rfind("capture_", 0U) != 0U) {
        continue;
      }
      uint64_t bytes = 0U;
      for (const auto & nested : std::filesystem::recursive_directory_iterator(entry.path())) {
        if (!nested.is_regular_file()) {
          continue;
        }
        const uint64_t size = nested.file_size();
        if (size > UINT64_MAX - bytes || size > UINT64_MAX - total_bytes) {
          throw std::runtime_error("native session storage accounting overflowed");
        }
        bytes += size;
        total_bytes += size;
      }
      sessions.push_back(StoredSession{entry.path(), entry.last_write_time(), bytes});
    }
    std::sort(
      sessions.begin(), sessions.end(), [](const auto & left, const auto & right) {
        return left.modified < right.modified;
      });
    const uint64_t previous_session_budget = total_max_bytes_ - reserved_session_bytes_;
    std::size_t first_retained = 0U;
    while (first_retained < sessions.size() &&
      (sessions.size() - first_retained >= max_sessions_ || total_bytes > previous_session_budget))
    {
      const StoredSession & oldest = sessions[first_retained++];
      std::filesystem::remove_all(oldest.path, error);
      if (error) {
        throw std::runtime_error("failed to prune old native session: " + error.message());
      }
      total_bytes = oldest.logical_bytes > total_bytes ? 0U : total_bytes - oldest.logical_bytes;
    }
    if (first_retained > 0U) {
      const int directory =
        ::open(output_directory_.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
      if (directory < 0) {
        throw std::runtime_error("failed to open native output directory for sync");
      }
      const bool synced = ::fsync(directory) == 0;
      const bool closed = ::close(directory) == 0;
      if (!synced || !closed) {
        throw std::runtime_error("failed to sync native session retention updates");
      }
    }
  }

  void install_clock_callback()
  {
    try {
      rcl_jump_threshold_t threshold{};
      threshold.on_clock_change = true;
      threshold.min_forward.nanoseconds = static_cast<int64_t>(clock_forward_jump_ns_);
      threshold.min_backward.nanoseconds = -static_cast<int64_t>(clock_backward_jump_ns_);
      jump_handler_ = node_.get_clock()->create_jump_callback(
        []() {},
        [this](const rcl_time_jump_t & jump) {
          clock_jump_delta_ns_.store(jump.delta.nanoseconds, std::memory_order_relaxed);
          clock_change_code_.store(
            static_cast<int32_t>(jump.clock_change),
            std::memory_order_relaxed);
          if (jump.clock_change == RCL_ROS_TIME_NO_CHANGE ||
          jump.clock_change == RCL_SYSTEM_TIME_NO_CHANGE)
          {
            clock_anomaly_callback_count_.fetch_add(1U, std::memory_order_relaxed);
          }
          clock_event_count_.fetch_add(1U, std::memory_order_release);
        },
        threshold);
    } catch (const std::exception & error) {
      RCLCPP_WARN(node_.get_logger(), "ROS clock jump callbacks unavailable: %s", error.what());
    }
  }

  template<typename Callback>
  void callback_boundary(const char * name, Callback && callback) noexcept
  {
    try {
      callback();
    } catch (const std::exception & error) {
      runtime_callback_faults_.fetch_add(1U, std::memory_order_relaxed);
      state_.store(kInvariantFault, std::memory_order_release);
      try {
        RCLCPP_ERROR(node_.get_logger(), "%s callback failed: %s", name, error.what());
      } catch (...) {
      }
    } catch (...) {
      runtime_callback_faults_.fetch_add(1U, std::memory_order_relaxed);
      state_.store(kInvariantFault, std::memory_order_release);
    }
  }

  void graph_wait_loop() noexcept
  {
    try {
      auto event = node_.get_graph_event();
      while (graph_running_.load(std::memory_order_acquire)) {
        node_.wait_for_graph_change(event, 250ms);
        if (event->check_and_clear()) {
          graph_dirty_ns_.store(monotonic_now_ns(), std::memory_order_relaxed);
          graph_dirty_.store(true, std::memory_order_release);
        }
      }
    } catch (const std::exception & error) {
      graph_wait_faults_.fetch_add(1U, std::memory_order_relaxed);
      graph_coverage_faults_.fetch_add(1U, std::memory_order_relaxed);
      RCLCPP_ERROR(node_.get_logger(), "graph waiter stopped: %s", error.what());
    }
  }

  void discovery_tick()
  {
    if (stop_requested_.load(std::memory_order_acquire)) {
      return;
    }
    if (graph_dirty_.exchange(false, std::memory_order_acq_rel)) {
      refresh_graph(false);
    }
  }

  void refresh_graph(bool force)
  {
    ProducerGuard guard(producer_active_);
    if (!guard.acquired) {
      record_invariant_drop(0U, 0U);
      return;
    }
    drain_reclaimed_unlocked();

    std::map<std::string, std::vector<std::string>> discovered;
    try {
      discovered = node_.get_topic_names_and_types();
    } catch (const std::exception & error) {
      graph_snapshot_failures_.fetch_add(1U, std::memory_order_relaxed);
      graph_coverage_faults_.fetch_add(1U, std::memory_order_relaxed);
      enqueue_control_unlocked(
        "graph", 0U, EventFlag::kGraphEvent,
        "{\"change\":\"snapshot_failed\",\"error\":\"" +
        json_escape(error.what()) + "\"}");
      return;
    }

    std::vector<std::string> candidates(configured_topics_.begin(), configured_topics_.end());
    std::set<std::string> candidate_names(configured_topics_.begin(), configured_topics_.end());
    if (discover_all_) {
      for (const auto & [topic, types] : discovered) {
        (void)types;
        if (!is_excluded(topic) && candidate_names.insert(topic).second) {
          candidates.push_back(topic);
        }
      }
    }

    std::map<std::string, GraphTopic> current_topics;
    for (const std::string & topic : candidates) {
      if (current_topics.size() >= max_topics_) {
        topic_coverage_truncated_.store(true, std::memory_order_release);
        continue;
      }
      if (is_excluded(topic)) {
        continue;
      }
      std::vector<std::string> types;
      const auto type_iterator = discovered.find(topic);
      if (type_iterator != discovered.end()) {
        types = type_iterator->second;
      } else {
        const auto subscription = subscriptions_.find(topic);
        if (subscription != subscriptions_.end()) {
          types.push_back(subscription->second.type);
        }
      }
      if (types.size() > 1U) {
        ambiguous_topic_types_.fetch_add(1U, std::memory_order_relaxed);
        graph_coverage_faults_.fetch_add(1U, std::memory_order_relaxed);
        enqueue_control_unlocked(
          "graph", 0U, EventFlag::kGraphEvent,
          "{\"change\":\"ambiguous_topic_type\",\"topic\":\"" +
          json_escape(topic) + "\",\"type_count\":" + std::to_string(types.size()) +
          "}");
        continue;
      }

      std::vector<rclcpp::TopicEndpointInfo> publishers;
      std::vector<rclcpp::TopicEndpointInfo> subscribers;
      try {
        publishers = node_.get_publishers_info_by_topic(topic);
        subscribers = node_.get_subscriptions_info_by_topic(topic);
      } catch (const std::exception & error) {
        endpoint_query_failures_.fetch_add(1U, std::memory_order_relaxed);
        graph_coverage_faults_.fetch_add(1U, std::memory_order_relaxed);
        enqueue_control_unlocked(
          "graph", 0U, EventFlag::kGraphEvent,
          "{\"change\":\"endpoint_query_failed\",\"topic\":\"" +
          json_escape(topic) + "\",\"error\":\"" +
          json_escape(error.what()) + "\"}");
        continue;
      }

      const std::size_t publisher_count = count_external(publishers);
      const std::size_t subscriber_count = count_external(subscribers);
      const std::string type = types.empty() ? std::string{} : types.front();
      const uint64_t qos_signature = endpoint_signature(topic, publishers);
      current_topics.emplace(
        topic,
        GraphTopic{type, publisher_count, subscriber_count, qos_signature});

      const auto previous = graph_topics_.find(topic);
      if (force || previous == graph_topics_.end()) {
        emit_graph_topic("topic_observed", topic, type, publisher_count, subscriber_count);
      } else {
        if (previous->second.publishers != publisher_count) {
          emit_graph_topic(
            "publisher_count_changed", topic, type, publisher_count,
            subscriber_count);
        }
        if (previous->second.subscribers != subscriber_count) {
          emit_graph_topic(
            "subscriber_count_changed", topic, type, publisher_count,
            subscriber_count);
        }
        if (previous->second.qos_signature != qos_signature) {
          emit_graph_qos(topic, type, publishers);
        }
        if (previous->second.type != type) {
          emit_graph_topic("type_changed", topic, type, publisher_count, subscriber_count);
        }
        if ((previous->second.publishers + previous->second.subscribers) != 0U &&
          (publisher_count + subscriber_count) == 0U)
        {
          emit_graph_topic("topic_disappeared", topic, type, publisher_count, subscriber_count);
        }
      }

      if (!type.empty() && publisher_count > 0U) {
        const auto existing = subscriptions_.find(topic);
        if (existing == subscriptions_.end() || existing->second.type != type ||
          existing->second.qos_signature != qos_signature)
        {
          if (existing != subscriptions_.end()) {
            (void)triggers_->deconfigure_topic(existing->second.topic_id);
            subscriptions_.erase(existing);
          }
          create_subscription(topic, type, publishers, qos_signature);
        }
      } else if (discover_all_ && !is_configured(topic) && publisher_count == 0U) {
        const auto existing = subscriptions_.find(topic);
        if (existing != subscriptions_.end()) {
          (void)triggers_->deconfigure_topic(existing->second.topic_id);
          subscriptions_.erase(existing);
        }
      }
    }

    for (const auto & [topic, previous] : graph_topics_) {
      if (current_topics.find(topic) == current_topics.end() &&
        (previous.publishers + previous.subscribers) != 0U)
      {
        emit_graph_topic("topic_disappeared", topic, previous.type, 0U, 0U);
      }
    }
    graph_topics_ = std::move(current_topics);
    refresh_nodes_unlocked();
  }

  void refresh_nodes_unlocked()
  {
    std::set<std::string> current;
    try {
      const auto nodes =
        node_.get_node_graph_interface()->get_node_names_and_namespaces();
      for (const auto & [name, node_namespace] : nodes) {
        if (current.size() >= max_graph_nodes_) {
          node_coverage_truncated_.store(true, std::memory_order_release);
          break;
        }
        const std::string full = node_namespace == "/" ? "/" + name :
          node_namespace + "/" + name;
        if (full != node_.get_fully_qualified_name() && full.size() <= 512U) {
          current.insert(full);
        }
      }
    } catch (const std::exception & error) {
      node_snapshot_failures_.fetch_add(1U, std::memory_order_relaxed);
      graph_coverage_faults_.fetch_add(1U, std::memory_order_relaxed);
      enqueue_control_unlocked(
        "graph", 0U, EventFlag::kGraphEvent,
        "{\"change\":\"node_snapshot_failed\",\"error\":\"" +
        json_escape(error.what()) + "\"}");
      return;
    }
    for (const std::string & node_name : current) {
      if (graph_nodes_.find(node_name) == graph_nodes_.end()) {
        enqueue_control_unlocked(
          "graph", 0U, EventFlag::kGraphEvent,
          "{\"change\":\"node_appeared\",\"node\":\"" +
          json_escape(node_name) + "\"}");
      }
    }
    for (const std::string & node_name : graph_nodes_) {
      if (current.find(node_name) == current.end()) {
        enqueue_control_unlocked(
          "graph", 0U, EventFlag::kGraphEvent,
          "{\"change\":\"node_disappeared\",\"node\":\"" +
          json_escape(node_name) + "\"}");
      }
    }
    graph_nodes_ = std::move(current);
  }

  std::size_t count_external(const std::vector<rclcpp::TopicEndpointInfo> & endpoints) const
  {
    return static_cast<std::size_t>(std::count_if(
             endpoints.begin(), endpoints.end(), [this](const auto & endpoint) {
               return !(endpoint.node_name() == node_.get_name() &&
               endpoint.node_namespace() == node_.get_namespace());
             }));
  }

  uint64_t endpoint_signature(
    const std::string & topic,
    const std::vector<rclcpp::TopicEndpointInfo> & endpoints) const
  {
    const rclcpp::QoS requested = adaptive_qos(topic, endpoints);
    const auto & profile = requested.get_rmw_qos_profile();
    uint64_t hash = 1469598103934665603ULL;
    const std::array<uint64_t, 4> values{
      static_cast<uint64_t>(profile.reliability), static_cast<uint64_t>(profile.durability),
      static_cast<uint64_t>(profile.history), static_cast<uint64_t>(profile.depth)};
    for (const uint64_t value : values) {
      hash ^= value;
      hash *= 1099511628211ULL;
    }
    return hash;
  }

  rclcpp::QoS adaptive_qos(
    const std::string & topic,
    const std::vector<rclcpp::TopicEndpointInfo> & publishers) const
  {
    (void)topic;
    rclcpp::QoS qos{rclcpp::KeepLast(subscription_depth_)};
    bool all_reliable = !publishers.empty();
    bool all_transient = !publishers.empty();
    for (const auto & publisher : publishers) {
      const auto & profile = publisher.qos_profile().get_rmw_qos_profile();
      all_reliable = all_reliable &&
        profile.reliability == RMW_QOS_POLICY_RELIABILITY_RELIABLE;
      all_transient = all_transient &&
        profile.durability == RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL;
    }
    if (all_reliable) {
      qos.reliable();
    } else {
      qos.best_effort();
    }
    if (all_transient) {
      qos.transient_local();
    } else {
      qos.durability_volatile();
    }
    return qos;
  }

  std::string qos_metadata(
    const std::vector<rclcpp::TopicEndpointInfo> & publishers,
    const rclcpp::QoS & requested) const
  {
    const auto & request = requested.get_rmw_qos_profile();
    std::ostringstream stream;
    stream << "{\"requested\":{\"reliability\":"
           << static_cast<int>(request.reliability) << ",\"durability\":"
           << static_cast<int>(request.durability) << ",\"depth\":" << request.depth
           << "},\"offered_count\":" << count_external(publishers)
           << ",\"offered_profiles\":[";
    bool first = true;
    for (const auto & publisher : publishers) {
      if (publisher.node_name() == node_.get_name() &&
        publisher.node_namespace() == node_.get_namespace())
      {
        continue;
      }
      if (!first) {
        stream << ',';
      }
      first = false;
      const auto & offered = publisher.qos_profile().get_rmw_qos_profile();
      stream << "{\"reliability\":" << static_cast<int>(offered.reliability)
             << ",\"durability\":" << static_cast<int>(offered.durability)
             << ",\"history\":" << static_cast<int>(offered.history)
             << ",\"depth\":" << offered.depth << '}';
    }
    stream << "]}";
    return stream.str();
  }

  void emit_graph_qos(
    const std::string & topic, const std::string & type,
    const std::vector<rclcpp::TopicEndpointInfo> & publishers)
  {
    const rclcpp::QoS requested = adaptive_qos(topic, publishers);
    enqueue_control_unlocked(
      "graph", 0U, EventFlag::kGraphEvent,
      "{\"change\":\"qos_changed\",\"topic\":\"" + json_escape(topic) +
      "\",\"type\":\"" + json_escape(type) + "\",\"qos\":" +
      qos_metadata(publishers, requested) + "}");
  }

  void create_subscription(
    const std::string & topic, const std::string & type,
    const std::vector<rclcpp::TopicEndpointInfo> & publishers,
    uint64_t qos_signature)
  {
    TopicRegistration registration{};
    {
      std::lock_guard<std::mutex> lock(registry_mutex_);
      registration = registry_->register_topic(topic, type, "cdr");
    }
    if (!registration.ok()) {
      const uint64_t sequence = next_sequence();
      metrics_->record_received(0U, 0U);
      metrics_->record_drop(
        0U, DropReason::kRegistryExhausted, 0U, monotonic_now_ns(),
        sequence);
      enqueue_control_unlocked(
        "graph", 0U, EventFlag::kGraphEvent,
        "{\"change\":\"topic_registry_exhausted\",\"topic\":\"" +
        json_escape(topic) + "\"}");
      return;
    }

    const rclcpp::QoS qos = adaptive_qos(topic, publishers);
    if (registration.created) {
      TopicCommand command{};
      const std::string qos_json = qos_metadata(publishers, qos);
      if (!copy_fixed(command.topic, topic) || !copy_fixed(command.type, type) ||
        !copy_fixed(command.serialization, "cdr") || !copy_fixed(command.qos, qos_json))
      {
        const uint64_t sequence = next_sequence();
        metrics_->record_received(registration.topic_id, 0U);
        metrics_->record_drop(
          registration.topic_id, DropReason::kRegistryExhausted, 0U,
          monotonic_now_ns(), sequence);
        enqueue_control_unlocked(
          "graph", registration.topic_id, EventFlag::kGraphEvent,
          "{\"change\":\"topic_metadata_oversized\"}");
        return;
      }
      command.topic_id = registration.topic_id;
      if (!topic_commands_->try_push(command, AdmissionClass::kControl)) {
        record_invariant_drop(registration.topic_id, 0U);
        return;
      }
      writer_cv_.notify_one();
    }

    rclcpp::SubscriptionOptions options;
    options.callback_group = callback_group_;
    options.use_intra_process_comm = rclcpp::IntraProcessSetting::Disable;
    options.event_callbacks.message_lost_callback =
      [this, topic_id = registration.topic_id](rclcpp::QOSMessageLostInfo & info) {
        const uint64_t change = info.total_count_change > 0 ?
          static_cast<uint64_t>(info.total_count_change) : 0U;
        rmw_messages_lost_.fetch_add(change, std::memory_order_relaxed);
        callback_boundary(
          "message_lost", [this, topic_id, change, &info]() {
            qos_event(
              topic_id, "rmw_message_lost", change,
              static_cast<uint64_t>(info.total_count));
          });
      };
    options.event_callbacks.incompatible_qos_callback =
      [this, topic_id = registration.topic_id](
      rclcpp::QOSRequestedIncompatibleQoSInfo & info) {
        incompatible_qos_events_.fetch_add(1U, std::memory_order_relaxed);
        callback_boundary(
          "incompatible_qos", [this, topic_id, &info]() {
            qos_event(
              topic_id, "requested_incompatible_qos",
              static_cast<uint64_t>(std::max(info.total_count_change, 0)),
              static_cast<uint64_t>(std::max(info.total_count, 0)));
          });
      };

    std::shared_ptr<rclcpp::GenericSubscription> subscription;
    try {
      subscription = node_.create_generic_subscription(
        topic, type, qos,
        [this, topic_id = registration.topic_id, high_priority = is_high_priority(topic)](
          std::shared_ptr<rclcpp::SerializedMessage> message) {
          ingest_message(topic_id, high_priority, std::move(message));
        },
        options);
    } catch (const rclcpp::UnsupportedEventTypeException &) {
      options.event_callbacks = {};
      try {
        subscription = node_.create_generic_subscription(
          topic, type, qos,
          [this, topic_id = registration.topic_id, high_priority = is_high_priority(topic)](
            std::shared_ptr<rclcpp::SerializedMessage> message) {
            ingest_message(topic_id, high_priority, std::move(message));
          },
          options);
      } catch (const std::exception & error) {
        subscription_failures_.fetch_add(1U, std::memory_order_relaxed);
        graph_dirty_.store(true, std::memory_order_release);
        enqueue_control_unlocked(
          "graph", registration.topic_id, EventFlag::kGraphEvent,
          "{\"change\":\"subscription_failed_without_rmw_events\",\"error\":\"" +
          json_escape(error.what()) + "\"}");
        return;
      }
      enqueue_control_unlocked(
        "graph", registration.topic_id, EventFlag::kGraphEvent,
        "{\"change\":\"rmw_event_callbacks_unavailable\"}");
      rmw_event_callbacks_unavailable_.fetch_add(1U, std::memory_order_relaxed);
    } catch (const std::exception & error) {
      subscription_failures_.fetch_add(1U, std::memory_order_relaxed);
      graph_dirty_.store(true, std::memory_order_release);
      enqueue_control_unlocked(
        "graph", registration.topic_id, EventFlag::kGraphEvent,
        "{\"change\":\"subscription_failed\",\"error\":\"" +
        json_escape(error.what()) + "\"}");
      return;
    }

    TopicTriggerConfig trigger_config{};
    trigger_config.heartbeat_enabled = is_configured(topic);
    trigger_config.dead_topic_ns = dead_topic_timeout_ns_;
    trigger_config.rate_window_ns = rate_window_ns_;
    const auto expected = expected_rates_.find(topic);
    if (expected != expected_rates_.end()) {
      trigger_config.rate_enabled = true;
      trigger_config.expected_rate_hz = static_cast<float>(expected->second);
      trigger_config.low_rate_fraction = static_cast<float>(1.0 - rate_deviation_ratio_);
      trigger_config.high_rate_fraction = static_cast<float>(1.0 + rate_deviation_ratio_);
    }
    (void)triggers_->configure_topic(
      registration.topic_id, trigger_config, monotonic_now_ns());

    const bool best_effort =
      qos.get_rmw_qos_profile().reliability == RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT;
    if (best_effort && best_effort_topic_ids_[registration.topic_id] == 0U) {
      best_effort_topic_ids_[registration.topic_id] = 1U;
      best_effort_topics_.fetch_add(1U, std::memory_order_relaxed);
    }
    subscriptions_[topic] = SubscriptionState{type, registration.topic_id, qos_signature,
      best_effort, std::move(subscription)};
    enqueue_control_unlocked(
      "graph", registration.topic_id, EventFlag::kGraphEvent,
      "{\"change\":\"subscription_created\",\"topic\":\"" +
      json_escape(topic) + "\",\"type\":\"" +
      json_escape(type) + "\"}");
  }

  void qos_event(uint32_t topic_id, const char * change, uint64_t current, uint64_t total)
  {
    if (!accepting_.load(std::memory_order_acquire)) {
      return;
    }
    ProducerGuard guard(producer_active_);
    if (!guard.acquired) {
      record_invariant_drop(topic_id, 0U);
      return;
    }
    drain_reclaimed_unlocked();
    enqueue_control_unlocked(
      "graph", topic_id, EventFlag::kGraphEvent,
      "{\"change\":\"" + std::string(change) +
      "\",\"count_change\":" + std::to_string(current) +
      ",\"total_count\":" + std::to_string(total) + "}");
  }

  void ingest_message(
    uint32_t topic_id, bool high_priority,
    std::shared_ptr<rclcpp::SerializedMessage> message) noexcept
  {
    if (!accepting_.load(std::memory_order_acquire)) {
      return;
    }
    const uint64_t now = monotonic_now_ns();
    const uint64_t sequence = next_sequence();
    const uint64_t size = message ? static_cast<uint64_t>(message->size()) : 0U;
    metrics_->record_received(topic_id, size);

    ProducerGuard guard(producer_active_);
    if (!guard.acquired) {
      metrics_->record_drop(topic_id, DropReason::kInvariantFault, size, now, sequence);
      state_.store(kInvariantFault, std::memory_order_release);
      return;
    }
    drain_reclaimed_unlocked();
    if (!accepting_.load(std::memory_order_acquire)) {
      metrics_->record_drop(topic_id, DropReason::kShutdownCutoff, size, now, sequence);
      return;
    }

    const std::size_t depth = event_ring_->size();
    const std::size_t high_mark = static_cast<std::size_t>(
      std::ceil(high_watermark_ratio_ * static_cast<double>(event_ring_->data_capacity())));
    if (depth >= high_mark && !high_priority) {
      metrics_->record_drop(topic_id, DropReason::kLowPriorityShed, size, now, sequence);
      state_.store(kShedding, std::memory_order_release);
      return;
    }

    PayloadHandle payload{};
    const auto * bytes = message == nullptr ?
      nullptr :
      reinterpret_cast<const std::byte *>(
      message->get_rcl_serialized_message().buffer);
    const PayloadAllocationResult allocation =
      arena_->allocate_copy(bytes, static_cast<std::size_t>(size), payload);
    if (allocation != PayloadAllocationResult::kSuccess) {
      const DropReason reason = allocation == PayloadAllocationResult::kOversized ?
        DropReason::kPayloadOversized :
        DropReason::kPayloadExhausted;
      metrics_->record_drop(topic_id, reason, size, now, sequence);
      return;
    }

    Event event{};
    event.header.monotonic_ns = now;
    bool ros_time_valid = false;
    event.header.ros_time_ns = ros_now_ns(ros_time_valid);
    event.header.sequence = sequence;
    event.header.topic_id = topic_id;
    event.header.payload_size = static_cast<uint32_t>(size);
    event.header.flags = to_underlying(EventFlag::kSerializedMessage);
    if (ros_time_valid) {
      event.header.flags |= to_underlying(EventFlag::kRosTimeValid);
    }
    if (high_priority) {
      event.header.flags |= to_underlying(EventFlag::kHighPriority);
    }
    event.payload = payload;
    if (!event_ring_->try_push(event, AdmissionClass::kData)) {
      (void)arena_->release(payload);
      metrics_->record_drop(topic_id, DropReason::kRingFull, size, now, sequence);
      state_.store(kShedding, std::memory_order_release);
      return;
    }
    metrics_->record_admitted(topic_id, size);
    metrics_->observe_queue_depth(event_ring_->size(), event_ring_->capacity());
    triggers_->observe_message(topic_id, now);
    if (event_ring_->size() >= high_mark) {
      state_.store(kHighWatermark, std::memory_order_release);
    }
    writer_cv_.notify_one();
  }

  void trigger_tick()
  {
    if (!accepting_.load(std::memory_order_acquire)) {
      return;
    }
    ProducerGuard guard(producer_active_);
    if (!guard.acquired) {
      record_invariant_drop(0U, 0U);
      return;
    }
    drain_reclaimed_unlocked();
    emit_clock_events_unlocked();

    std::array<TriggerEvent, 32> trigger_events{};
    const uint64_t now = monotonic_now_ns();
    emit_missing_configured_triggers_unlocked(now);
    const std::size_t count =
      triggers_->evaluate(now, trigger_events.data(), trigger_events.size());
    for (std::size_t index = 0; index < count; ++index) {
      const TriggerEvent & trigger = trigger_events[index];
      const std::string topic = topic_name_for_id(trigger.topic_id);
      std::ostringstream payload;
      payload << "{\"code\":" << static_cast<uint16_t>(trigger.code)
              << ",\"severity\":" << static_cast<uint16_t>(trigger.severity)
              << ",\"first_seen_ns\":" << trigger.first_seen_ns
              << ",\"confirmed_ns\":" << trigger.confirmed_ns << ",\"value\":"
              << trigger.value << ",\"threshold\":" << trigger.threshold
              << ",\"topic\":\"" << json_escape(topic) << "\"}";
      enqueue_control_unlocked(
        "trigger", trigger.topic_id, EventFlag::kTriggerEvent,
        payload.str());
    }

    const std::size_t depth = event_ring_->size();
    const std::size_t high_mark = static_cast<std::size_t>(
      std::ceil(high_watermark_ratio_ * static_cast<double>(event_ring_->data_capacity())));
    TriggerEvent queue_trigger{};
    const float queue_ratio = event_ring_->capacity() == 0U ?
      0.0F :
      static_cast<float>(depth) /
      static_cast<float>(event_ring_->capacity());
    if (triggers_->evaluate_threshold(
        TriggerCode::kQueueHighWatermark, Severity::kWarning, 0U, now, queue_ratio,
        static_cast<float>(high_watermark_ratio_),
        static_cast<float>(high_watermark_ratio_ * 0.75), queue_trigger))
    {
      std::ostringstream payload;
      payload << "{\"code\":" << static_cast<uint16_t>(queue_trigger.code)
              << ",\"severity\":" << static_cast<uint16_t>(queue_trigger.severity)
              << ",\"first_seen_ns\":" << queue_trigger.first_seen_ns
              << ",\"confirmed_ns\":" << queue_trigger.confirmed_ns
              << ",\"value\":" << queue_trigger.value << ",\"threshold\":"
              << queue_trigger.threshold << '}';
      enqueue_control_unlocked("trigger", 0U, EventFlag::kTriggerEvent, payload.str());
    }
    if (depth < high_mark / 2U && !writer_faulted_.load(std::memory_order_acquire)) {
      state_.store(kNormal, std::memory_order_release);
    }
  }

  void emit_missing_configured_triggers_unlocked(uint64_t now)
  {
    for (const std::string & topic : configured_topics_) {
      if (subscriptions_.find(topic) != subscriptions_.end()) {
        missing_topic_since_ns_.erase(topic);
        missing_topic_triggered_.erase(topic);
        continue;
      }
      const auto [position, inserted] = missing_topic_since_ns_.emplace(topic, now);
      (void)inserted;
      const uint64_t since = position->second;
      if (now < since || now - since < dead_topic_timeout_ns_ ||
        missing_topic_triggered_.find(topic) != missing_topic_triggered_.end())
      {
        continue;
      }
      missing_topic_triggered_.insert(topic);
      std::ostringstream payload;
      payload << "{\"code\":" << static_cast<uint16_t>(TriggerCode::kDeadTopic)
              << ",\"severity\":" << static_cast<uint16_t>(Severity::kWarning)
              << ",\"first_seen_ns\":" << since + dead_topic_timeout_ns_
              << ",\"confirmed_ns\":" << now << ",\"value\":"
              << static_cast<float>(now - since) / 1.0e9F << ",\"threshold\":"
              << static_cast<float>(dead_topic_timeout_ns_) / 1.0e9F
              << ",\"topic\":\"" << json_escape(topic) << "\"}";
      enqueue_control_unlocked("trigger", 0U, EventFlag::kTriggerEvent, payload.str());
    }
  }

  std::string topic_name_for_id(uint32_t topic_id) const
  {
    std::lock_guard<std::mutex> lock(registry_mutex_);
    const auto topic = registry_->by_id(topic_id);
    return topic ? std::string(topic->topic) : std::string{};
  }

  void emit_clock_events_unlocked()
  {
    const uint64_t total = clock_event_count_.load(std::memory_order_acquire);
    if (total == emitted_clock_event_count_) {
      return;
    }
    const int64_t delta = clock_jump_delta_ns_.load(std::memory_order_relaxed);
    const int32_t change = clock_change_code_.load(std::memory_order_relaxed);
    const uint64_t coalesced_count = total - emitted_clock_event_count_;
    const uint64_t anomaly_total =
      clock_anomaly_callback_count_.load(std::memory_order_acquire);
    const uint64_t anomaly_count = anomaly_total - emitted_clock_anomaly_count_;
    metrics_->record_clock_anomaly(anomaly_count);
    enqueue_control_unlocked(
      "clock", 0U, EventFlag::kClockEvent,
      "{\"change_code\":" + std::to_string(change) + ",\"delta_ns\":" +
      std::to_string(delta) + ",\"observed_count\":" + std::to_string(total) +
      ",\"coalesced_count\":" + std::to_string(coalesced_count) +
      ",\"anomaly_count\":" + std::to_string(anomaly_count) + "}");
    emitted_clock_event_count_ = total;
    emitted_clock_anomaly_count_ = anomaly_total;
  }

  void status_tick()
  {
    if (!accepting_.load(std::memory_order_acquire)) {
      return;
    }
    publish_status();
    ProducerGuard guard(producer_active_);
    if (!guard.acquired) {
      status_publish_failures_.fetch_add(1U, std::memory_order_relaxed);
      return;
    }
    drain_reclaimed_unlocked();
    emit_drop_events_unlocked();
    enqueue_control_unlocked("status", 0U, EventFlag::kStatusEvent, status_json());
  }

  void emit_drop_events_unlocked()
  {
    const std::size_t reason_count = static_cast<std::size_t>(DropReason::kCount);
    const uint32_t highest_topic = static_cast<uint32_t>(registry_->size());
    for (uint32_t topic_id = 0U; topic_id <= highest_topic; ++topic_id) {
      for (std::size_t reason_index = 0U; reason_index < reason_count; ++reason_index) {
        const auto reason = static_cast<DropReason>(reason_index);
        const DropSnapshot snapshot = metrics_->drop_snapshot(topic_id, reason);
        const std::size_t ledger_index =
          static_cast<std::size_t>(topic_id) * reason_count + reason_index;
        if (snapshot.count == 0U || snapshot.count == emitted_drop_counts_[ledger_index]) {
          continue;
        }
        std::ostringstream payload;
        payload << "{\"reason\":" << reason_index << ",\"count\":" << snapshot.count
                << ",\"bytes\":" << snapshot.bytes << ",\"first_monotonic_ns\":"
                << snapshot.first_monotonic_ns << ",\"last_monotonic_ns\":"
                << snapshot.last_monotonic_ns << ",\"first_sequence\":"
                << snapshot.first_sequence << ",\"last_sequence\":"
                << snapshot.last_sequence << '}';
        if (enqueue_control_unlocked(
            "drop", topic_id, EventFlag::kDropEvent,
            payload.str()))
        {
          emitted_drop_counts_[ledger_index] = snapshot.count;
        }
      }
    }
  }

  void publish_status() noexcept
  {
    try {
      std_msgs::msg::String message;
      message.data = status_json();
      status_publisher_->publish(message);
    } catch (const std::exception & error) {
      status_publish_failures_.fetch_add(1U, std::memory_order_relaxed);
      RCLCPP_ERROR(node_.get_logger(), "capture status publish failed: %s", error.what());
    }
  }

  void emit_graph_topic(
    const char * change, const std::string & topic, const std::string & type,
    std::size_t publishers, std::size_t subscribers)
  {
    uint32_t topic_id = 0U;
    {
      std::lock_guard<std::mutex> lock(registry_mutex_);
      const auto registered = registry_->find_topic(topic);
      topic_id = registered ? registered->topic_id : 0U;
    }
    enqueue_control_unlocked(
      "graph", topic_id, EventFlag::kGraphEvent,
      "{\"change\":\"" + std::string(change) +
      "\",\"topic\":\"" + json_escape(topic) +
      "\",\"type\":\"" + json_escape(type) +
      "\",\"publisher_count\":" + std::to_string(publishers) +
      ",\"subscriber_count\":" +
      std::to_string(subscribers) + "}");
  }

  bool enqueue_control_unlocked(
    const char * kind, uint32_t topic_id, EventFlag flag,
    const std::string & payload_object)
  {
    (void)kind;
    const uint64_t now = monotonic_now_ns();
    const uint64_t sequence = next_sequence();
    metrics_->record_received(topic_id, payload_object.size());

    PayloadHandle handle{};
    const auto allocation = arena_->allocate_copy(
      reinterpret_cast<const std::byte *>(payload_object.data()), payload_object.size(), handle);
    if (allocation != PayloadAllocationResult::kSuccess) {
      const DropReason reason = allocation == PayloadAllocationResult::kOversized ?
        DropReason::kPayloadOversized :
        DropReason::kPayloadExhausted;
      metrics_->record_drop(topic_id, reason, payload_object.size(), now, sequence);
      return false;
    }
    Event event{};
    bool ros_time_valid = false;
    const int64_t ros_time_ns = ros_now_ns(ros_time_valid);
    EventFlag event_flags = flag | EventFlag::kHighPriority;
    if (ros_time_valid) {
      event_flags = event_flags | EventFlag::kRosTimeValid;
    }
    event.header = EventHeader{now,
      ros_time_ns,
      sequence,
      topic_id,
      static_cast<uint32_t>(payload_object.size()),
      to_underlying(event_flags),
      0U};
    event.payload = handle;
    if (!event_ring_->try_push(event, AdmissionClass::kControl)) {
      (void)arena_->release(handle);
      metrics_->record_drop(
        topic_id, DropReason::kControlReserveFull, payload_object.size(), now,
        sequence);
      return false;
    }
    metrics_->record_admitted(topic_id, payload_object.size());
    metrics_->observe_queue_depth(event_ring_->size(), event_ring_->capacity());
    writer_cv_.notify_one();
    return true;
  }

  void writer_thread_entry() noexcept
  {
    try {
      writer_loop();
    } catch (...) {
      metrics_->record_storage_error();
      state_.store(kStorageFault, std::memory_order_release);
      writer_faulted_.store(true, std::memory_order_release);
      writer_clean_.store(false, std::memory_order_release);
      drain_incomplete_.store(true, std::memory_order_release);
      discard_remaining_for_shutdown();
      (void)writer_->close();
    }
  }

  void writer_loop()
  {
    auto next_flush = std::chrono::steady_clock::now() + flush_period_;
    uint64_t last_dequeued_monotonic_ns = 0U;
    while (writer_running_.load(std::memory_order_acquire) || !event_ring_->empty() ||
      !topic_commands_->empty())
    {
      if (!writer_running_.load(std::memory_order_acquire) &&
        monotonic_now_ns() >= drain_deadline_ns_.load(std::memory_order_acquire))
      {
        discard_remaining_for_shutdown();
        break;
      }

      drain_topic_commands();
      Event event{};
      if (event_ring_->try_pop(event)) {
        last_dequeued_monotonic_ns = event.header.monotonic_ns;
        if (writer_faulted_.load(std::memory_order_acquire) || writer_->faulted()) {
          metrics_->record_drop(
            event.header.topic_id, DropReason::kStorageFault,
            event.header.payload_size, event.header.monotonic_ns,
            event.header.sequence);
        } else {
          const CaptureStatus status = writer_->write_event(event, *arena_);
          if (status.ok()) {
            metrics_->record_committed(event.header.topic_id, event.header.payload_size);
            if (has_flag(event.header.flags, EventFlag::kTriggerEvent)) {
              if (active_trigger_sequence_ == 0U) {
                active_trigger_sequence_ = event.header.sequence;
                trigger_start_ns_ = event.header.monotonic_ns;
                begin_incident_capture();
                rotate_writer();
                collect_incident_segments(trigger_start_ns_);
              }
              const uint64_t deadline = event.header.monotonic_ns > UINT64_MAX - post_trigger_ns_ ?
                UINT64_MAX :
                event.header.monotonic_ns + post_trigger_ns_;
              post_trigger_deadline_ns_ = std::max(post_trigger_deadline_ns_, deadline);
            }
            (void)sync_closed_segments();
          } else {
            metrics_->record_storage_error();
            metrics_->record_drop(
              event.header.topic_id, DropReason::kStorageFault,
              event.header.payload_size, event.header.monotonic_ns,
              event.header.sequence);
            state_.store(kStorageFault, std::memory_order_release);
            writer_faulted_.store(true, std::memory_order_release);
          }
        }
        if (event.payload.valid() &&
          !reclaim_ring_->try_push(event.payload, AdmissionClass::kData))
        {
          metrics_->record_storage_error();
          state_.store(kInvariantFault, std::memory_order_release);
        }
        set_writer_counters();
      } else {
        std::unique_lock<std::mutex> lock(writer_mutex_);
        writer_cv_.wait_for(lock, 2ms);
      }

      const auto now = std::chrono::steady_clock::now();
      if (now >= next_flush) {
        flush_writer();
        next_flush = now + flush_period_;
      }
      const SegmentInfo info = writer_->current_segment();
      if (info.event_count > 0U &&
        info.last_monotonic_ns - info.first_monotonic_ns >= segment_max_duration_ns_)
      {
        rotate_writer();
      }
      const bool post_trigger_chronology_reached =
        post_trigger_deadline_ns_ != 0U &&
        last_dequeued_monotonic_ns >= post_trigger_deadline_ns_;
      const bool post_trigger_queue_caught_up =
        post_trigger_deadline_ns_ != 0U && event_ring_->empty() &&
        monotonic_now_ns() >= post_trigger_deadline_ns_;
      if (post_trigger_chronology_reached || post_trigger_queue_caught_up) {
        rotate_writer();
        finalize_incident_manifest(true);
        post_trigger_deadline_ns_ = 0U;
      }
    }

    flush_writer();
    set_writer_counters();
    const CaptureStatus close_status = writer_->close();
    if (!close_status.ok()) {
      metrics_->record_storage_error();
      writer_faulted_.store(true, std::memory_order_release);
      writer_clean_.store(false, std::memory_order_release);
    } else {
      (void)sync_closed_segments();
      finalize_incident_manifest(false);
      const MetricsSnapshot snapshot = metrics_->aggregate_snapshot();
      const uint64_t delta = snapshot.committed > snapshot.durable ?
        snapshot.committed - snapshot.durable :
        0U;
      metrics_->record_durable(delta);
      writer_clean_.store(
        !drain_incomplete_.load(std::memory_order_acquire) &&
        !writer_faulted_.load(std::memory_order_acquire) &&
        incident_manifest_errors_.load(std::memory_order_acquire) == 0U,
        std::memory_order_release);
      if (!write_final_session_quality()) {
        set_storage_fault();
        writer_clean_.store(false, std::memory_order_release);
      }
    }
  }

  void drain_topic_commands()
  {
    TopicCommand command{};
    while (topic_commands_->try_pop(command)) {
      TopicDefinition definition{};
      definition.topic_id = command.topic_id;
      definition.topic = command.topic.data();
      definition.type = command.type.data();
      definition.serialization_format = command.serialization.data();
      definition.qos_metadata = command.qos.data();
      const CaptureStatus status = writer_->register_topic(definition);
      if (!status.ok()) {
        metrics_->record_storage_error();
        state_.store(kStorageFault, std::memory_order_release);
        writer_faulted_.store(true, std::memory_order_release);
      }
    }
  }

  void set_writer_counters() noexcept
  {
    const MetricsSnapshot snapshot = metrics_->aggregate_snapshot();
    const uint64_t utilization = event_ring_->capacity() == 0U ?
      0U :
      snapshot.peak_queue_depth * 100U /
      static_cast<uint64_t>(event_ring_->capacity());
    writer_->set_segment_counters(
      SegmentCounters{
        snapshot.received, snapshot.admitted, snapshot.committed, snapshot.dropped,
        snapshot.committed_bytes, snapshot.dropped_bytes, utilization, snapshot.storage_errors,
        snapshot.clock_anomalies});
  }

  void set_storage_fault() noexcept
  {
    metrics_->record_storage_error();
    state_.store(kStorageFault, std::memory_order_release);
    writer_faulted_.store(true, std::memory_order_release);
  }

  bool enforce_retention()
  {
    while (rolling_segments_.size() > retention_max_segments_ ||
      rolling_segment_bytes_ > retention_max_bytes_)
    {
      if (rolling_segments_.empty()) {
        break;
      }
      const SegmentInfo & oldest = rolling_segments_.front();
      std::error_code error;
      (void)std::filesystem::remove(oldest.path, error);
      if (error) {
        set_storage_fault();
        return false;
      }
      std::filesystem::path sidecar = oldest.path;
      sidecar.replace_extension(".json");
      (void)std::filesystem::remove(sidecar, error);
      if (error) {
        set_storage_fault();
        return false;
      }
      if (oldest.event_count > UINT64_MAX - retention_evicted_events_ ||
        oldest.file_bytes > UINT64_MAX - retention_evicted_bytes_)
      {
        set_storage_fault();
        return false;
      }
      rolling_segment_bytes_ = oldest.file_bytes > rolling_segment_bytes_ ?
        0U :
        rolling_segment_bytes_ - oldest.file_bytes;
      ++retention_evicted_segments_;
      retention_evicted_events_ += oldest.event_count;
      retention_evicted_bytes_ += oldest.file_bytes;
      rolling_segments_.pop_front();
      rolling_segment_count_status_.store(rolling_segments_.size(), std::memory_order_release);
      rolling_segment_bytes_status_.store(rolling_segment_bytes_, std::memory_order_release);
      retention_evicted_segments_status_.store(
        retention_evicted_segments_, std::memory_order_release);
      retention_evicted_events_status_.store(
        retention_evicted_events_, std::memory_order_release);
      retention_evicted_bytes_status_.store(
        retention_evicted_bytes_, std::memory_order_release);
    }
    return true;
  }

  bool sync_closed_segments()
  {
    const auto & closed = writer_->closed_segments();
    if (closed.empty() || closed.back().segment_index < next_closed_segment_index_) {
      return true;
    }
    for (const SegmentInfo & info : closed) {
      if (info.segment_index < next_closed_segment_index_) {
        continue;
      }
      if (info.segment_index != next_closed_segment_index_ ||
        info.file_bytes > UINT64_MAX - rolling_segment_bytes_)
      {
        set_storage_fault();
        return false;
      }
      rolling_segments_.push_back(info);
      rolling_segment_bytes_ += info.file_bytes;
      rolling_segment_count_status_.store(rolling_segments_.size(), std::memory_order_release);
      rolling_segment_bytes_status_.store(rolling_segment_bytes_, std::memory_order_release);
      next_closed_segment_index_ = info.segment_index + 1U;
    }
    return enforce_retention();
  }

  bool evict_oldest_incident()
  {
    if (incident_records_.empty()) {
      return true;
    }
    std::error_code error;
    (void)std::filesystem::remove_all(incident_records_.front().path, error);
    if (error) {
      set_storage_fault();
      return false;
    }
    incident_records_.pop_front();
    return true;
  }

  void record_manifest_error() noexcept
  {
    incident_manifest_errors_.fetch_add(1U, std::memory_order_relaxed);
    metrics_->record_storage_error();
  }

  void append_drop_breakdown(std::ostringstream & output) const
  {
    std::lock_guard<std::mutex> lock(registry_mutex_);
    output << '[';
    bool first = true;
    const std::size_t reason_count = static_cast<std::size_t>(DropReason::kCount);
    const uint32_t highest_topic = static_cast<uint32_t>(registry_->size());
    for (uint32_t topic_id = 0U; topic_id <= highest_topic; ++topic_id) {
      for (std::size_t reason_index = 0U; reason_index < reason_count; ++reason_index) {
        const DropSnapshot drop =
          metrics_->drop_snapshot(topic_id, static_cast<DropReason>(reason_index));
        if (drop.count == 0U) {
          continue;
        }
        if (!first) {
          output << ',';
        }
        first = false;
        output << "{\"topic_id\":" << topic_id << ",\"topic\":\"";
        if (const auto topic = registry_->by_id(topic_id)) {
          output << json_escape(topic->topic);
        }
        output << "\",\"reason\":" << reason_index
               << ",\"count\":" << drop.count
               << ",\"bytes\":" << drop.bytes
               << ",\"first_monotonic_ns\":" << drop.first_monotonic_ns
               << ",\"last_monotonic_ns\":" << drop.last_monotonic_ns
               << ",\"first_sequence\":" << drop.first_sequence
               << ",\"last_sequence\":" << drop.last_sequence << '}';
      }
    }
    output << ']';
  }

  bool write_final_session_quality()
  {
    const MetricsSnapshot metrics = metrics_->aggregate_snapshot();
    uint64_t retained_events = 0U;
    for (const SegmentInfo & info : rolling_segments_) {
      if (info.event_count > UINT64_MAX - retained_events) {
        return false;
      }
      retained_events += info.event_count;
    }
    const uint64_t first_monotonic =
      rolling_segments_.empty() ? 0U : rolling_segments_.front().first_monotonic_ns;
    const uint64_t last_monotonic =
      rolling_segments_.empty() ? 0U : rolling_segments_.back().last_monotonic_ns;
    std::ostringstream quality;
    quality << '{'
            << "\"schema_version\":\"blackboxrs.capture_quality.v1\","
            << "\"session_id\":\"" << json_escape(session_id_) << "\","
            << "\"backend\":\"cpp\","
            << "\"role\":\"" << json_escape(runtime_role_) << "\","
            << "\"observed_host\":\"" << json_escape(observed_host_) << "\","
            << "\"clean\":"
            << (writer_clean_.load(std::memory_order_acquire) ? "true" : "false") << ','
            << "\"received\":" << metrics.received << ','
            << "\"admitted\":" << metrics.admitted << ','
            << "\"committed\":" << metrics.committed << ','
            << "\"durable\":" << metrics.durable << ','
            << "\"dropped\":" << metrics.dropped << ','
            << "\"bytes_captured\":" << metrics.committed_bytes << ','
            << "\"bytes_dropped\":" << metrics.dropped_bytes << ','
            << "\"storage_errors\":" << metrics.storage_errors << ','
            << "\"clock_anomalies\":" << metrics.clock_anomalies << ','
            << "\"graph_wait_faults\":"
            << graph_wait_faults_.load(std::memory_order_acquire) << ','
            << "\"graph_coverage_faults\":"
            << graph_coverage_faults_.load(std::memory_order_acquire) << ','
            << "\"graph_snapshot_failures\":"
            << graph_snapshot_failures_.load(std::memory_order_acquire) << ','
            << "\"node_snapshot_failures\":"
            << node_snapshot_failures_.load(std::memory_order_acquire) << ','
            << "\"endpoint_query_failures\":"
            << endpoint_query_failures_.load(std::memory_order_acquire) << ','
            << "\"subscription_failures\":"
            << subscription_failures_.load(std::memory_order_acquire) << ','
            << "\"runtime_callback_faults\":"
            << runtime_callback_faults_.load(std::memory_order_acquire) << ','
            << "\"rmw_messages_lost\":"
            << rmw_messages_lost_.load(std::memory_order_acquire) << ','
            << "\"rmw_event_callbacks_unavailable\":"
            << rmw_event_callbacks_unavailable_.load(std::memory_order_acquire) << ','
            << "\"incompatible_qos_events\":"
            << incompatible_qos_events_.load(std::memory_order_acquire) << ','
            << "\"ambiguous_topic_types\":"
            << ambiguous_topic_types_.load(std::memory_order_acquire) << ','
            << "\"best_effort_topics\":"
            << best_effort_topics_.load(std::memory_order_acquire) << ','
            << "\"topic_coverage_truncated\":"
            << (topic_coverage_truncated_.load(std::memory_order_acquire) ? "true" : "false")
            << ','
            << "\"node_coverage_truncated\":"
            << (node_coverage_truncated_.load(std::memory_order_acquire) ? "true" : "false")
            << ','
            << "\"delivery_scope\":\"callback_received\","
            << "\"graph_scope\":\"" << (discover_all_ ? "all_bounded" : "configured")
            << "\","
            << "\"peak_queue_depth\":" << metrics.peak_queue_depth << ','
            << "\"queue_capacity\":" << event_ring_->capacity() << ','
            << "\"retained_segments\":" << rolling_segments_.size() << ','
            << "\"retained_events\":" << retained_events << ','
            << "\"retained_bytes\":" << rolling_segment_bytes_ << ','
            << "\"retention_evicted_segments\":" << retention_evicted_segments_ << ','
            << "\"retention_evicted_events\":" << retention_evicted_events_ << ','
            << "\"retention_evicted_bytes\":" << retention_evicted_bytes_ << ','
            << "\"retention_max_segments\":" << retention_max_segments_ << ','
            << "\"retention_max_bytes\":" << retention_max_bytes_ << ','
            << "\"monotonic_start_ns\":" << first_monotonic << ','
            << "\"monotonic_end_ns\":" << last_monotonic << ','
            << "\"capture_memory_budget_bytes\":" << capture_memory_budget_bytes_ << ','
            << "\"configured_memory_budget_bytes\":" << configured_memory_budget_bytes_ << ','
            << "\"capture_started_monotonic_ns\":" << capture_started_monotonic_ns_ << ','
            << "\"capture_ended_monotonic_ns\":" << monotonic_now_ns() << ','
            << "\"drop_breakdown\":";
    append_drop_breakdown(quality);
    quality << "}\n";
    const std::filesystem::path path =
      output_directory_ / ("capture_" + session_id_) / "capture_quality.json";
    return write_atomic_text(path, quality.str());
  }

  void begin_incident_capture()
  {
    while (incident_records_.size() >= max_incidents_) {
      if (!evict_oldest_incident()) {
        active_incident_links_complete_ = false;
        active_incident_storage_error_ = true;
        return;
      }
    }
    const std::filesystem::path session_directory =
      output_directory_ / ("capture_" + session_id_);
    std::ostringstream incident_name;
    incident_name << "incident_" << std::setw(20) << std::setfill('0')
                  << active_trigger_sequence_;
    active_incident_directory_ =
      session_directory / "incidents" / incident_name.str();
    std::error_code error;
    std::filesystem::create_directories(active_incident_directory_, error);
    if (error) {
      active_incident_links_complete_ = false;
      active_incident_storage_error_ = true;
      record_manifest_error();
      return;
    }
    collect_incident_segments(trigger_start_ns_);
  }

  void collect_incident_segments(uint64_t requested_end)
  {
    if (active_incident_directory_.empty()) {
      return;
    }
    const uint64_t requested_start = trigger_start_ns_ > history_ns_ ?
      trigger_start_ns_ - history_ns_ :
      0U;
    for (const SegmentInfo & info : rolling_segments_) {
      if (info.last_monotonic_ns < requested_start ||
        info.first_monotonic_ns > requested_end)
      {
        continue;
      }
      const auto already_linked = std::find_if(
        active_incident_segments_.begin(), active_incident_segments_.end(),
        [&info](const SegmentInfo & existing) {
          return existing.segment_index == info.segment_index;
        });
      if (already_linked != active_incident_segments_.end()) {
        continue;
      }
      if (active_incident_segments_.size() >= retention_max_segments_) {
        active_incident_links_complete_ = false;
        continue;
      }

      const std::filesystem::path segment_link =
        active_incident_directory_ / info.path.filename();
      std::filesystem::path source_sidecar = info.path;
      source_sidecar.replace_extension(".json");
      const std::filesystem::path sidecar_link =
        active_incident_directory_ / source_sidecar.filename();
      std::error_code error;
      std::filesystem::create_hard_link(info.path, segment_link, error);
      if (error) {
        active_incident_links_complete_ = false;
        active_incident_storage_error_ = true;
        continue;
      }
      std::filesystem::create_hard_link(source_sidecar, sidecar_link, error);
      if (error) {
        active_incident_links_complete_ = false;
        active_incident_storage_error_ = true;
        std::error_code remove_error;
        (void)std::filesystem::remove(segment_link, remove_error);
        continue;
      }
      active_incident_segments_.push_back(info);
    }
  }

  void reset_active_incident() noexcept
  {
    active_incident_segments_.clear();
    active_incident_directory_.clear();
    active_incident_links_complete_ = true;
    active_incident_storage_error_ = false;
    active_trigger_sequence_ = 0U;
    trigger_start_ns_ = 0U;
  }

  void finalize_incident_manifest(bool post_window_elapsed)
  {
    if (active_trigger_sequence_ == 0U) {
      return;
    }
    (void)sync_closed_segments();
    const uint64_t requested_start = trigger_start_ns_ > history_ns_ ?
      trigger_start_ns_ - history_ns_ :
      0U;
    const uint64_t requested_end = post_trigger_deadline_ns_ != 0U ?
      post_trigger_deadline_ns_ :
      (trigger_start_ns_ > UINT64_MAX - post_trigger_ns_ ?
      UINT64_MAX :
      trigger_start_ns_ + post_trigger_ns_);
    collect_incident_segments(requested_end);

    const uint64_t actual_start = active_incident_segments_.empty() ?
      0U : active_incident_segments_.front().first_monotonic_ns;
    const uint64_t actual_end = active_incident_segments_.empty() ?
      0U : active_incident_segments_.back().last_monotonic_ns;
    const bool history_complete =
      !active_incident_segments_.empty() && actual_start <= requested_start;
    const MetricsSnapshot metrics = metrics_->aggregate_snapshot();
    std::ostringstream manifest;
    manifest << '{'
             << "\"schema_version\":\"blackboxrs.incident_capture.v1\","
             << "\"session_id\":\"" << json_escape(session_id_) << "\","
             << "\"trigger_sequence\":" << active_trigger_sequence_ << ','
             << "\"trigger_monotonic_ns\":" << trigger_start_ns_ << ','
             << "\"monotonic_anchor_ns\":" << capture_started_monotonic_ns_ << ','
             << "\"system_time_anchor_ns\":" << capture_started_system_ns_ << ','
             << "\"requested_start_monotonic_ns\":" << requested_start << ','
             << "\"requested_end_monotonic_ns\":" << requested_end << ','
             << "\"actual_start_monotonic_ns\":" << actual_start << ','
             << "\"actual_end_monotonic_ns\":" << actual_end << ','
             << "\"history_complete\":" << (history_complete ? "true" : "false") << ','
             << "\"post_window_elapsed\":"
             << (post_window_elapsed ? "true" : "false") << ','
             << "\"links_complete\":"
             << (active_incident_links_complete_ ? "true" : "false") << ','
             << "\"received\":" << metrics.received << ','
             << "\"committed\":" << metrics.committed << ','
             << "\"dropped\":" << metrics.dropped << ','
             << "\"window_event_count\":";
    uint64_t window_event_count = 0U;
    for (const SegmentInfo & info : active_incident_segments_) {
      window_event_count += info.event_count;
    }
    manifest << window_event_count << ','
             << "\"segments\":[";
    for (std::size_t index = 0U; index < active_incident_segments_.size(); ++index) {
      if (index != 0U) {
        manifest << ',';
      }
      const SegmentInfo & info = active_incident_segments_[index];
      manifest << "{\"path\":\"" << json_escape(info.path.filename().string())
               << "\",\"segment_index\":" << info.segment_index
               << ",\"first_monotonic_ns\":" << info.first_monotonic_ns
               << ",\"last_monotonic_ns\":" << info.last_monotonic_ns
               << ",\"first_sequence\":" << info.first_sequence
               << ",\"last_sequence\":" << info.last_sequence
               << ",\"event_count\":" << info.event_count
               << ",\"file_bytes\":" << info.file_bytes << ",\"sha256\":\""
               << json_escape(info.sha256) << "\"}";
    }
    manifest << "]}\n";

    std::error_code error;
    if (active_incident_directory_.empty() ||
      !write_atomic_text(active_incident_directory_ / "capture.json", manifest.str()))
    {
      record_manifest_error();
      if (!active_incident_directory_.empty()) {
        std::filesystem::remove_all(active_incident_directory_, error);
      }
      reset_active_incident();
      return;
    }
    if (active_incident_storage_error_) {
      record_manifest_error();
    }
    incident_records_.push_back(
      IncidentRecord{active_incident_directory_, active_trigger_sequence_});
    reset_active_incident();
  }

  void flush_writer() noexcept
  {
    if (writer_faulted_.load(std::memory_order_acquire) || writer_->faulted()) {
      return;
    }
    set_writer_counters();
    const CaptureStatus status = writer_->flush();
    if (!status.ok()) {
      metrics_->record_storage_error();
      state_.store(kStorageFault, std::memory_order_release);
      writer_faulted_.store(true, std::memory_order_release);
      return;
    }
    const MetricsSnapshot snapshot = metrics_->aggregate_snapshot();
    if (snapshot.committed > snapshot.durable) {
      metrics_->record_durable(snapshot.committed - snapshot.durable);
    }
  }

  void rotate_writer()
  {
    if (writer_faulted_.load(std::memory_order_acquire) || writer_->faulted()) {
      return;
    }
    set_writer_counters();
    const CaptureStatus status = writer_->rotate();
    if (!status.ok()) {
      metrics_->record_storage_error();
      state_.store(kStorageFault, std::memory_order_release);
      writer_faulted_.store(true, std::memory_order_release);
    } else {
      (void)sync_closed_segments();
    }
  }

  void discard_remaining_for_shutdown() noexcept
  {
    Event event{};
    while (event_ring_->try_pop(event)) {
      metrics_->record_drop(
        event.header.topic_id, DropReason::kShutdownCutoff,
        event.header.payload_size, event.header.monotonic_ns,
        event.header.sequence);
      if (event.payload.valid()) {
        (void)reclaim_ring_->try_push(event.payload, AdmissionClass::kData);
      }
    }
    drain_incomplete_.store(true, std::memory_order_release);
    writer_clean_.store(false, std::memory_order_release);
  }

  void drain_reclaimed() noexcept
  {
    PayloadHandle handle{};
    while (reclaim_ring_ && reclaim_ring_->try_pop(handle)) {
      (void)arena_->release(handle);
    }
  }

  void drain_reclaimed_unlocked() noexcept {drain_reclaimed();}

  void record_invariant_drop(uint32_t topic_id, uint64_t bytes) noexcept
  {
    const uint64_t sequence = next_sequence();
    metrics_->record_received(topic_id, bytes);
    metrics_->record_drop(
      topic_id, DropReason::kInvariantFault, bytes, monotonic_now_ns(),
      sequence);
    state_.store(kInvariantFault, std::memory_order_release);
  }

  uint64_t next_sequence() noexcept
  {
    return sequence_.fetch_add(1U, std::memory_order_relaxed) + 1U;
  }

  int64_t ros_now_ns(bool & valid) const noexcept
  {
    try {
      if (!node_.get_clock()->started()) {
        valid = false;
        return 0;
      }
      const int64_t now = node_.get_clock()->now().nanoseconds();
      valid = true;
      return now;
    } catch (...) {
      valid = false;
      return 0;
    }
  }

  bool is_configured(const std::string & topic) const
  {
    return std::find(configured_topics_.begin(), configured_topics_.end(), topic) !=
           configured_topics_.end();
  }

  bool is_high_priority(const std::string & topic) const
  {
    return std::find(high_priority_topics_.begin(), high_priority_topics_.end(), topic) !=
           high_priority_topics_.end();
  }

  bool is_excluded(const std::string & topic) const
  {
    for (const std::string & pattern : excluded_topics_) {
      if (pattern == topic) {
        return true;
      }
      if (pattern.size() >= 3U && pattern.compare(pattern.size() - 3U, 3U, "/**") == 0) {
        const std::string prefix = pattern.substr(0U, pattern.size() - 2U);
        if (topic.rfind(prefix, 0U) == 0U) {
          return true;
        }
      }
    }
    return false;
  }

  RecorderNode & node_;
  std::string runtime_role_;
  std::string observed_host_;
  std::string session_id_;
  std::vector<std::string> configured_topics_;
  std::vector<std::string> excluded_topics_;
  std::vector<std::string> high_priority_topics_;
  std::map<std::string, double> expected_rates_;
  bool discover_all_{false};
  std::chrono::milliseconds discovery_period_{100};
  std::chrono::milliseconds status_period_{1000};
  std::chrono::milliseconds flush_period_{250};
  std::chrono::milliseconds failure_delay_{0};
  std::chrono::milliseconds drain_timeout_{5000};
  uint32_t max_topics_{1024};
  std::size_t max_graph_nodes_{2048};
  std::size_t topic_string_bytes_{262144};
  uint32_t max_payload_bytes_{4194304};
  std::size_t subscription_depth_{1000};
  std::size_t event_capacity_{16384};
  std::size_t control_reserve_{256};
  uint32_t payload_block_size_{4096};
  uint32_t payload_block_count_{16384};
  uint64_t configured_memory_budget_bytes_{134217728ULL};
  double high_watermark_ratio_{0.8};
  std::filesystem::path output_directory_;
  uint64_t segment_max_bytes_{268435456};
  uint64_t segment_max_events_{1000000};
  uint64_t segment_max_duration_ns_{5000000000ULL};
  uint64_t chunk_size_bytes_{1048576};
  uint64_t retention_max_bytes_{2147483648ULL};
  std::size_t retention_max_segments_{256};
  std::size_t max_incidents_{20};
  uint64_t total_max_bytes_{53687091200ULL};
  std::size_t max_sessions_{10};
  uint64_t reserved_session_bytes_{0};
  int64_t failure_after_bytes_{-1};
  uint64_t dead_topic_timeout_ns_{2000000000ULL};
  uint64_t clock_forward_jump_ns_{1000000000ULL};
  uint64_t clock_backward_jump_ns_{1000000ULL};
  uint64_t rate_window_ns_{5000000000ULL};
  double rate_deviation_ratio_{0.5};
  uint64_t history_ns_{30000000000ULL};
  uint64_t post_trigger_ns_{10000000000ULL};
  uint64_t capture_memory_budget_bytes_{0};
  uint64_t capture_started_monotonic_ns_{0};
  int64_t capture_started_system_ns_{0};

  rclcpp::CallbackGroup::SharedPtr callback_group_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr status_publisher_;
  rclcpp::TimerBase::SharedPtr discovery_timer_;
  rclcpp::TimerBase::SharedPtr trigger_timer_;
  rclcpp::TimerBase::SharedPtr status_timer_;
  rclcpp::JumpHandler::SharedPtr jump_handler_;

  std::unique_ptr<SpscRingBuffer<Event>> event_ring_;
  std::unique_ptr<SpscRingBuffer<PayloadHandle>> reclaim_ring_;
  std::unique_ptr<SpscRingBuffer<TopicCommand>> topic_commands_;
  std::unique_ptr<PayloadArena> arena_;
  std::unique_ptr<TopicRegistry> registry_;
  std::unique_ptr<CaptureMetrics> metrics_;
  std::unique_ptr<TriggerEngine> triggers_;
  std::unique_ptr<SegmentWriter> writer_;
  std::vector<uint64_t> emitted_drop_counts_;
  std::vector<uint8_t> best_effort_topic_ids_;
  mutable std::mutex registry_mutex_;

  std::unordered_map<std::string, SubscriptionState> subscriptions_;
  std::map<std::string, GraphTopic> graph_topics_;
  std::set<std::string> graph_nodes_;
  std::map<std::string, uint64_t> missing_topic_since_ns_;
  std::set<std::string> missing_topic_triggered_;

  std::atomic_flag producer_active_ = ATOMIC_FLAG_INIT;
  std::atomic<uint64_t> sequence_{0};
  std::atomic<bool> accepting_{true};
  std::atomic<bool> stop_requested_{false};
  std::atomic<bool> stop_started_{false};
  std::atomic<bool> stop_finished_{false};
  std::atomic<bool> stop_clean_{false};
  std::atomic<uint8_t> state_{kStarting};
  std::mutex stop_mutex_;
  std::condition_variable stop_cv_;

  std::atomic<bool> graph_running_{false};
  std::atomic<bool> graph_dirty_{true};
  std::atomic<uint64_t> graph_dirty_ns_{0};
  std::atomic<uint64_t> graph_wait_faults_{0};
  std::atomic<uint64_t> graph_coverage_faults_{0};
  std::atomic<uint64_t> graph_snapshot_failures_{0};
  std::atomic<uint64_t> node_snapshot_failures_{0};
  std::atomic<uint64_t> endpoint_query_failures_{0};
  std::atomic<uint64_t> subscription_failures_{0};
  std::atomic<uint64_t> runtime_callback_faults_{0};
  std::atomic<uint64_t> rmw_messages_lost_{0};
  std::atomic<uint64_t> rmw_event_callbacks_unavailable_{0};
  std::atomic<uint64_t> incompatible_qos_events_{0};
  std::atomic<uint64_t> ambiguous_topic_types_{0};
  std::atomic<uint64_t> best_effort_topics_{0};
  std::atomic<bool> topic_coverage_truncated_{false};
  std::atomic<bool> node_coverage_truncated_{false};
  std::thread graph_thread_;

  std::atomic<bool> writer_running_{false};
  std::atomic<bool> writer_clean_{false};
  std::atomic<bool> writer_faulted_{false};
  std::atomic<bool> drain_incomplete_{false};
  std::atomic<uint64_t> drain_deadline_ns_{UINT64_MAX};
  std::thread writer_thread_;
  std::mutex writer_mutex_;
  std::condition_variable writer_cv_;

  std::atomic<uint64_t> clock_event_count_{0};
  std::atomic<uint64_t> clock_anomaly_callback_count_{0};
  std::atomic<int64_t> clock_jump_delta_ns_{0};
  std::atomic<int32_t> clock_change_code_{0};
  uint64_t emitted_clock_event_count_{0};
  uint64_t emitted_clock_anomaly_count_{0};
  std::atomic<uint64_t> status_publish_failures_{0};
  std::atomic<uint64_t> incident_manifest_errors_{0};
  std::atomic<std::size_t> rolling_segment_count_status_{0};
  std::atomic<uint64_t> rolling_segment_bytes_status_{0};
  std::atomic<uint64_t> retention_evicted_segments_status_{0};
  std::atomic<uint64_t> retention_evicted_events_status_{0};
  std::atomic<uint64_t> retention_evicted_bytes_status_{0};
  std::deque<SegmentInfo> rolling_segments_;
  std::deque<IncidentRecord> incident_records_;
  std::vector<SegmentInfo> active_incident_segments_;
  std::filesystem::path active_incident_directory_;
  bool active_incident_links_complete_{true};
  bool active_incident_storage_error_{false};
  uint64_t rolling_segment_bytes_{0};
  uint64_t retention_evicted_segments_{0};
  uint64_t retention_evicted_events_{0};
  uint64_t retention_evicted_bytes_{0};
  uint64_t next_closed_segment_index_{0};
  uint64_t active_trigger_sequence_{0};
  uint64_t trigger_start_ns_{0};
  uint64_t post_trigger_deadline_ns_{0};
};

RecorderNode::RecorderNode(const rclcpp::NodeOptions & options)
: rclcpp::Node("blackbox_capture", "/blackbox", options),
  impl_(std::make_unique<Impl>(*this)) {}

RecorderNode::~RecorderNode() = default;

void RecorderNode::request_stop() noexcept {impl_->request_stop();}

bool RecorderNode::drain_and_stop() noexcept
{
  return impl_->drain_and_stop_configured();
}

bool RecorderNode::drain_and_stop(std::chrono::milliseconds timeout) noexcept
{
  return impl_->drain_and_stop(timeout);
}

std::string RecorderNode::status_json() const {return impl_->status_json();}

}  // namespace blackbox_capture

RCLCPP_COMPONENTS_REGISTER_NODE(blackbox_capture::RecorderNode)
