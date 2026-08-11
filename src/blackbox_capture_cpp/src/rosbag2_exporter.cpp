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

#include "blackbox_capture/rosbag2_exporter.hpp"

#include <fcntl.h>
#include <linux/fs.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <climits>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include <mcap/reader.hpp>
#include <mcap/writer.hpp>

namespace blackbox_capture
{
namespace
{

constexpr std::string_view kControlTopic = "/blackboxrs/events";
constexpr std::string_view kControlSchema = "blackboxrs.capture_event.v1";
constexpr std::string_view kSessionSchema = "blackboxrs.capture_session.v1";
constexpr std::string_view kQualitySchema = "blackboxrs.capture_quality.v1";
constexpr std::string_view kSegmentSchema = "blackboxrs.capture_segment.v1";

class CheckedFileWritable final : public mcap::IWritable
{
public:
  ~CheckedFileWritable() override {close_fd();}

  CaptureStatus open_new(const std::filesystem::path & path)
  {
    fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0640);
    if (fd_ < 0) {
      return CaptureStatus::from_errno("failed to create rosbag2 export temporary");
    }
    return CaptureStatus::success();
  }

  void handleWrite(const std::byte * data, uint64_t size) override
  {
    if (size > std::numeric_limits<uint64_t>::max() - logical_size_) {
      set_fault(EOVERFLOW, "MCAP export size overflow");
      return;
    }
    logical_size_ += size;
    if (faulted_) {
      return;
    }
    uint64_t offset = 0U;
    while (offset < size) {
      const uint64_t remaining = size - offset;
      const std::size_t amount = static_cast<std::size_t>(
        std::min<uint64_t>(remaining, static_cast<uint64_t>(SSIZE_MAX)));
      const ssize_t written = ::write(fd_, data + offset, amount);
      if (written < 0) {
        if (errno == EINTR) {
          continue;
        }
        set_fault(errno, "MCAP export write failed");
        return;
      }
      if (written == 0) {
        set_fault(EIO, "MCAP export write returned zero bytes");
        return;
      }
      offset += static_cast<uint64_t>(written);
      physical_size_ += static_cast<uint64_t>(written);
    }
  }

  void end() override {}
  uint64_t size() const override {return logical_size_;}

  CaptureStatus sync_and_close()
  {
    if (!faulted_ && fd_ >= 0 && ::fdatasync(fd_) != 0) {
      set_fault(errno, "failed to sync rosbag2 export temporary");
    }
    close_fd();
    return faulted_ ? status_ : CaptureStatus::success();
  }

  bool faulted() const noexcept {return faulted_;}
  const CaptureStatus & status() const noexcept {return status_;}
  uint64_t physical_size() const noexcept {return physical_size_;}

private:
  void set_fault(int error_number, std::string message)
  {
    if (faulted_) {
      return;
    }
    faulted_ = true;
    status_ = CaptureStatus::from_errno(std::move(message), error_number);
  }

  void close_fd() noexcept
  {
    if (fd_ >= 0) {
      while (::close(fd_) != 0 && errno == EINTR) {
      }
      fd_ = -1;
    }
  }

  int fd_{-1};
  uint64_t logical_size_{0U};
  uint64_t physical_size_{0U};
  bool faulted_{false};
  CaptureStatus status_{};
};

struct TopicBinding
{
  std::string type;
  std::string schema_encoding;
  std::vector<std::byte> schema_data;
  std::string offered_qos_profiles;
  mcap::ChannelId output_channel_id{0U};
};

class TemporaryPathGuard
{
public:
  explicit TemporaryPathGuard(std::filesystem::path path)
  : path_(std::move(path)) {}

  ~TemporaryPathGuard()
  {
    if (!released_) {
      std::error_code error;
      (void)std::filesystem::remove(path_, error);
    }
  }

  void release() noexcept {released_ = true;}

private:
  std::filesystem::path path_;
  bool released_{false};
};

CaptureStatus read_bounded_text(
  const std::filesystem::path & path, uint64_t max_bytes,
  std::string & output)
{
  std::error_code error;
  const uint64_t size = std::filesystem::file_size(path, error);
  if (error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to inspect native metadata " + path.string() + ": " + error.message(),
      error.value());
  }
  if (size > max_bytes || size > static_cast<uint64_t>(std::numeric_limits<std::size_t>::max())) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCapacityExceeded,
      "native metadata document exceeds export limit: " + path.string());
  }
  std::ifstream stream(path, std::ios::binary);
  if (!stream) {
    return CaptureStatus::from_errno("failed to open native metadata " + path.string());
  }
  output.assign(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
  if (stream.bad()) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to read native metadata " + path.string());
  }
  return CaptureStatus::success();
}

bool has_trailing_mcap_magic(const std::filesystem::path & path)
{
  std::error_code error;
  const uint64_t size = std::filesystem::file_size(path, error);
  if (error || size < sizeof(mcap::Magic)) {
    return false;
  }
  std::ifstream stream(path, std::ios::binary);
  if (!stream) {
    return false;
  }
  stream.seekg(static_cast<std::streamoff>(size - sizeof(mcap::Magic)));
  std::array<std::byte, sizeof(mcap::Magic)> magic{};
  stream.read(reinterpret_cast<char *>(magic.data()), static_cast<std::streamsize>(magic.size()));
  return stream.good() &&
         std::memcmp(magic.data(), mcap::Magic, sizeof(mcap::Magic)) == 0;
}

std::optional<std::size_t> json_value_offset(
  const std::string & document, std::string_view key)
{
  const std::string token = "\"" + std::string(key) + "\"";
  std::size_t position = document.find(token);
  if (position == std::string::npos) {
    return std::nullopt;
  }
  position = document.find(':', position + token.size());
  if (position == std::string::npos) {
    return std::nullopt;
  }
  ++position;
  while (position < document.size() &&
    (document[position] == ' ' || document[position] == '\t' ||
    document[position] == '\r' || document[position] == '\n'))
  {
    ++position;
  }
  return position;
}

bool json_string_equals(
  const std::string & document, std::string_view key,
  std::string_view expected)
{
  const auto position = json_value_offset(document, key);
  return position && *position < document.size() && document[*position] == '"' &&
         document.compare(*position + 1U, expected.size(), expected) == 0 &&
         *position + expected.size() + 1U < document.size() &&
         document[*position + expected.size() + 1U] == '"';
}

bool json_bool_equals(const std::string & document, std::string_view key, bool expected)
{
  const auto position = json_value_offset(document, key);
  if (!position) {
    return false;
  }
  const std::string_view token = expected ? std::string_view{"true"} : std::string_view{"false"};
  if (document.compare(*position, token.size(), token) != 0) {
    return false;
  }
  const std::size_t end = *position + token.size();
  return end == document.size() || document[end] == ',' || document[end] == '}' ||
         document[end] == ' ' || document[end] == '\t' ||
         document[end] == '\r' || document[end] == '\n';
}

bool has_partial_suffix(const std::filesystem::path & path)
{
  const std::string filename = path.filename().string();
  constexpr std::string_view suffix = ".partial.mcap";
  return filename.size() >= suffix.size() &&
         filename.compare(filename.size() - suffix.size(), suffix.size(), suffix) == 0;
}

CaptureStatus validate_segment_sidecar(
  const std::filesystem::path & segment, const Rosbag2ExportLimits & limits)
{
  std::filesystem::path sidecar = segment;
  sidecar.replace_extension(".json");
  std::string document;
  CaptureStatus status = read_bounded_text(
    sidecar, limits.max_metadata_document_bytes, document);
  if (!status) {
    return CaptureStatus::failure(
      status.code,
      "native segment is not finalized: " + status.message,
      status.system_errno);
  }
  if ((!json_string_equals(document, "schema_version", kSegmentSchema) &&
    !json_string_equals(document, "schema", kSegmentSchema)) ||
    !json_string_equals(document, "path", segment.filename().string()) ||
    !json_bool_equals(document, "clean", true))
  {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native segment sidecar is incompatible or not clean: " + sidecar.string());
  }
  return CaptureStatus::success();
}

CaptureStatus validate_session_metadata(
  const std::filesystem::path & session, const Rosbag2ExportLimits & limits)
{
  std::string session_json;
  CaptureStatus status = read_bounded_text(
    session / "session.json", limits.max_metadata_document_bytes, session_json);
  if (!status) {
    return status;
  }
  if (!json_string_equals(session_json, "schema_version", kSessionSchema) &&
    !json_string_equals(session_json, "schema", kSessionSchema))
  {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "input directory is not a versioned native capture session");
  }

  std::string quality_json;
  status = read_bounded_text(
    session / "capture_quality.json", limits.max_metadata_document_bytes, quality_json);
  if (!status) {
    return CaptureStatus::failure(
      status.code,
      "native session is not finalized: " + status.message,
      status.system_errno);
  }
  if (!json_string_equals(quality_json, "schema_version", kQualitySchema) ||
    !json_bool_equals(quality_json, "clean", true))
  {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native session capture quality is absent, incompatible, or not clean");
  }
  return CaptureStatus::success();
}

CaptureStatus collect_segments(
  const std::filesystem::path & input, const Rosbag2ExportLimits & limits,
  std::vector<std::filesystem::path> & segments)
{
  std::error_code error;
  const std::filesystem::file_status input_status = std::filesystem::status(input, error);
  if (error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to inspect export input: " + error.message(), error.value());
  }
  if (std::filesystem::is_regular_file(input_status)) {
    if (input.extension() != ".mcap" || has_partial_suffix(input)) {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "input segment must be a finalized .mcap file");
    }
    CaptureStatus status = validate_segment_sidecar(input, limits);
    if (!status) {
      return status;
    }
    segments.push_back(input);
    return CaptureStatus::success();
  }
  if (!std::filesystem::is_directory(input_status)) {
    return CaptureStatus::failure(
      CaptureStatusCode::kInvalidArgument,
      "input must be a finalized native .mcap segment or session directory");
  }

  CaptureStatus status = validate_session_metadata(input, limits);
  if (!status) {
    return status;
  }
  const std::filesystem::path segment_directory = input / "segments";
  if (!std::filesystem::is_directory(segment_directory, error) || error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native session segments directory is unavailable");
  }
  for (std::filesystem::directory_iterator iterator(segment_directory, error), end;
    !error && iterator != end; iterator.increment(error))
  {
    const std::filesystem::path path = iterator->path();
    if (has_partial_suffix(path)) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "native session contains an unfinished partial segment");
    }
    std::error_code type_error;
    if (path.extension() != ".mcap" || !iterator->is_regular_file(type_error) || type_error) {
      continue;
    }
    if (segments.size() >= limits.max_segments) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCapacityExceeded,
        "native session exceeds the export segment limit");
    }
    status = validate_segment_sidecar(path, limits);
    if (!status) {
      return status;
    }
    segments.push_back(path);
  }
  if (error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to enumerate native session segments: " + error.message(), error.value());
  }
  std::sort(segments.begin(), segments.end());
  if (segments.empty()) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native session contains no finalized segments");
  }
  return CaptureStatus::success();
}

CaptureStatus validate_limits(const Rosbag2ExportLimits & limits)
{
  if (limits.max_segments == 0U || limits.max_topics == 0U ||
    limits.max_schema_bytes == 0U || limits.max_message_bytes == 0U ||
    limits.max_metadata_document_bytes == 0U)
  {
    return CaptureStatus::failure(
      CaptureStatusCode::kInvalidArgument,
      "all rosbag2 export limits must be greater than zero");
  }
  return CaptureStatus::success();
}

CaptureStatus inspect_output_path(
  const std::filesystem::path & output,
  const std::vector<std::filesystem::path> & segments)
{
  if (output.empty() || output.extension() != ".mcap" || has_partial_suffix(output)) {
    return CaptureStatus::failure(
      CaptureStatusCode::kInvalidArgument,
      "output must be a new finalized .mcap path");
  }
  std::error_code error;
  const std::filesystem::file_status status = std::filesystem::symlink_status(output, error);
  if (error && error != std::errc::no_such_file_or_directory) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to inspect export output: " + error.message(), error.value());
  }
  if (!error && status.type() != std::filesystem::file_type::not_found) {
    return CaptureStatus::failure(
      CaptureStatusCode::kAlreadyOpen,
      "refusing to overwrite an existing rosbag2 export");
  }
  const std::filesystem::path parent = output.has_parent_path() ? output.parent_path() : ".";
  if (!std::filesystem::is_directory(parent, error) || error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kInvalidArgument,
      "rosbag2 export parent directory does not exist");
  }
  const std::filesystem::path resolved_output =
    std::filesystem::weakly_canonical(parent, error) / output.filename();
  if (error) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed to resolve rosbag2 export path: " + error.message(), error.value());
  }
  for (const auto & segment : segments) {
    const std::filesystem::path resolved_input = std::filesystem::canonical(segment, error);
    if (error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to resolve native input segment: " + error.message(), error.value());
    }
    if (resolved_input == resolved_output) {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "rosbag2 export output aliases an input segment");
    }
  }
  return CaptureStatus::success();
}

CaptureStatus create_temporary(
  const std::filesystem::path & output, std::filesystem::path & temporary,
  CheckedFileWritable & sink)
{
  static std::atomic<uint64_t> next_id{0U};
  const std::filesystem::path parent = output.has_parent_path() ? output.parent_path() : ".";
  for (std::size_t attempt = 0U; attempt < 64U; ++attempt) {
    const uint64_t id = next_id.fetch_add(1U, std::memory_order_relaxed);
    temporary = parent /
      ("." + output.filename().string() + ".partial." + std::to_string(::getpid()) + "." +
      std::to_string(id));
    CaptureStatus status = sink.open_new(temporary);
    if (status.ok()) {
      return status;
    }
    if (status.system_errno != EEXIST) {
      return status;
    }
  }
  return CaptureStatus::failure(
    CaptureStatusCode::kIoError,
    "could not allocate a unique rosbag2 export temporary");
}

CaptureStatus sync_directory(const std::filesystem::path & directory)
{
  const int descriptor = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (descriptor < 0) {
    return CaptureStatus::from_errno("failed to open rosbag2 export directory for sync");
  }
  int result;
  do {
    result = ::fsync(descriptor);
  } while (result != 0 && errno == EINTR);
  const int sync_error = result == 0 ? 0 : errno;
  while (::close(descriptor) != 0 && errno == EINTR) {
  }
  if (sync_error != 0) {
    return CaptureStatus::from_errno("failed to sync rosbag2 export directory", sync_error);
  }
  return CaptureStatus::success();
}

CaptureStatus publish_no_replace(
  const std::filesystem::path & temporary, const std::filesystem::path & output,
  bool & published)
{
  if (::syscall(
      SYS_renameat2, AT_FDCWD, temporary.c_str(), AT_FDCWD, output.c_str(),
      RENAME_NOREPLACE) != 0)
  {
    if (errno == EEXIST) {
      return CaptureStatus::failure(
        CaptureStatusCode::kAlreadyOpen,
        "refusing to overwrite an existing rosbag2 export", errno);
    }
    return CaptureStatus::from_errno("failed to publish rosbag2 export");
  }
  published = true;
  const std::filesystem::path parent = output.has_parent_path() ? output.parent_path() : ".";
  CaptureStatus status = sync_directory(parent);
  if (!status) {
    status.message =
      "rosbag2 export was published atomically, but directory durability is unconfirmed: " +
      status.message;
  }
  return status;
}

bool schemas_equal(const TopicBinding & binding, const mcap::Schema & schema)
{
  return binding.type == schema.name && binding.schema_encoding == schema.encoding &&
         binding.schema_data == schema.data;
}

CaptureStatus reserve_schema_bytes(
  uint64_t & used, const mcap::Channel & channel, const mcap::Schema & schema,
  const std::string & offered_qos_profiles, const Rosbag2ExportLimits & limits)
{
  uint64_t addition = static_cast<uint64_t>(channel.topic.size()) +
    static_cast<uint64_t>(channel.messageEncoding.size()) +
    static_cast<uint64_t>(schema.name.size()) +
    static_cast<uint64_t>(schema.encoding.size()) +
    static_cast<uint64_t>(schema.data.size()) +
    static_cast<uint64_t>(offered_qos_profiles.size());
  if (addition > limits.max_schema_bytes || used > limits.max_schema_bytes - addition) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCapacityExceeded,
      "rosbag2 export schema metadata exceeds the configured byte limit");
  }
  used += addition;
  return CaptureStatus::success();
}

CaptureStatus register_data_channel(
  const mcap::Channel & source_channel, const mcap::Schema & source_schema,
  mcap::McapWriter & writer, CheckedFileWritable & sink,
  std::map<std::string, TopicBinding> & topics, uint64_t & schema_bytes,
  const Rosbag2ExportLimits & limits)
{
  if (source_channel.messageEncoding != "cdr" ||
    (source_schema.encoding != "ros2msg" && source_schema.encoding != "ros2idl") ||
    source_schema.name.empty() || source_channel.topic.empty())
  {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native data channel is not a compatible CDR ROS channel: " + source_channel.topic);
  }
  const auto qos = source_channel.metadata.find("offered_qos_profiles");
  const std::string offered_qos_profiles =
    qos == source_channel.metadata.end() ? std::string{} : qos->second;
  const auto existing = topics.find(source_channel.topic);
  if (existing != topics.end()) {
    if (!schemas_equal(existing->second, source_schema) ||
      existing->second.offered_qos_profiles != offered_qos_profiles)
    {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "type churn or incompatible channel definition for topic " + source_channel.topic);
    }
    return CaptureStatus::success();
  }
  if (topics.size() >= limits.max_topics) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCapacityExceeded,
      "native input exceeds the rosbag2 export topic limit");
  }
  CaptureStatus status = reserve_schema_bytes(
    schema_bytes, source_channel, source_schema, offered_qos_profiles, limits);
  if (!status) {
    return status;
  }

  mcap::Schema schema = source_schema;
  writer.addSchema(schema);
  mcap::Channel channel(
    source_channel.topic, "cdr", schema.id,
    {{"offered_qos_profiles", offered_qos_profiles}});
  writer.addChannel(channel);
  if (sink.faulted()) {
    return sink.status();
  }
  TopicBinding binding{};
  binding.type = schema.name;
  binding.schema_encoding = schema.encoding;
  binding.schema_data = schema.data;
  binding.offered_qos_profiles = offered_qos_profiles;
  binding.output_channel_id = channel.id;
  topics.emplace(source_channel.topic, std::move(binding));
  return CaptureStatus::success();
}

CaptureStatus export_segment(
  const std::filesystem::path & path, mcap::McapWriter & writer,
  CheckedFileWritable & sink, std::map<std::string, TopicBinding> & topics,
  uint64_t & schema_bytes, Rosbag2ExportSummary & summary,
  const Rosbag2ExportLimits & limits)
{
  mcap::McapReader reader;
  mcap::Status status = reader.open(path.string());
  if (!status.ok()) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "failed to open native segment " + path.string() + ": " + status.message);
  }
  if (!reader.header() || reader.header()->profile != "ros2") {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native segment does not use the ros2 MCAP profile: " + path.string());
  }

  std::string problem;
  const auto on_problem = [&](const mcap::Status & issue) {
      if (problem.empty()) {
        problem = issue.message;
      }
    };
  status = reader.readSummary(mcap::ReadSummaryMethod::ForceScan, on_problem);
  if (!status.ok() || !problem.empty() || !has_trailing_mcap_magic(path)) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native segment is not cleanly readable: " + path.string() +
      (problem.empty() ? "" : ": " + problem));
  }

  std::vector<mcap::ChannelPtr> channels;
  channels.reserve(reader.channels().size());
  for (const auto & entry : reader.channels()) {
    channels.push_back(entry.second);
  }
  std::sort(
    channels.begin(), channels.end(),
    [](const mcap::ChannelPtr & lhs, const mcap::ChannelPtr & rhs) {
      return lhs->topic == rhs->topic ? lhs->id < rhs->id : lhs->topic < rhs->topic;
    });
  for (const mcap::ChannelPtr & channel : channels) {
    const mcap::SchemaPtr schema = reader.schema(channel->schemaId);
    if (schema == nullptr) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "native channel references an unknown schema: " + channel->topic);
    }
    if (channel->topic == kControlTopic) {
      if (channel->messageEncoding != "json" || schema->name != kControlSchema) {
        return CaptureStatus::failure(
          CaptureStatusCode::kCorruptData,
          "native control channel definition is incompatible");
      }
      continue;
    }
    CaptureStatus channel_status = register_data_channel(
      *channel, *schema, writer, sink, topics, schema_bytes, limits);
    if (!channel_status) {
      return channel_status;
    }
  }
  mcap::ReadMessageOptions read_options;
  read_options.readOrder = mcap::ReadMessageOptions::ReadOrder::FileOrder;
  for (const mcap::MessageView & view : reader.readMessages(on_problem, read_options)) {
    if (view.channel == nullptr) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "native message references an unknown channel: " + path.string());
    }
    if (view.channel->topic == kControlTopic) {
      ++summary.control_messages_excluded;
      continue;
    }
    if (view.schema == nullptr) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "native message references an unknown schema: " + view.channel->topic);
    }
    if (view.message.dataSize > limits.max_message_bytes) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCapacityExceeded,
        "native message exceeds the rosbag2 export byte limit");
    }

    const auto topic = topics.find(view.channel->topic);
    if (topic == topics.end() || !schemas_equal(topic->second, *view.schema)) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "native message channel changed after definition scan: " + view.channel->topic);
    }

    mcap::Message message = view.message;
    message.channelId = topic->second.output_channel_id;
    const mcap::Timestamp rosbag_time = message.publishTime != 0U ?
      message.publishTime : message.logTime;
    message.logTime = rosbag_time;
    message.publishTime = rosbag_time;
    status = writer.write(message);
    if (!status.ok()) {
      return CaptureStatus::failure(
        CaptureStatusCode::kMcapError,
        "failed to write rosbag2 MCAP message: " + status.message);
    }
    if (sink.faulted()) {
      return sink.status();
    }
    ++summary.messages;
  }
  if (!problem.empty()) {
    return CaptureStatus::failure(
      CaptureStatusCode::kCorruptData,
      "native segment message scan failed: " + path.string() + ": " + problem);
  }
  return CaptureStatus::success();
}

}  // namespace

CaptureStatus export_rosbag2_data(
  const std::filesystem::path & input,
  const std::filesystem::path & output,
  Rosbag2ExportSummary & summary,
  const Rosbag2ExportLimits & limits)
{
  summary = {};
  CaptureStatus status = validate_limits(limits);
  if (!status) {
    return status;
  }

  std::vector<std::filesystem::path> segments;
  segments.reserve(std::min<std::size_t>(limits.max_segments, 256U));
  status = collect_segments(input, limits, segments);
  if (!status) {
    return status;
  }
  status = inspect_output_path(output, segments);
  if (!status) {
    return status;
  }

  CheckedFileWritable sink;
  std::filesystem::path temporary;
  status = create_temporary(output, temporary, sink);
  if (!status) {
    return status;
  }
  TemporaryPathGuard temporary_guard(temporary);

  mcap::McapWriter writer;
  mcap::McapWriterOptions writer_options("ros2");
  writer_options.library = "blackbox_capture_cpp_rosbag2_export/1";
  writer_options.compression = mcap::Compression::None;
  writer_options.noChunkCRC = false;
  writer_options.enableDataCRC = true;
  writer_options.noSummaryCRC = false;
  writer_options.noSummary = false;
  writer_options.noMessageIndex = false;
  writer_options.noChunkIndex = false;
  writer.open(sink, writer_options);
  if (sink.faulted()) {
    writer.terminate();
    return sink.status();
  }

  std::map<std::string, TopicBinding> topics;
  uint64_t schema_bytes = 0U;
  for (const auto & segment : segments) {
    status = export_segment(segment, writer, sink, topics, schema_bytes, summary, limits);
    if (!status) {
      writer.terminate();
      return status;
    }
  }
  writer.close();
  status = sink.sync_and_close();
  if (!status) {
    return status;
  }

  summary.input_segments = segments.size();
  summary.topics = topics.size();
  summary.output_bytes = sink.physical_size();
  summary.output_path = output;
  status = publish_no_replace(temporary, output, summary.published);
  if (!status) {
    return status;
  }
  temporary_guard.release();
  return CaptureStatus::success();
}

}  // namespace blackbox_capture
