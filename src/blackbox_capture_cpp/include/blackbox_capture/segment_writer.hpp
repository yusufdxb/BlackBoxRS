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

#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/metrics.hpp"
#include "blackbox_capture/payload_arena.hpp"
#include "blackbox_capture/status.hpp"

namespace blackbox_capture
{

struct WriterFailureInjection
{
  uint64_t fail_after_bytes{UINT64_MAX};
  int failure_errno{0};
  std::size_t max_bytes_per_syscall{0};
  std::chrono::microseconds delay_per_write{0};
  bool fail_sync{false};
  bool fail_rename{false};
};

struct SegmentWriterOptions
{
  std::filesystem::path output_directory{};
  std::string session_id{};
  uint64_t max_segment_bytes{256ULL * 1024ULL * 1024ULL};
  uint64_t max_segment_events{1'000'000ULL};
  uint64_t chunk_size_bytes{1ULL * 1024ULL * 1024ULL};
  uint32_t max_payload_bytes{4U * 1024U * 1024U};
  std::size_t max_topics{256U};
  std::size_t max_topic_metadata_bytes{256U * 1024U};
  std::size_t max_closed_segment_records{128U};
  uint64_t session_monotonic_anchor_ns{0};
  int64_t session_system_anchor_ns{0};
  bool sync_on_rotation{true};
  WriterFailureInjection failure_injection{};
};

struct TopicDefinition
{
  uint32_t topic_id{0};
  std::string topic{};
  std::string type{};
  std::string serialization_format{"cdr"};
  std::string qos_metadata{};
};

struct SegmentCounters
{
  uint64_t received{0};
  uint64_t admitted{0};
  uint64_t committed{0};
  uint64_t dropped{0};
  uint64_t bytes_captured{0};
  uint64_t bytes_dropped{0};
  uint64_t peak_queue_utilization{0};
  uint64_t storage_errors{0};
  uint64_t clock_anomalies{0};
};

struct SegmentInfo
{
  uint64_t segment_index{0};
  std::filesystem::path path{};
  bool clean{false};
  bool recovered{false};
  uint64_t first_sequence{0};
  uint64_t last_sequence{0};
  uint64_t first_monotonic_ns{0};
  uint64_t last_monotonic_ns{0};
  uint64_t event_count{0};
  uint64_t file_bytes{0};
  std::string sha256{};
};

struct RecoveryResult
{
  bool recovered{false};
  bool input_was_clean{false};
  bool unwritten_tail_loss_unknown{false};
  uint64_t recovered_messages{0};
  uint64_t discarded_tail_bytes{0};
  std::optional<uint32_t> last_recovered_sequence_low32{};
  std::string corruption_reason{};
  std::filesystem::path output_path{};
};

class SegmentWriter
{
public:
  explicit SegmentWriter(SegmentWriterOptions options);
  ~SegmentWriter();

  SegmentWriter(const SegmentWriter &) = delete;
  SegmentWriter & operator=(const SegmentWriter &) = delete;
  SegmentWriter(SegmentWriter &&) = delete;
  SegmentWriter & operator=(SegmentWriter &&) = delete;

  [[nodiscard]] CaptureStatus open();
  [[nodiscard]] CaptureStatus register_topic(const TopicDefinition & definition);
  [[nodiscard]] CaptureStatus write_event(const Event & event, const PayloadArena & arena);
  [[nodiscard]] CaptureStatus flush();
  [[nodiscard]] CaptureStatus rotate();
  [[nodiscard]] CaptureStatus close();

  void set_segment_counters(const SegmentCounters & counters) noexcept;
  [[nodiscard]] bool is_open() const noexcept;
  [[nodiscard]] bool faulted() const noexcept;
  [[nodiscard]] const CaptureStatus & last_status() const noexcept;
  [[nodiscard]] const SegmentInfo & current_segment() const noexcept;
  [[nodiscard]] const std::vector<SegmentInfo> & closed_segments() const noexcept;

  [[nodiscard]] static CaptureStatus recover_partial(
    const std::filesystem::path & input,
    const std::filesystem::path & output,
    RecoveryResult & result);

private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace blackbox_capture
