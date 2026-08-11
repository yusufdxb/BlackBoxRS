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

#include <gtest/gtest.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/recorder.hpp"
#include "mcap/reader.hpp"
#include "rcutils/logging.h"
#include "rclcpp/executors/single_threaded_executor.hpp"
#include "rclcpp/parameter.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/int32.hpp"
#include "std_msgs/msg/string.hpp"

extern char ** environ;

namespace blackbox_capture
{
namespace
{

using namespace std::chrono_literals;

struct CapturedRateStatus
{
  std::string message;
  std::thread::id thread_id;
};

std::mutex g_rate_status_log_mutex;
std::vector<CapturedRateStatus> g_rate_status_logs;

void capture_rate_status_log(
  const rcutils_log_location_t *, int, const char *, rcutils_time_point_value_t,
  const char * format, va_list * arguments)
{
  std::array<char, 65536> buffer{};
  va_list copy;
  va_copy(copy, *arguments);
  const int length = std::vsnprintf(buffer.data(), buffer.size(), format, copy);
  va_end(copy);
  if (length <= 0 || static_cast<std::size_t>(length) >= buffer.size()) {
    return;
  }
  const std::string message(buffer.data(), static_cast<std::size_t>(length));
  if (message.rfind("RATE_STATUS ", 0U) != 0U) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_rate_status_log_mutex);
  g_rate_status_logs.push_back(CapturedRateStatus{message, std::this_thread::get_id()});
}

class ScopedRateStatusLogCapture
{
public:
  ScopedRateStatusLogCapture()
  : previous_(rcutils_logging_get_output_handler())
  {
    std::lock_guard<std::mutex> lock(g_rate_status_log_mutex);
    g_rate_status_logs.clear();
    rcutils_logging_set_output_handler(capture_rate_status_log);
  }

  ~ScopedRateStatusLogCapture() {rcutils_logging_set_output_handler(previous_);}

  std::vector<CapturedRateStatus> snapshot() const
  {
    std::lock_guard<std::mutex> lock(g_rate_status_log_mutex);
    return g_rate_status_logs;
  }

private:
  rcutils_logging_output_handler_t previous_;
};

class TestDirectory
{
public:
  TestDirectory()
  {
    std::string pattern =
      (std::filesystem::temp_directory_path() / "blackbox-recorder-XXXXXX").string();
    std::vector<char> storage(pattern.begin(), pattern.end());
    storage.push_back('\0');
    char * created = ::mkdtemp(storage.data());
    if (created == nullptr) {
      throw std::runtime_error("mkdtemp failed");
    }
    path_ = created;
  }

  ~TestDirectory()
  {
    std::error_code error;
    std::filesystem::remove_all(path_, error);
  }

  const std::filesystem::path & path() const {return path_;}

private:
  std::filesystem::path path_;
};

std::vector<rclcpp::Parameter> minimal_parameters(const std::filesystem::path & output)
{
  return {
    {"storage.output_directory", output.string()},
    {"capture.max_topics", 8},
    {"capture.max_graph_nodes", 16},
    {"capture.topic_string_bytes", 2048},
    {"capture.max_payload_bytes", 1024},
    {"buffer.event_capacity", 64},
    {"buffer.control_reserve", 8},
    {"buffer.payload_block_size", 256},
    {"buffer.payload_block_count", 64},
    {"storage.segment_max_bytes", 65536},
    {"storage.segment_max_events", 1024},
    {"storage.segment_max_duration_sec", 0.1},
    {"storage.chunk_size_bytes", 4096},
    {"storage.retention_max_bytes", 1048576},
    {"storage.retention_max_segments", 4},
    {"storage.max_incidents", 2},
    {"status.publish_period_ms", 50},
    {"shutdown.drain_timeout_ms", 2000},
  };
}

uint64_t json_uint(const std::string & json, const std::string & field)
{
  const std::string prefix = "\"" + field + "\":";
  const std::size_t position = json.find(prefix);
  if (position == std::string::npos) {
    throw std::runtime_error("missing JSON field: " + field);
  }
  return std::stoull(json.substr(position + prefix.size()));
}

uint64_t rate_message_count(const std::string & status, const std::string & topic)
{
  const std::string prefix =
    "\"topic\":\"" + topic + "\",\"message_count\":";
  const std::size_t position = status.find(prefix);
  if (position == std::string::npos) {
    return 0U;
  }
  return std::stoull(status.substr(position + prefix.size()));
}

std::set<std::string> schema_names_for_topic(
  const std::filesystem::path & root, const std::string & topic)
{
  std::set<std::string> schemas;
  for (const auto & entry : std::filesystem::recursive_directory_iterator(root)) {
    if (!entry.is_regular_file() || entry.path().extension() != ".mcap" ||
      entry.path().filename().string().find(".partial.") != std::string::npos)
    {
      continue;
    }
    mcap::McapReader reader;
    const mcap::Status open_status = reader.open(entry.path().string());
    EXPECT_TRUE(open_status.ok()) << open_status.message;
    if (!open_status.ok()) {
      continue;
    }
    for (const mcap::MessageView & view : reader.readMessages()) {
      if (view.channel != nullptr && view.channel->topic == topic && view.schema != nullptr) {
        schemas.insert(view.schema->name);
      }
    }
    reader.close();
  }
  return schemas;
}

struct ChildProcessResult
{
  int spawn_error{0};
  int wait_error{0};
  int wait_status{0};
  bool timed_out{false};

  bool succeeded() const
  {
    return spawn_error == 0 && wait_error == 0 && !timed_out &&
           WIFEXITED(wait_status) && WEXITSTATUS(wait_status) == 0;
  }
};

ChildProcessResult run_type_churn_publisher(
  const std::string & type, rclcpp::executors::SingleThreadedExecutor & executor)
{
  // A subprocess gives each publisher a fresh DDS participant. This models
  // independently restarted ROS nodes without making the publisher fixture
  // share Fast DDS topic/type state with the recorder under test.
  ChildProcessResult result;
  const std::filesystem::path helper =
    std::filesystem::canonical("/proc/self/exe").parent_path() /
    "test_type_churn_publisher";
  std::string executable = helper.string();
  std::string argument_type = type;
  std::string topic = "/type_flip";
  std::array<char *, 4> arguments{
    executable.data(), argument_type.data(), topic.data(), nullptr};
  pid_t process_id = -1;
  result.spawn_error = ::posix_spawn(
    &process_id, executable.c_str(), nullptr, nullptr, arguments.data(), environ);
  if (result.spawn_error != 0) {
    return result;
  }

  const auto deadline = std::chrono::steady_clock::now() + 7s;
  while (std::chrono::steady_clock::now() < deadline) {
    const pid_t waited = ::waitpid(process_id, &result.wait_status, WNOHANG);
    if (waited == process_id) {
      return result;
    }
    if (waited < 0 && errno != EINTR) {
      result.wait_error = errno;
      return result;
    }
    executor.spin_some();
    std::this_thread::sleep_for(5ms);
  }

  result.timed_out = true;
  (void)::kill(process_id, SIGKILL);
  while (::waitpid(process_id, &result.wait_status, 0) < 0 && errno == EINTR) {
  }
  return result;
}

bool wait_for_no_publishers(
  const std::shared_ptr<RecorderNode> & recorder,
  rclcpp::executors::SingleThreadedExecutor & executor,
  std::chrono::milliseconds timeout)
{
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (std::chrono::steady_clock::now() < deadline) {
    executor.spin_some();
    if (recorder->get_publishers_info_by_topic("/type_flip").empty()) {
      return true;
    }
    std::this_thread::sleep_for(5ms);
  }
  return false;
}

class RecorderNodeTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 0;
    char ** argv = nullptr;
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite()
  {
    rclcpp::shutdown();
  }
};

TEST_F(RecorderNodeTest, GracefulDrainPublishesAuthoritativeStoppedState)
{
  TestDirectory directory;
  rclcpp::NodeOptions options;
  options.parameter_overrides(minimal_parameters(directory.path()));
  auto node = std::make_shared<RecorderNode>(options);

  EXPECT_TRUE(node->drain_and_stop(2s));
  const std::string status = node->status_json();
  EXPECT_NE(status.find("\"state\":\"STOPPED_CLEAN\""), std::string::npos);
  EXPECT_NE(status.find("\"durable\":"), std::string::npos);
  EXPECT_NE(status.find("\"accepting\":false"), std::string::npos);
  EXPECT_NE(status.find("\"writer_alive\":false"), std::string::npos);
  EXPECT_NE(status.find("\"writer_faulted\":false"), std::string::npos);
  EXPECT_EQ(node->get_parameter("status.rate_summary_period_ms").as_int(), 0);

  for (const auto & entry : std::filesystem::recursive_directory_iterator(directory.path())) {
    EXPECT_NE(entry.path().extension(), ".partial");
  }
}

TEST_F(RecorderNodeTest, RuntimeStorageFailureStopsAdmissionAndForcesIncompleteDrain)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("storage.failure_injection_fail_after_bytes", 64);
  parameters.emplace_back("storage.flush_period_ms", 10);
  parameters.emplace_back("capture.max_payload_bytes", 4096);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto node = std::make_shared<RecorderNode>(options);
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node);

  const auto deadline = std::chrono::steady_clock::now() + 2s;
  std::string status;
  do {
    executor.spin_some();
    status = node->status_json();
    if (status.find("\"writer_faulted\":true") != std::string::npos) {
      break;
    }
    std::this_thread::sleep_for(10ms);
  } while (std::chrono::steady_clock::now() < deadline);

  EXPECT_NE(status.find("\"state\":\"STORAGE_FAULT\""), std::string::npos);
  EXPECT_NE(status.find("\"accepting\":false"), std::string::npos);
  EXPECT_NE(status.find("\"writer_faulted\":true"), std::string::npos);
  EXPECT_NE(status.find("\"writer_alive\":true"), std::string::npos);
  executor.remove_node(node);
  EXPECT_FALSE(node->drain_and_stop(2s));
  status = node->status_json();
  EXPECT_NE(status.find("\"state\":\"STOPPED_INCOMPLETE\""), std::string::npos);
  EXPECT_NE(status.find("\"writer_alive\":false"), std::string::npos);
}

TEST_F(RecorderNodeTest, CallbackBeginningAfterStopIsSequencedAndAccounted)
{
  TestDirectory directory;
  auto publisher_node = std::make_shared<rclcpp::Node>("cutoff_test_publisher");
  auto publisher = publisher_node->create_publisher<std_msgs::msg::String>("/cutoff_test", 10);
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("capture.topics", std::vector<std::string>{"/cutoff_test"});
  parameters.emplace_back("capture.discovery_period_ms", 10);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto recorder = std::make_shared<RecorderNode>(options);

  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(publisher_node);
  executor.add_node(recorder);
  const auto discovery_deadline = std::chrono::steady_clock::now() + 2s;
  while (publisher->get_subscription_count() == 0U &&
    std::chrono::steady_clock::now() < discovery_deadline)
  {
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
  }
  ASSERT_GT(publisher->get_subscription_count(), 0U);

  const std::string before = recorder->status_json();
  const uint64_t received_before = json_uint(before, "received");
  const uint64_t dropped_before = json_uint(before, "dropped");
  const uint64_t sequence_before = json_uint(before, "last_sequence");
  recorder->request_stop();
  std_msgs::msg::String message;
  message.data = "callback after admission closed";
  publisher->publish(message);

  std::string after;
  const std::string cutoff_drop =
    "\"topic\":\"/cutoff_test\",\"reason\":" +
    std::to_string(static_cast<uint16_t>(DropReason::kShutdownCutoff)) +
    ",\"count\":1";
  const auto callback_deadline = std::chrono::steady_clock::now() + 2s;
  do {
    executor.spin_some();
    after = recorder->status_json();
    if (after.find(cutoff_drop) != std::string::npos) {
      break;
    }
    std::this_thread::sleep_for(10ms);
  } while (std::chrono::steady_clock::now() < callback_deadline);

  ASSERT_NE(after.find(cutoff_drop), std::string::npos);
  EXPECT_GE(json_uint(after, "received"), received_before + 1U);
  EXPECT_GE(json_uint(after, "dropped"), dropped_before + 1U);
  EXPECT_GE(json_uint(after, "last_sequence"), sequence_before + 1U);
  EXPECT_EQ(json_uint(after, "last_sequence"), json_uint(after, "received"));

  executor.remove_node(recorder);
  executor.remove_node(publisher_node);
  ASSERT_TRUE(recorder->drain_and_stop(2s));
  const std::string final_status = recorder->status_json();
  EXPECT_NE(final_status.find(cutoff_drop), std::string::npos);
  EXPECT_EQ(json_uint(final_status, "last_sequence"), json_uint(final_status, "received"));
  EXPECT_EQ(
    json_uint(final_status, "received"),
    json_uint(final_status, "admitted") + json_uint(final_status, "dropped"));
  EXPECT_EQ(json_uint(final_status, "committed"), json_uint(final_status, "admitted"));
  EXPECT_EQ(json_uint(final_status, "durable"), json_uint(final_status, "committed"));
}

TEST_F(RecorderNodeTest, PeriodicRateStatusReportsExactCallbackCountsOffTheIngestThread)
{
  ScopedRateStatusLogCapture capture;
  TestDirectory directory;
  auto publisher_node = std::make_shared<rclcpp::Node>("rate_status_test_publisher");
  auto publisher =
    publisher_node->create_publisher<std_msgs::msg::String>("/rate_status_test", 10);
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back(
    "capture.topics", std::vector<std::string>{"/rate_status_test"});
  parameters.emplace_back("capture.discovery_period_ms", 10);
  parameters.emplace_back("status.rate_summary_period_ms", 200);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto recorder = std::make_shared<RecorderNode>(options);

  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(publisher_node);
  executor.add_node(recorder);
  const auto discovery_deadline = std::chrono::steady_clock::now() + 2s;
  while (publisher->get_subscription_count() == 0U &&
    std::chrono::steady_clock::now() < discovery_deadline)
  {
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
  }
  ASSERT_GT(publisher->get_subscription_count(), 0U);

  constexpr uint64_t kMessages = 7U;
  std_msgs::msg::String message;
  message.data = "rate sample";
  for (uint64_t index = 0U; index < kMessages; ++index) {
    publisher->publish(message);
  }

  uint64_t reported = 0U;
  std::vector<CapturedRateStatus> logs;
  const auto report_deadline = std::chrono::steady_clock::now() + 2s;
  while (reported < kMessages && std::chrono::steady_clock::now() < report_deadline) {
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
    logs = capture.snapshot();
    reported = 0U;
    for (const CapturedRateStatus & log : logs) {
      reported += rate_message_count(log.message, "/rate_status_test");
    }
  }

  ASSERT_EQ(reported, kMessages);
  ASSERT_FALSE(logs.empty());
  const std::thread::id executor_thread = std::this_thread::get_id();
  bool found_topic = false;
  for (const CapturedRateStatus & log : logs) {
    if (rate_message_count(log.message, "/rate_status_test") == 0U) {
      continue;
    }
    found_topic = true;
    EXPECT_NE(log.thread_id, executor_thread);
    EXPECT_NE(
      log.message.find(
        "RATE_STATUS {\"schema_version\":"
        "\"blackboxrs.capture_rate_status.v1\""),
      std::string::npos);
    EXPECT_NE(log.message.find("\"session_id\":"), std::string::npos);
    EXPECT_NE(log.message.find("\"batch_index\":0"), std::string::npos);
    EXPECT_NE(log.message.find("\"batch_count\":1"), std::string::npos);
    EXPECT_NE(log.message.find("\"topics_truncated\":false"), std::string::npos);
    EXPECT_NE(log.message.find("\"frequency_hz\":"), std::string::npos);
    EXPECT_NE(log.message.find("\"interval_ms\":"), std::string::npos);
    const uint64_t start_ns = json_uint(log.message, "window_start_monotonic_ns");
    const uint64_t end_ns = json_uint(log.message, "window_end_monotonic_ns");
    EXPECT_LT(start_ns, end_ns);
  }
  EXPECT_TRUE(found_topic);
  EXPECT_EQ(json_uint(recorder->status_json(), "rate_status_failures"), 0U);
  EXPECT_NE(recorder->status_json().find("\"rate_status_alive\":true"), std::string::npos);

  executor.remove_node(recorder);
  executor.remove_node(publisher_node);
  EXPECT_TRUE(recorder->drain_and_stop(2s));
  EXPECT_NE(recorder->status_json().find("\"rate_status_alive\":false"), std::string::npos);
}

TEST_F(RecorderNodeTest, OversizedArrivalsStillSatisfyHeartbeatAtCallbackReceipt)
{
  TestDirectory directory;
  auto publisher_node = std::make_shared<rclcpp::Node>("oversize_test_publisher");
  auto publisher = publisher_node->create_publisher<std_msgs::msg::String>("/oversize_test", 10);
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("capture.topics", std::vector<std::string>{"/oversize_test"});
  parameters.emplace_back("capture.discovery_period_ms", 10);
  parameters.emplace_back("trigger.dead_topic_timeout_sec", 0.3);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto recorder = std::make_shared<RecorderNode>(options);

  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(publisher_node);
  executor.add_node(recorder);
  const auto discovery_deadline = std::chrono::steady_clock::now() + 2s;
  while (publisher->get_subscription_count() == 0U &&
    std::chrono::steady_clock::now() < discovery_deadline)
  {
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
  }
  ASSERT_GT(publisher->get_subscription_count(), 0U);

  std_msgs::msg::String message;
  message.data.assign(2048U, 'x');
  const auto publish_deadline = std::chrono::steady_clock::now() + 800ms;
  while (std::chrono::steady_clock::now() < publish_deadline) {
    publisher->publish(message);
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
  }
  const std::string status = recorder->status_json();
  EXPECT_NE(status.find("\"reason\":3"), std::string::npos);

  executor.remove_node(recorder);
  executor.remove_node(publisher_node);
  EXPECT_TRUE(recorder->drain_and_stop(2s));
  for (const auto & entry : std::filesystem::directory_iterator(directory.path())) {
    EXPECT_NE(entry.path().filename().string().rfind("incident_", 0U), 0U);
  }
}

TEST_F(RecorderNodeTest, PublisherTypeChangeGetsANewDurableTopicDefinition)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("capture.topics", std::vector<std::string>{"/type_flip"});
  parameters.emplace_back("capture.discovery_period_ms", 10);
  parameters.emplace_back("storage.segment_max_duration_sec", 10.0);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto recorder = std::make_shared<RecorderNode>(options);
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(recorder);
  const ChildProcessResult string_result = run_type_churn_publisher("string", executor);
  ASSERT_TRUE(string_result.succeeded())
    << "string publisher process failed: spawn_error=" << string_result.spawn_error
    << " wait_error=" << string_result.wait_error
    << " wait_status=" << string_result.wait_status
    << " timed_out=" << string_result.timed_out;
  ASSERT_TRUE(wait_for_no_publishers(recorder, executor, 2s));

  const auto graph_settle_deadline = std::chrono::steady_clock::now() + 100ms;
  while (std::chrono::steady_clock::now() < graph_settle_deadline) {
    executor.spin_some();
    std::this_thread::sleep_for(5ms);
  }

  const ChildProcessResult integer_result = run_type_churn_publisher("int32", executor);
  ASSERT_TRUE(integer_result.succeeded())
    << "integer publisher process failed: spawn_error=" << integer_result.spawn_error
    << " wait_error=" << integer_result.wait_error
    << " wait_status=" << integer_result.wait_status
    << " timed_out=" << integer_result.timed_out;
  const std::string status = recorder->status_json();
  EXPECT_EQ(json_uint(status, "graph_coverage_faults"), 0U);
  EXPECT_EQ(json_uint(status, "subscription_failures"), 0U);

  executor.remove_node(recorder);
  EXPECT_TRUE(recorder->drain_and_stop(2s));
  const std::set<std::string> schemas = schema_names_for_topic(directory.path(), "/type_flip");
  EXPECT_NE(schemas.find("std_msgs/msg/String"), schemas.end());
  EXPECT_NE(schemas.find("std_msgs/msg/Int32"), schemas.end());
}

TEST_F(RecorderNodeTest, PublisherFanoutCannotOverflowBoundedQosMetadata)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("capture.topics", std::vector<std::string>{"/publisher_fanout"});
  parameters.emplace_back("capture.discovery_period_ms", 10);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto recorder = std::make_shared<RecorderNode>(options);
  auto publisher_node = std::make_shared<rclcpp::Node>("fanout_publishers");
  std::vector<rclcpp::Publisher<std_msgs::msg::String>::SharedPtr> publishers;
  for (std::size_t index = 0U; index < 20U; ++index) {
    publishers.push_back(
      publisher_node->create_publisher<std_msgs::msg::String>("/publisher_fanout", 10));
  }

  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(recorder);
  executor.add_node(publisher_node);
  const auto discovery_deadline = std::chrono::steady_clock::now() + 2s;
  while (publishers.front()->get_subscription_count() == 0U &&
    std::chrono::steady_clock::now() < discovery_deadline)
  {
    executor.spin_some();
    std::this_thread::sleep_for(10ms);
  }
  ASSERT_GT(publishers.front()->get_subscription_count(), 0U);
  EXPECT_EQ(json_uint(recorder->status_json(), "graph_coverage_faults"), 0U);

  std_msgs::msg::String message;
  message.data = "fanout";
  publishers.front()->publish(message);
  executor.spin_some();
  executor.remove_node(publisher_node);
  executor.remove_node(recorder);
  EXPECT_TRUE(recorder->drain_and_stop(2s));
}

TEST_F(RecorderNodeTest, RejectsNegativeDurationBeforeStartingThreads)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("storage.segment_max_duration_sec", -1.0);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  EXPECT_THROW((void)std::make_shared<RecorderNode>(options), std::invalid_argument);
}

TEST_F(RecorderNodeTest, RejectsNegativeRateSummaryPeriodBeforeStartingThreads)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("status.rate_summary_period_ms", -1);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  EXPECT_THROW((void)std::make_shared<RecorderNode>(options), std::invalid_argument);
}

TEST_F(RecorderNodeTest, RejectsCaptureOwnedMemoryAboveConfiguredBudget)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("buffer.memory_budget_bytes", 1);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  EXPECT_THROW((void)std::make_shared<RecorderNode>(options), std::invalid_argument);
}

TEST_F(RecorderNodeTest, RejectsConfiguredTopicsAboveRegistryCapacity)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back(
    "capture.topics",
    std::vector<std::string>{
        "/topic_1", "/topic_2", "/topic_3", "/topic_4", "/topic_5",
        "/topic_6", "/topic_7", "/topic_8", "/topic_9"});
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  EXPECT_THROW((void)std::make_shared<RecorderNode>(options), std::invalid_argument);
}

TEST_F(RecorderNodeTest, PrunesOldSessionsBeforePublishingTheCurrentSession)
{
  TestDirectory directory;
  const auto old_session = directory.path() / "capture_legacy";
  std::filesystem::create_directories(old_session);
  std::ofstream(old_session / "evidence.bin", std::ios::binary) << "old evidence";

  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back("storage.max_sessions", 1);
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto node = std::make_shared<RecorderNode>(options);

  EXPECT_FALSE(std::filesystem::exists(old_session));
  EXPECT_TRUE(std::filesystem::exists(directory.path() / "current_session.json"));
  EXPECT_TRUE(node->drain_and_stop(2s));
}

TEST_F(RecorderNodeTest, AcceptsGraduatedPriorityTiersAndTheLegacyAlias)
{
  TestDirectory directory;
  auto parameters = minimal_parameters(directory.path());
  parameters.emplace_back(
    "capture.priority_tier_0", std::vector<std::string>{"/cmd_vel", "/joint_states", "/imu/data"});
  parameters.emplace_back("capture.priority_tier_1", std::vector<std::string>{"/tf"});
  parameters.emplace_back("capture.priority_tier_2", std::vector<std::string>{"/tf_static"});
  parameters.emplace_back("capture.high_priority_topics", std::vector<std::string>{"/diagnostics"});
  rclcpp::NodeOptions options;
  options.parameter_overrides(parameters);
  auto node = std::make_shared<RecorderNode>(options);

  EXPECT_EQ(
    node->get_parameter("capture.priority_tier_0").as_string_array().size(), 3U);
  EXPECT_EQ(node->get_parameter("capture.priority_tier_1").as_string_array().front(), "/tf");
  EXPECT_TRUE(node->drain_and_stop(2s));
}

TEST_F(RecorderNodeTest, ConcurrentStopCallersObserveTheSameFinalResult)
{
  TestDirectory directory;
  rclcpp::NodeOptions options;
  options.parameter_overrides(minimal_parameters(directory.path()));
  auto node = std::make_shared<RecorderNode>(options);

  std::atomic<bool> start{false};
  bool first_result = false;
  bool second_result = false;
  std::thread first([&]() {
      while (!start.load(std::memory_order_acquire)) {
        std::this_thread::yield();
      }
      first_result = node->drain_and_stop(2s);
    });
  std::thread second([&]() {
      while (!start.load(std::memory_order_acquire)) {
        std::this_thread::yield();
      }
      second_result = node->drain_and_stop(2s);
    });
  start.store(true, std::memory_order_release);
  first.join();
  second.join();

  EXPECT_TRUE(first_result);
  EXPECT_TRUE(second_result);
  EXPECT_NE(node->status_json().find("\"state\":\"STOPPED_CLEAN\""), std::string::npos);
}

}  // namespace
}  // namespace blackbox_capture
