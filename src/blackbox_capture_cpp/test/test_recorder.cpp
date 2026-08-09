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
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "blackbox_capture/recorder.hpp"
#include "rclcpp/parameter.hpp"
#include "rclcpp/rclcpp.hpp"

namespace blackbox_capture
{
namespace
{

using namespace std::chrono_literals;

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

  for (const auto & entry : std::filesystem::recursive_directory_iterator(directory.path())) {
    EXPECT_NE(entry.path().extension(), ".partial");
  }
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
