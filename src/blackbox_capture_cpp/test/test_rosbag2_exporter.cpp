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
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstddef>
#include <cstdint>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include <mcap/reader.hpp>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/payload_arena.hpp"
#include "blackbox_capture/rosbag2_exporter.hpp"
#include "blackbox_capture/segment_writer.hpp"
#include "rclcpp/executors/single_threaded_executor.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"

extern char ** environ;

namespace blackbox_capture
{
namespace
{

class TestDirectory
{
public:
  TestDirectory()
  {
    std::string pattern =
      (std::filesystem::temp_directory_path() / "blackbox-export-XXXXXX").string();
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

class Rosbag2ExporterRosTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    int argc = 0;
    char ** argv = nullptr;
    rclcpp::init(argc, argv);
  }

  static void TearDownTestSuite() {rclcpp::shutdown();}
};

SegmentWriterOptions writer_options(
  const std::filesystem::path & root, const std::string & session,
  uint64_t max_segment_events = 100U)
{
  SegmentWriterOptions options{};
  options.output_directory = root;
  options.session_id = session;
  options.max_segment_bytes = 1024U * 1024U;
  options.max_segment_events = max_segment_events;
  options.chunk_size_bytes = 64U * 1024U;
  options.max_payload_bytes = 8192U;
  options.max_topics = 8U;
  options.max_topic_metadata_bytes = 4096U;
  return options;
}

std::vector<std::byte> serialized_string(std::string_view value)
{
  const uint32_t length = static_cast<uint32_t>(value.size() + 1U);
  std::vector<std::byte> bytes(8U + length);
  bytes[0] = std::byte{0x00};
  bytes[1] = std::byte{0x01};
  bytes[2] = std::byte{0x00};
  bytes[3] = std::byte{0x00};
  bytes[4] = static_cast<std::byte>(length & 0xffU);
  bytes[5] = static_cast<std::byte>((length >> 8U) & 0xffU);
  bytes[6] = static_cast<std::byte>((length >> 16U) & 0xffU);
  bytes[7] = static_cast<std::byte>((length >> 24U) & 0xffU);
  std::memcpy(bytes.data() + 8U, value.data(), value.size());
  bytes.back() = std::byte{0x00};
  return bytes;
}

Event make_event(
  uint64_t sequence, uint32_t topic_id, uint64_t monotonic_ns,
  int64_t ros_time_ns, uint32_t payload_size, uint32_t flags)
{
  Event event{};
  event.header.sequence = sequence;
  event.header.topic_id = topic_id;
  event.header.monotonic_ns = monotonic_ns;
  event.header.ros_time_ns = ros_time_ns;
  event.header.payload_size = payload_size;
  event.header.flags = flags;
  return event;
}

void write_payload(
  SegmentWriter & writer, PayloadArena & arena, Event event,
  const std::vector<std::byte> & payload)
{
  ASSERT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), event.payload),
    PayloadAllocationResult::kSuccess);
  ASSERT_TRUE(writer.write_event(event, arena).ok()) << writer.last_status().message;
  ASSERT_EQ(arena.release(event.payload), PayloadReleaseResult::kSuccess);
}

std::filesystem::path create_native_segment(
  const std::filesystem::path & root, const std::string & session,
  bool include_control = true)
{
  SegmentWriter writer(writer_options(root, session));
  EXPECT_TRUE(writer.open().ok()) << writer.last_status().message;
  EXPECT_TRUE(
    writer.register_topic(
      TopicDefinition{1U, "/export/string", "std_msgs/msg/String", "cdr", ""}).ok());
  PayloadArena arena({256U, 16U, 4096U});
  const std::vector<std::byte> cdr = serialized_string("preserved");
  write_payload(
    writer, arena,
    make_event(
      41U, 1U, 500U, 1'700'000'000'000'000'500LL,
      static_cast<uint32_t>(cdr.size()),
      to_underlying(EventFlag::kSerializedMessage) |
      to_underlying(EventFlag::kRosTimeValid)),
    cdr);
  if (include_control) {
    const std::string control = "{\"state\":\"NORMAL\"}";
    std::vector<std::byte> control_bytes(control.size());
    std::memcpy(control_bytes.data(), control.data(), control.size());
    write_payload(
      writer, arena,
      make_event(
        42U, 0U, 600U, 1'700'000'000'000'000'600LL,
        static_cast<uint32_t>(control_bytes.size()),
        to_underlying(EventFlag::kStatusEvent) |
        to_underlying(EventFlag::kRosTimeValid)),
      control_bytes);
  }
  EXPECT_TRUE(writer.close().ok()) << writer.last_status().message;
  EXPECT_EQ(writer.closed_segments().size(), 1U);
  return writer.closed_segments().front().path;
}

bool has_export_temporary(const std::filesystem::path & directory)
{
  for (const auto & entry : std::filesystem::directory_iterator(directory)) {
    if (entry.path().filename().string().find(".partial.") != std::string::npos) {
      return true;
    }
  }
  return false;
}

int run_rosbag2_info(const std::filesystem::path & output)
{
  std::string executable = "ros2";
  std::string bag = output.string();
  std::string verb = "bag";
  std::string command = "info";
  std::string storage_option = "--storage";
  std::string storage = "mcap";
  std::vector<char *> arguments{
    executable.data(), verb.data(), command.data(), storage_option.data(), storage.data(),
    bag.data(), nullptr};
  posix_spawn_file_actions_t actions;
  if (::posix_spawn_file_actions_init(&actions) != 0) {
    return -1;
  }
  const int null_fd = ::open("/dev/null", O_WRONLY | O_CLOEXEC);
  if (null_fd >= 0) {
    (void)::posix_spawn_file_actions_adddup2(&actions, null_fd, STDOUT_FILENO);
    (void)::posix_spawn_file_actions_adddup2(&actions, null_fd, STDERR_FILENO);
  }
  pid_t process_id = -1;
  const int spawn_error = ::posix_spawnp(
    &process_id, executable.c_str(), &actions, nullptr, arguments.data(), environ);
  (void)::posix_spawn_file_actions_destroy(&actions);
  if (null_fd >= 0) {
    (void)::close(null_fd);
  }
  if (spawn_error != 0) {
    return -spawn_error;
  }
  int status = 0;
  while (::waitpid(process_id, &status, 0) < 0 && errno == EINTR) {
  }
  return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

int run_rosbag2_play(
  const std::filesystem::path & output,
  rclcpp::executors::SingleThreadedExecutor & executor,
  bool & message_received)
{
  std::string executable = "ros2";
  std::string verb = "bag";
  std::string command = "play";
  std::string storage_option = "--storage";
  std::string storage = "mcap";
  std::string keyboard = "--disable-keyboard-controls";
  std::string delay_option = "--delay";
  std::string delay = "0.5";
  std::string bag = output.string();
  std::vector<char *> arguments{
    executable.data(), verb.data(), command.data(), storage_option.data(), storage.data(),
    keyboard.data(), delay_option.data(), delay.data(), bag.data(), nullptr};
  posix_spawn_file_actions_t actions;
  if (::posix_spawn_file_actions_init(&actions) != 0) {
    return -1;
  }
  const int null_fd = ::open("/dev/null", O_WRONLY | O_CLOEXEC);
  if (null_fd >= 0) {
    (void)::posix_spawn_file_actions_adddup2(&actions, null_fd, STDOUT_FILENO);
    (void)::posix_spawn_file_actions_adddup2(&actions, null_fd, STDERR_FILENO);
  }
  pid_t process_id = -1;
  const int spawn_error = ::posix_spawnp(
    &process_id, executable.c_str(), &actions, nullptr, arguments.data(), environ);
  (void)::posix_spawn_file_actions_destroy(&actions);
  if (null_fd >= 0) {
    (void)::close(null_fd);
  }
  if (spawn_error != 0) {
    return -spawn_error;
  }

  int status = 0;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(8);
  bool exited = false;
  while (std::chrono::steady_clock::now() < deadline) {
    executor.spin_some();
    const pid_t waited = ::waitpid(process_id, &status, WNOHANG);
    if (waited == process_id) {
      exited = true;
      break;
    }
    if (waited < 0 && errno != EINTR) {
      return -1;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  if (!exited) {
    (void)::kill(process_id, SIGKILL);
    while (::waitpid(process_id, &status, 0) < 0 && errno == EINTR) {
    }
    return -1;
  }
  const auto delivery_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  while (!message_received && std::chrono::steady_clock::now() < delivery_deadline) {
    executor.spin_some();
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

TEST_F(Rosbag2ExporterRosTest, WritesAndPlaysStandardDataOnlyMcapWithExactCdrPayload)
{
  TestDirectory directory;
  const std::filesystem::path input =
    create_native_segment(directory.path(), "single");
  const std::filesystem::path output = directory.path() / "export.mcap";

  Rosbag2ExportSummary summary{};
  const CaptureStatus status = export_rosbag2_data(input, output, summary);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_EQ(summary.input_segments, 1U);
  EXPECT_EQ(summary.topics, 1U);
  EXPECT_EQ(summary.messages, 1U);
  EXPECT_EQ(summary.control_messages_excluded, 1U);
  EXPECT_GT(summary.output_bytes, 0U);
  EXPECT_EQ(summary.output_path, output);
  EXPECT_TRUE(summary.published);
  EXPECT_FALSE(has_export_temporary(directory.path()));

  mcap::McapReader reader;
  ASSERT_TRUE(reader.open(output.string()).ok());
  ASSERT_TRUE(reader.header().has_value());
  EXPECT_EQ(reader.header()->profile, "ros2");
  EXPECT_TRUE(reader.readSummary(mcap::ReadSummaryMethod::NoFallbackScan).ok());
  ASSERT_TRUE(reader.statistics().has_value());
  EXPECT_EQ(reader.statistics()->messageCount, 1U);

  const std::vector<std::byte> expected = serialized_string("preserved");
  std::size_t messages = 0U;
  for (const mcap::MessageView & view : reader.readMessages()) {
    ++messages;
    ASSERT_NE(view.channel, nullptr);
    ASSERT_NE(view.schema, nullptr);
    EXPECT_EQ(view.channel->topic, "/export/string");
    EXPECT_EQ(view.channel->messageEncoding, "cdr");
    EXPECT_EQ(view.channel->metadata.at("offered_qos_profiles"), "");
    EXPECT_EQ(view.schema->name, "std_msgs/msg/String");
    EXPECT_EQ(view.schema->encoding, "ros2msg");
    EXPECT_EQ(view.message.sequence, 41U);
    EXPECT_EQ(view.message.logTime, 1'700'000'000'000'000'500ULL);
    EXPECT_EQ(view.message.publishTime, view.message.logTime);
    ASSERT_EQ(view.message.dataSize, expected.size());
    EXPECT_EQ(
      std::memcmp(view.message.data, expected.data(), expected.size()), 0);
  }
  EXPECT_EQ(messages, 1U);
  reader.close();
  EXPECT_EQ(run_rosbag2_info(output), 0);

  auto subscriber_node = std::make_shared<rclcpp::Node>("rosbag2_export_typed_subscriber");
  bool received = false;
  std::string received_data;
  auto subscription = subscriber_node->create_subscription<std_msgs::msg::String>(
    "/export/string", 10,
    [&](std_msgs::msg::String::ConstSharedPtr message) {
      received = true;
      received_data = message->data;
    });
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(subscriber_node);
  EXPECT_EQ(run_rosbag2_play(output, executor, received), 0);
  EXPECT_TRUE(received);
  EXPECT_EQ(received_data, "preserved");
  executor.remove_node(subscriber_node);
  (void)subscription;
}

TEST(Rosbag2ExporterTest, ExportsEveryFinalizedSegmentFromCleanSession)
{
  TestDirectory directory;
  SegmentWriter writer(writer_options(directory.path(), "session", 1U));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(
    writer.register_topic(
      TopicDefinition{1U, "/export/string", "std_msgs/msg/String", "cdr", ""}).ok());
  PayloadArena arena({256U, 16U, 4096U});
  const std::vector<std::byte> first = serialized_string("first");
  const std::vector<std::byte> second = serialized_string("second");
  write_payload(
    writer, arena,
    make_event(
      1U, 1U, 100U, 1'700'000'000'000'000'100LL,
      static_cast<uint32_t>(first.size()),
      to_underlying(EventFlag::kSerializedMessage) |
      to_underlying(EventFlag::kRosTimeValid)),
    first);
  write_payload(
    writer, arena,
    make_event(
      2U, 1U, 200U, 1'700'000'000'000'000'200LL,
      static_cast<uint32_t>(second.size()),
      to_underlying(EventFlag::kSerializedMessage) |
      to_underlying(EventFlag::kRosTimeValid)),
    second);
  ASSERT_TRUE(writer.close().ok());
  ASSERT_EQ(writer.closed_segments().size(), 2U);
  const std::filesystem::path session = directory.path() / "capture_session";
  {
    std::ofstream quality(session / "capture_quality.json");
    quality <<
      "{\"schema_version\":\"blackboxrs.capture_quality.v1\",\"clean\":true}\n";
  }

  Rosbag2ExportSummary summary{};
  const std::filesystem::path output = directory.path() / "session.mcap";
  const CaptureStatus status = export_rosbag2_data(session, output, summary);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_EQ(summary.input_segments, 2U);
  EXPECT_EQ(summary.topics, 1U);
  EXPECT_EQ(summary.messages, 2U);
  EXPECT_TRUE(summary.published);
}

TEST(Rosbag2ExporterTest, RejectsTypeChurnAndRemovesTemporaryOutput)
{
  TestDirectory directory;
  SegmentWriter writer(writer_options(directory.path(), "churn"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(
    writer.register_topic(
      TopicDefinition{1U, "/export/churn", "std_msgs/msg/String", "cdr", ""}).ok());
  ASSERT_TRUE(
    writer.register_topic(
      TopicDefinition{2U, "/export/churn", "std_msgs/msg/Int32", "cdr", ""}).ok());
  PayloadArena arena({256U, 16U, 4096U});
  const std::vector<std::byte> first = serialized_string("old");
  const std::vector<std::byte> second{
    std::byte{0x00}, std::byte{0x01}, std::byte{0x00}, std::byte{0x00},
    std::byte{0x2a}, std::byte{0x00}, std::byte{0x00}, std::byte{0x00}};
  write_payload(
    writer, arena,
    make_event(
      1U, 1U, 100U, 1000, static_cast<uint32_t>(first.size()),
      to_underlying(EventFlag::kSerializedMessage) |
      to_underlying(EventFlag::kRosTimeValid)),
    first);
  write_payload(
    writer, arena,
    make_event(
      2U, 2U, 200U, 2000, static_cast<uint32_t>(second.size()),
      to_underlying(EventFlag::kSerializedMessage) |
      to_underlying(EventFlag::kRosTimeValid)),
    second);
  ASSERT_TRUE(writer.close().ok());
  const std::filesystem::path output = directory.path() / "churn.mcap";

  Rosbag2ExportSummary summary{};
  const CaptureStatus status = export_rosbag2_data(
    writer.closed_segments().front().path, output, summary);
  EXPECT_EQ(status.code, CaptureStatusCode::kCorruptData);
  EXPECT_NE(status.message.find("type churn"), std::string::npos);
  EXPECT_FALSE(std::filesystem::exists(output));
  EXPECT_FALSE(has_export_temporary(directory.path()));

  const std::filesystem::path retry_input =
    create_native_segment(directory.path(), "retry", false);
  const CaptureStatus retry_status = export_rosbag2_data(retry_input, output, summary);
  EXPECT_TRUE(retry_status.ok()) << retry_status.message;
  EXPECT_TRUE(summary.published);
}

TEST(Rosbag2ExporterTest, RefusesOverwriteAndPreservesExistingFile)
{
  TestDirectory directory;
  const std::filesystem::path input =
    create_native_segment(directory.path(), "overwrite", false);
  const std::filesystem::path output = directory.path() / "existing.mcap";
  {
    std::ofstream stream(output);
    stream << "sentinel";
  }

  Rosbag2ExportSummary summary{};
  const CaptureStatus status = export_rosbag2_data(input, output, summary);
  EXPECT_EQ(status.code, CaptureStatusCode::kAlreadyOpen);
  std::ifstream stream(output);
  EXPECT_EQ(
    std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()),
    "sentinel");
}

TEST(Rosbag2ExporterTest, RejectsUnfinalizedSession)
{
  TestDirectory directory;
  (void)create_native_segment(directory.path(), "unclean", false);
  const std::filesystem::path session = directory.path() / "capture_unclean";
  {
    std::ofstream quality(session / "capture_quality.json");
    quality <<
      "{\"schema_version\":\"blackboxrs.capture_quality.v1\",\"clean\":false}\n";
  }

  Rosbag2ExportSummary summary{};
  const CaptureStatus status = export_rosbag2_data(
    session, directory.path() / "unclean.mcap", summary);
  EXPECT_EQ(status.code, CaptureStatusCode::kCorruptData);
  EXPECT_NE(status.message.find("not clean"), std::string::npos);
}

}  // namespace
}  // namespace blackbox_capture
