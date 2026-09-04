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

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <mcap/reader.hpp>

#include "blackbox_capture/segment_writer.hpp"

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
      (std::filesystem::temp_directory_path() / "blackbox-capture-XXXXXX").string();
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

SegmentWriterOptions options_for(
  const std::filesystem::path & root,
  std::string session = "test")
{
  SegmentWriterOptions options{};
  options.output_directory = root;
  options.session_id = std::move(session);
  options.max_segment_bytes = 1024U * 1024U;
  options.max_segment_events = 100U;
  options.chunk_size_bytes = 64U * 1024U;
  options.max_payload_bytes = 8192U;
  options.max_topics = 8U;
  options.max_topic_metadata_bytes = 4096U;
  return options;
}

TopicDefinition test_topic()
{
  return TopicDefinition{1U, "/test/data", "std_msgs/msg/ByteMultiArray", "cdr", "reliable"};
}

Event make_event(
  uint64_t sequence, uint64_t monotonic_ns, uint32_t size,
  uint32_t flags = to_underlying(EventFlag::kSerializedMessage) |
  to_underlying(EventFlag::kRosTimeValid))
{
  Event event{};
  event.header.monotonic_ns = monotonic_ns;
  event.header.ros_time_ns = static_cast<int64_t>(monotonic_ns + 1000U);
  event.header.sequence = sequence;
  event.header.topic_id = 1U;
  event.header.payload_size = size;
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

std::size_t count_messages(const std::filesystem::path & path, std::string * control = nullptr)
{
  mcap::McapReader reader;
  const mcap::Status status = reader.open(path.string());
  EXPECT_TRUE(status.ok()) << status.message;
  std::size_t count = 0U;
  for (const mcap::MessageView & view : reader.readMessages()) {
    ++count;
    if (control != nullptr && view.channel->topic == "/blackboxrs/events") {
      control->assign(reinterpret_cast<const char *>(view.message.data), view.message.dataSize);
    }
  }
  reader.close();
  return count;
}

std::vector<uint32_t> read_message_sequences(const std::filesystem::path & path)
{
  mcap::McapReader reader;
  const mcap::Status status = reader.open(path.string());
  EXPECT_TRUE(status.ok()) << status.message;
  std::vector<uint32_t> sequences;
  for (const mcap::MessageView & view : reader.readMessages()) {
    sequences.push_back(view.message.sequence);
  }
  reader.close();
  return sequences;
}

std::vector<mcap::ByteOffset> chunk_offsets(const std::filesystem::path & path)
{
  mcap::McapReader reader;
  const mcap::Status open_status = reader.open(path.string());
  EXPECT_TRUE(open_status.ok()) << open_status.message;
  std::vector<mcap::ByteOffset> offsets;
  if (!open_status.ok() || reader.dataSource() == nullptr) {
    return offsets;
  }
  mcap::TypedRecordReader records(
    *reader.dataSource(), sizeof(mcap::Magic), std::filesystem::file_size(path));
  records.onChunk = [&](const mcap::Chunk &, mcap::ByteOffset offset) {
      offsets.push_back(offset);
    };
  while (records.next()) {
  }
  reader.close();
  return offsets;
}

void corrupt_payload(
  const std::filesystem::path & path, std::byte marker,
  std::size_t marker_size)
{
  std::fstream stream(path, std::ios::binary | std::ios::in | std::ios::out);
  ASSERT_TRUE(stream.good());
  std::vector<char> bytes(
    (std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
  const std::vector<char> marker_bytes(marker_size, static_cast<char>(marker));
  const auto marker_position = std::search(
    bytes.begin(), bytes.end(), marker_bytes.begin(), marker_bytes.end());
  ASSERT_NE(marker_position, bytes.end());
  const std::streamoff offset =
    static_cast<std::streamoff>(std::distance(bytes.begin(), marker_position) + 8);
  stream.clear();
  stream.seekp(offset);
  const char corrupt = static_cast<char>(0xa5);
  stream.write(&corrupt, 1);
  stream.close();
}

struct McapLayout
{
  std::size_t chunks{0U};
  std::size_t message_indexes{0U};
  std::size_t chunk_indexes{0U};
  std::size_t footers{0U};
  bool chunk_crc_present{false};
  uint64_t summary_start{UINT64_MAX};
  uint64_t summary_offset_start{UINT64_MAX};
};

McapLayout inspect_layout(const std::filesystem::path & path)
{
  mcap::McapReader reader;
  const mcap::Status open_status = reader.open(path.string());
  EXPECT_TRUE(open_status.ok()) << open_status.message;
  McapLayout layout{};
  if (!open_status.ok() || reader.dataSource() == nullptr) {
    return layout;
  }
  mcap::TypedRecordReader records(
    *reader.dataSource(), sizeof(mcap::Magic), std::filesystem::file_size(path));
  records.onChunk = [&](const mcap::Chunk & chunk, mcap::ByteOffset) {
      ++layout.chunks;
      layout.chunk_crc_present = layout.chunk_crc_present || chunk.uncompressedCrc != 0U;
    };
  records.onMessageIndex = [&](const mcap::MessageIndex &, mcap::ByteOffset) {
      ++layout.message_indexes;
    };
  records.onChunkIndex = [&](const mcap::ChunkIndex &, mcap::ByteOffset) {
      ++layout.chunk_indexes;
    };
  records.onFooter = [&](const mcap::Footer & footer, mcap::ByteOffset) {
      ++layout.footers;
      layout.summary_start = footer.summaryStart;
      layout.summary_offset_start = footer.summaryOffsetStart;
    };
  while (records.next()) {
  }
  reader.close();
  return layout;
}

TEST(SegmentWriterTest, WritesReadableMcapAndVersionedSidecar) {
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path()));
  ASSERT_TRUE(writer.open().ok()) << writer.last_status().message;
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());

  PayloadArena arena({64U, 16U, 512U});
  std::vector<std::byte> payload(100U, std::byte{0x2a});
  write_payload(
    writer, arena,
    make_event(41U, 500U, static_cast<uint32_t>(payload.size())), payload);
  SegmentCounters counters{};
  counters.received = 1U;
  counters.admitted = 1U;
  counters.committed = 1U;
  counters.bytes_captured = payload.size();
  counters.peak_queue_utilization = 73U;
  writer.set_segment_counters(counters);
  ASSERT_TRUE(writer.close().ok()) << writer.last_status().message;

  ASSERT_EQ(writer.closed_segments().size(), 1U);
  const SegmentInfo & info = writer.closed_segments().front();
  EXPECT_TRUE(info.clean);
  EXPECT_EQ(info.first_sequence, 41U);
  EXPECT_EQ(info.last_sequence, 41U);
  EXPECT_EQ(info.sha256.size(), 64U);
  EXPECT_EQ(count_messages(info.path), 1U);

  const auto sidecar = info.path.parent_path() / "0000000000000000.json";
  std::ifstream stream(sidecar);
  const std::string json((std::istreambuf_iterator<char>(stream)),
    std::istreambuf_iterator<char>());
  EXPECT_NE(json.find("blackboxrs.capture_segment.v1"), std::string::npos);
  EXPECT_NE(json.find("\"committed\":1"), std::string::npos);
  EXPECT_NE(json.find("\"accounting_scope\":\"session_cumulative\""), std::string::npos);
  EXPECT_NE(json.find("\"sha256\":"), std::string::npos);
  const std::size_t event_count_key = json.find("\"event_count\":");
  ASSERT_NE(event_count_key, std::string::npos);
  EXPECT_EQ(json.find("\"event_count\":", event_count_key + 1U), std::string::npos);

  std::ifstream session_stream(directory.path() / "capture_test" / "session.json");
  const std::string session_json((std::istreambuf_iterator<char>(session_stream)),
    std::istreambuf_iterator<char>());
  EXPECT_NE(
    session_json.find("\"schema\":\"blackboxrs.capture_session.v1\""),
    std::string::npos);
  EXPECT_NE(session_json.find("\"monotonic_anchor_ns\":"), std::string::npos);
  EXPECT_NE(session_json.find("\"system_time_anchor_ns\":"), std::string::npos);
}

TEST(SegmentWriterTest, WritesChecksummedChunksWithoutOnlineIndexesOrSummary)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "bounded-index"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(64U, std::byte{0x17});
  write_payload(writer, arena, make_event(1U, 10U, 64U), payload);
  write_payload(writer, arena, make_event(2U, 20U, 64U), payload);
  ASSERT_TRUE(writer.close().ok()) << writer.last_status().message;

  const auto & path = writer.closed_segments().front().path;
  EXPECT_EQ(count_messages(path), 2U);
  const McapLayout layout = inspect_layout(path);
  EXPECT_GT(layout.chunks, 0U);
  EXPECT_TRUE(layout.chunk_crc_present);
  EXPECT_EQ(layout.message_indexes, 0U);
  EXPECT_EQ(layout.chunk_indexes, 0U);
  EXPECT_EQ(layout.footers, 1U);
  EXPECT_EQ(layout.summary_start, 0U);
  EXPECT_EQ(layout.summary_offset_start, 0U);
}

TEST(SegmentWriterTest, StaleLegacyMetadataTemporaryDoesNotBlockFinalization)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "stale-metadata"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(32U, std::byte{0x21});
  write_payload(writer, arena, make_event(1U, 10U, 32U), payload);

  const auto sidecar = directory.path() / "capture_stale-metadata" / "segments" /
    "0000000000000000.json";
  const auto stale_temporary = sidecar.string() + ".tmp";
  {
    std::ofstream stream(stale_temporary);
    ASSERT_TRUE(stream.good());
    stream << "stale";
  }

  ASSERT_TRUE(writer.close().ok()) << writer.last_status().message;
  EXPECT_TRUE(std::filesystem::exists(sidecar));
  EXPECT_TRUE(std::filesystem::exists(stale_temporary));
  EXPECT_EQ(count_messages(writer.closed_segments().front().path), 1U);
}

TEST(SegmentWriterTest, PersistsControlEnvelopeWithDualClockAndGlobalSequence) {
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "control"));
  ASSERT_TRUE(writer.open().ok());
  PayloadArena arena({64U, 8U, 256U});
  const std::string object = "{\"change\":\"publisher_appeared\"}";
  std::vector<std::byte> payload(object.size());
  std::memcpy(payload.data(), object.data(), object.size());
  Event event = make_event(
    0x1'0000'0005ULL, 900U, static_cast<uint32_t>(payload.size()),
    to_underlying(EventFlag::kGraphEvent) |
    to_underlying(EventFlag::kRosTimeValid));
  write_payload(writer, arena, event, payload);
  ASSERT_TRUE(writer.close().ok());

  std::string control;
  ASSERT_EQ(count_messages(writer.closed_segments().front().path, &control), 1U);
  EXPECT_NE(
    control.find("\"schema_version\":\"blackboxrs.capture_event.v1\""),
    std::string::npos);
  EXPECT_NE(control.find("\"kind\":\"graph\""), std::string::npos);
  EXPECT_NE(control.find("\"monotonic_ns\":900"), std::string::npos);
  EXPECT_NE(control.find("\"sequence\":4294967301"), std::string::npos);
  EXPECT_NE(control.find(object), std::string::npos);
}

TEST(SegmentWriterTest, PreservesAlreadyVersionedControlEnvelopeWithoutDoubleWrapping) {
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "envelope"));
  ASSERT_TRUE(writer.open().ok());
  PayloadArena arena({128U, 8U, 512U});
  const std::string envelope =
    "{\"schema_version\":\"blackboxrs.capture_event.v1\",\"kind\":\"status\","
    "\"monotonic_ns\":10,\"ros_time_ns\":20,\"sequence\":30,\"topic_id\":0,"
    "\"flags\":64,\"payload\":{\"state\":\"NORMAL\"}}";
  std::vector<std::byte> payload(envelope.size());
  std::memcpy(payload.data(), envelope.data(), envelope.size());
  Event event = make_event(
    30U, 10U, static_cast<uint32_t>(payload.size()),
    to_underlying(EventFlag::kStatusEvent) | to_underlying(EventFlag::kRosTimeValid));
  write_payload(writer, arena, event, payload);
  ASSERT_TRUE(writer.close().ok());

  std::string control;
  ASSERT_EQ(count_messages(writer.closed_segments().front().path, &control), 1U);
  EXPECT_EQ(control, envelope);
}

TEST(SegmentWriterTest, MonotonicOrderSurvivesRosTimeRollback) {
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "clock-rollback"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 256U});
  const std::vector<std::byte> payload(8U, std::byte{0x04});

  Event first = make_event(1U, 100U, static_cast<uint32_t>(payload.size()));
  first.header.ros_time_ns = 1000;
  Event second = make_event(2U, 200U, static_cast<uint32_t>(payload.size()));
  second.header.ros_time_ns = 500;
  Event third = make_event(3U, 300U, static_cast<uint32_t>(payload.size()));
  third.header.ros_time_ns = 1500;
  write_payload(writer, arena, first, payload);
  write_payload(writer, arena, second, payload);
  write_payload(writer, arena, third, payload);
  ASSERT_TRUE(writer.close().ok());

  mcap::McapReader reader;
  ASSERT_TRUE(reader.open(writer.closed_segments().front().path.string()).ok());
  std::vector<uint64_t> monotonic_times;
  std::vector<uint64_t> ros_times;
  for (const mcap::MessageView & view : reader.readMessages()) {
    monotonic_times.push_back(view.message.logTime);
    ros_times.push_back(view.message.publishTime);
  }
  reader.close();
  EXPECT_EQ(monotonic_times, (std::vector<uint64_t>{100U, 200U, 300U}));
  EXPECT_EQ(ros_times, (std::vector<uint64_t>{1000U, 500U, 1500U}));
}

TEST(SegmentWriterTest, RotationAndClosedSummaryMemoryAreBounded) {
  TestDirectory directory;
  auto options = options_for(directory.path(), "rotation");
  options.max_segment_events = 1U;
  options.max_closed_segment_records = 2U;
  SegmentWriter writer(options);
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({16U, 8U, 64U});
  std::vector<std::byte> payload(8U, std::byte{0x01});
  for (uint64_t sequence = 1U; sequence <= 5U; ++sequence) {
    write_payload(
      writer, arena,
      make_event(sequence, sequence, static_cast<uint32_t>(payload.size())), payload);
  }
  ASSERT_TRUE(writer.close().ok());
  ASSERT_EQ(writer.closed_segments().size(), 2U);
  EXPECT_EQ(writer.closed_segments()[0].segment_index, 3U);
  EXPECT_EQ(writer.closed_segments()[1].segment_index, 4U);
  for (uint64_t index = 0U; index < 5U; ++index) {
    const auto sidecar = directory.path() / "capture_rotation" / "segments" /
      (std::string(15U, '0') + std::to_string(index) + ".json");
    EXPECT_TRUE(std::filesystem::exists(sidecar));
  }
}

TEST(SegmentWriterTest, ScriptedShortSyscallsAreRetried) {
  TestDirectory directory;
  auto options = options_for(directory.path(), "short");
  options.failure_injection.max_bytes_per_syscall = 3U;
  SegmentWriter writer(options);
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 16U, 512U});
  std::vector<std::byte> payload(128U, std::byte{0x07});
  write_payload(
    writer, arena,
    make_event(1U, 1U, static_cast<uint32_t>(payload.size())), payload);
  ASSERT_TRUE(writer.close().ok()) << writer.last_status().message;
  EXPECT_EQ(count_messages(writer.closed_segments().front().path), 1U);
}

TEST(SegmentWriterTest, InjectedDiskFullLeavesPartialAndReportsFault) {
  TestDirectory directory;
  auto options = options_for(directory.path(), "full");
  options.failure_injection.fail_after_bytes = 512U;
  options.failure_injection.failure_errno = ENOSPC;
  SegmentWriter writer(options);
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({1024U, 8U, 4096U});
  std::vector<std::byte> payload(4096U, std::byte{0x33});
  Event event = make_event(1U, 1U, static_cast<uint32_t>(payload.size()));
  ASSERT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), event.payload),
    PayloadAllocationResult::kSuccess);
  ASSERT_TRUE(writer.write_event(event, arena).ok());
  const CaptureStatus status = writer.flush();
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code, CaptureStatusCode::kNoSpace);
  EXPECT_TRUE(writer.faulted());
  EXPECT_TRUE(
    std::filesystem::exists(
      directory.path() / "capture_full" / "segments" /
      "0000000000000000.partial.mcap"));
  EXPECT_FALSE(
    std::filesystem::exists(
      directory.path() / "capture_full" / "segments" /
      "0000000000000000.mcap"));
  EXPECT_EQ(arena.release(event.payload), PayloadReleaseResult::kSuccess);
}

TEST(SegmentWriterTest, InjectedSyncAndRenameFailuresRemainExplicit) {
  for (const bool fail_sync : {true, false}) {
    TestDirectory directory;
    auto options = options_for(directory.path(), fail_sync ? "sync" : "rename");
    options.failure_injection.fail_sync = fail_sync;
    options.failure_injection.fail_rename = !fail_sync;
    options.failure_injection.failure_errno = EIO;
    SegmentWriter writer(options);
    ASSERT_TRUE(writer.open().ok());
    ASSERT_TRUE(writer.register_topic(test_topic()).ok());
    PayloadArena arena({16U, 2U, 16U});
    std::vector<std::byte> payload(8U, std::byte{0x09});
    write_payload(writer, arena, make_event(1U, 1U, 8U), payload);
    const CaptureStatus status = writer.close();
    EXPECT_FALSE(status.ok());
    EXPECT_EQ(status.code, CaptureStatusCode::kIoError);
    EXPECT_TRUE(writer.faulted());
  }
}

TEST(SegmentWriterTest, RecoversMessagesFromTruncatedCleanSegment) {
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "recover"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 16U, 512U});
  std::vector<std::byte> payload(32U, std::byte{0x55});
  write_payload(
    writer, arena,
    make_event(1U, 10U, static_cast<uint32_t>(payload.size())), payload);
  write_payload(
    writer, arena,
    make_event(2U, 20U, static_cast<uint32_t>(payload.size())), payload);
  ASSERT_TRUE(writer.close().ok());
  const auto source = writer.closed_segments().front().path;
  const uint64_t original_size = std::filesystem::file_size(source);
  ASSERT_GT(original_size, 64U);
  std::filesystem::resize_file(source, original_size - 32U);

  RecoveryResult result{};
  const auto recovered = directory.path() / "recovered.mcap";
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_TRUE(result.recovered);
  EXPECT_FALSE(result.input_was_clean);
  EXPECT_TRUE(result.unwritten_tail_loss_unknown);
  EXPECT_EQ(result.recovered_messages, 2U);
  ASSERT_TRUE(result.last_recovered_sequence_low32.has_value());
  EXPECT_EQ(*result.last_recovered_sequence_low32, 2U);
  EXPECT_GT(result.discarded_tail_bytes, 0U);
  EXPECT_LE(result.discarded_tail_bytes, original_size - 32U);
  EXPECT_EQ(count_messages(recovered), 2U);
  const auto recovery_sidecar = recovered.string() + ".recovery.json";
  ASSERT_TRUE(std::filesystem::exists(recovery_sidecar));
  std::ifstream recovery_stream(recovery_sidecar);
  const std::string recovery_json((std::istreambuf_iterator<char>(recovery_stream)),
    std::istreambuf_iterator<char>());
  EXPECT_NE(
    recovery_json.find("blackboxrs.capture_recovery.v1"),
    std::string::npos);
  EXPECT_NE(recovery_json.find("\"recovered_messages\":2"), std::string::npos);
  EXPECT_NE(
    recovery_json.find("\"unwritten_tail_loss_unknown\":true"),
    std::string::npos);
  EXPECT_NE(
    recovery_json.find("\"last_recovered_sequence_low32\":2"),
    std::string::npos);
}

TEST(SegmentWriterTest, RecoveryReplacesOwnedStalePartialAndSidecar)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "retry-recovery"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(16U, std::byte{0x2c});
  write_payload(writer, arena, make_event(9U, 90U, 16U), payload);
  ASSERT_TRUE(writer.close().ok());

  const auto source = writer.closed_segments().front().path;
  const auto recovered = directory.path() / "retry.mcap";
  const auto stale_partial = recovered.string() + ".partial";
  const auto stale_sidecar = recovered.string() + ".recovery.json";
  {
    std::ofstream stream(stale_partial, std::ios::binary);
    ASSERT_TRUE(stream.good());
    stream << "incomplete";
  }
  {
    std::ofstream stream(stale_sidecar);
    ASSERT_TRUE(stream.good());
    stream << "stale";
  }

  RecoveryResult result{};
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_TRUE(result.recovered);
  EXPECT_TRUE(result.input_was_clean);
  EXPECT_FALSE(result.unwritten_tail_loss_unknown);
  EXPECT_EQ(count_messages(recovered), 1U);
  EXPECT_FALSE(std::filesystem::exists(stale_partial));
  EXPECT_TRUE(std::filesystem::exists(stale_sidecar));
  std::ifstream sidecar_stream(stale_sidecar);
  const std::string sidecar_json((std::istreambuf_iterator<char>(sidecar_stream)),
    std::istreambuf_iterator<char>());
  EXPECT_NE(sidecar_json.find("blackboxrs.capture_recovery.v1"), std::string::npos);
}

TEST(SegmentWriterTest, RecoveryDoesNotTreatJunkBeforeTrailingMagicAsClean)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "junk-before-magic"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(16U, std::byte{0x33});
  write_payload(writer, arena, make_event(1U, 10U, 16U), payload);
  ASSERT_TRUE(writer.close().ok());
  const auto source = writer.closed_segments().front().path;

  std::ifstream input(source, std::ios::binary);
  ASSERT_TRUE(input.good());
  std::vector<char> bytes(
    (std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  ASSERT_GE(bytes.size(), sizeof(mcap::Magic));
  bytes.insert(bytes.end() - static_cast<std::ptrdiff_t>(sizeof(mcap::Magic)), '\x7f');
  std::ofstream output(source, std::ios::binary | std::ios::trunc);
  ASSERT_TRUE(output.good());
  output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
  output.close();

  RecoveryResult result{};
  const auto recovered = directory.path() / "junk-before-magic-recovered.mcap";
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_TRUE(result.recovered);
  EXPECT_FALSE(result.input_was_clean);
  EXPECT_TRUE(result.unwritten_tail_loss_unknown);
  EXPECT_GT(result.discarded_tail_bytes, 0U);
  EXPECT_EQ(result.corruption_reason, "trailing or structurally invalid bytes");
}

TEST(SegmentWriterTest, RecoveryRejectsInputThatAliasesOwnedOutputPaths)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "alias-source"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(16U, std::byte{0x31});
  write_payload(writer, arena, make_event(1U, 10U, 16U), payload);
  ASSERT_TRUE(writer.close().ok());
  const auto source = writer.closed_segments().front().path;

  for (const char * suffix : {".partial", ".recovery.json"}) {
    const auto recovery_output = directory.path() / "alias";
    const std::filesystem::path input = recovery_output.string() + suffix;
    std::filesystem::copy_file(source, input);
    const uint64_t original_bytes = std::filesystem::file_size(input);
    RecoveryResult result{};
    const CaptureStatus status =
      SegmentWriter::recover_partial(input, recovery_output, result);
    EXPECT_FALSE(status.ok());
    EXPECT_EQ(status.code, CaptureStatusCode::kInvalidArgument);
    EXPECT_TRUE(std::filesystem::exists(input));
    EXPECT_EQ(std::filesystem::file_size(input), original_bytes);
  }
}

TEST(SegmentWriterTest, RecoveryRejectsForgedUncompressedChunkSize)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "forged-chunk"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 8U, 512U});
  const std::vector<std::byte> payload(32U, std::byte{0x42});
  write_payload(writer, arena, make_event(1U, 10U, 32U), payload);
  ASSERT_TRUE(writer.close().ok());
  const auto source = writer.closed_segments().front().path;

  mcap::McapReader reader;
  ASSERT_TRUE(reader.open(source.string()).ok());
  ASSERT_NE(reader.dataSource(), nullptr);
  mcap::ByteOffset chunk_offset = 0U;
  mcap::TypedRecordReader records(
    *reader.dataSource(), sizeof(mcap::Magic), std::filesystem::file_size(source));
  records.onChunk = [&](const mcap::Chunk &, mcap::ByteOffset offset) {
      chunk_offset = offset;
    };
  while (chunk_offset == 0U && records.next()) {
  }
  reader.close();
  ASSERT_NE(chunk_offset, 0U);

  // Chunk record header (opcode + length), start time, then end time precede
  // the little-endian uncompressed-size field.
  constexpr uint64_t forged_size = 1024ULL * 1024ULL * 1024ULL;
  std::fstream stream(source, std::ios::binary | std::ios::in | std::ios::out);
  ASSERT_TRUE(stream.good());
  stream.seekp(static_cast<std::streamoff>(chunk_offset + 1U + 8U + 8U + 8U));
  for (std::size_t byte = 0U; byte < sizeof(forged_size); ++byte) {
    stream.put(static_cast<char>((forged_size >> (byte * 8U)) & 0xffU));
  }
  stream.close();

  RecoveryResult result{};
  const auto recovered = directory.path() / "forged-recovered.mcap";
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code, CaptureStatusCode::kCorruptData);
  EXPECT_FALSE(std::filesystem::exists(recovered));
}

TEST(SegmentWriterTest, RecoveryStopsAtCorruptChunkAndPreservesOnlyStrictPrefix)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "corrupt-recovery"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 16U, 512U});
  constexpr std::size_t kPayloadSize = 32U;
  for (uint32_t sequence = 1U; sequence <= 3U; ++sequence) {
    const std::vector<std::byte> payload(
      kPayloadSize, static_cast<std::byte>(sequence * 0x11U));
    write_payload(
      writer, arena,
      make_event(sequence, sequence * 10U, static_cast<uint32_t>(payload.size())), payload);
    ASSERT_TRUE(writer.flush().ok());
  }
  ASSERT_TRUE(writer.close().ok());

  const auto source = writer.closed_segments().front().path;
  const std::vector<mcap::ByteOffset> offsets = chunk_offsets(source);
  ASSERT_EQ(offsets.size(), 3U);
  const uint64_t input_size = std::filesystem::file_size(source);
  corrupt_payload(source, std::byte{0x22}, kPayloadSize);

  RecoveryResult result{};
  const auto recovered = directory.path() / "corrupt-recovered.mcap";
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_TRUE(result.recovered);
  EXPECT_FALSE(result.input_was_clean);
  EXPECT_TRUE(result.unwritten_tail_loss_unknown);
  EXPECT_EQ(result.recovered_messages, 1U);
  ASSERT_TRUE(result.last_recovered_sequence_low32.has_value());
  EXPECT_EQ(*result.last_recovered_sequence_low32, 1U);
  EXPECT_EQ(result.discarded_tail_bytes, input_size - offsets[1]);
  EXPECT_EQ(result.corruption_reason, "partial MCAP contains a chunk CRC mismatch");
  EXPECT_EQ(read_message_sequences(recovered), std::vector<uint32_t>({1U}));
  EXPECT_FALSE(std::filesystem::exists(recovered.string() + ".partial"));
  const std::filesystem::path sidecar = recovered.string() + ".recovery.json";
  ASSERT_TRUE(std::filesystem::exists(sidecar));
  std::ifstream sidecar_stream(sidecar);
  const std::string sidecar_json((std::istreambuf_iterator<char>(sidecar_stream)),
    std::istreambuf_iterator<char>());
  EXPECT_NE(
    sidecar_json.find(
      "\"discarded_tail_bytes\":" + std::to_string(input_size - offsets[1])),
    std::string::npos);
  EXPECT_NE(
    sidecar_json.find(
      "\"corruption_reason\":\"partial MCAP contains a chunk CRC mismatch\""),
    std::string::npos);

  RecoveryResult retry_result{};
  const CaptureStatus retry_status =
    SegmentWriter::recover_partial(source, recovered, retry_result);
  EXPECT_FALSE(retry_status.ok());
  EXPECT_EQ(read_message_sequences(recovered), std::vector<uint32_t>({1U}));
}

TEST(SegmentWriterTest, RecoveryOfCorruptFirstChunkReissuesNoMessages)
{
  TestDirectory directory;
  SegmentWriter writer(options_for(directory.path(), "first-chunk-corrupt"));
  ASSERT_TRUE(writer.open().ok());
  ASSERT_TRUE(writer.register_topic(test_topic()).ok());
  PayloadArena arena({64U, 16U, 512U});
  constexpr std::size_t kPayloadSize = 32U;
  for (uint32_t sequence = 1U; sequence <= 2U; ++sequence) {
    const std::vector<std::byte> payload(
      kPayloadSize, static_cast<std::byte>(sequence * 0x33U));
    write_payload(
      writer, arena,
      make_event(sequence, sequence * 10U, static_cast<uint32_t>(payload.size())), payload);
    ASSERT_TRUE(writer.flush().ok());
  }
  ASSERT_TRUE(writer.close().ok());

  const auto source = writer.closed_segments().front().path;
  const std::vector<mcap::ByteOffset> offsets = chunk_offsets(source);
  ASSERT_EQ(offsets.size(), 2U);
  const uint64_t input_size = std::filesystem::file_size(source);
  corrupt_payload(source, std::byte{0x33}, kPayloadSize);

  RecoveryResult result{};
  const auto recovered = directory.path() / "first-chunk-recovered.mcap";
  const CaptureStatus status = SegmentWriter::recover_partial(source, recovered, result);
  ASSERT_TRUE(status.ok()) << status.message;
  EXPECT_TRUE(result.recovered);
  EXPECT_FALSE(result.input_was_clean);
  EXPECT_TRUE(result.unwritten_tail_loss_unknown);
  EXPECT_EQ(result.recovered_messages, 0U);
  EXPECT_FALSE(result.last_recovered_sequence_low32.has_value());
  EXPECT_EQ(result.discarded_tail_bytes, input_size - offsets.front());
  EXPECT_EQ(result.corruption_reason, "partial MCAP contains a chunk CRC mismatch");
  EXPECT_TRUE(read_message_sequences(recovered).empty());
  EXPECT_FALSE(std::filesystem::exists(recovered.string() + ".partial"));
  EXPECT_TRUE(std::filesystem::exists(recovered.string() + ".recovery.json"));
}

}  // namespace
}  // namespace blackbox_capture
