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

#include <cstddef>
#include <cstdint>
#include <filesystem>

#include "blackbox_capture/status.hpp"

namespace blackbox_capture
{

struct Rosbag2ExportLimits
{
  std::size_t max_segments{4096U};
  std::size_t max_topics{4096U};
  uint64_t max_schema_bytes{16U * 1024U * 1024U};
  uint64_t max_message_bytes{64U * 1024U * 1024U};
  uint64_t max_metadata_document_bytes{4U * 1024U * 1024U};
};

struct Rosbag2ExportSummary
{
  std::size_t input_segments{0U};
  std::size_t topics{0U};
  uint64_t messages{0U};
  uint64_t control_messages_excluded{0U};
  uint64_t output_bytes{0U};
  std::filesystem::path output_path{};
  bool published{false};
};

/// Export finalized native CDR data to a standard, indexed rosbag2 MCAP file.
///
/// `input` may be one finalized native `.mcap` segment or a finalized native
/// session directory containing `session.json`, a clean `capture_quality.json`,
/// and finalized files under `segments/`. The destination must not exist.
/// Publication uses a same-directory temporary followed by an atomic,
/// no-overwrite rename.
[[nodiscard]] CaptureStatus export_rosbag2_data(
  const std::filesystem::path & input,
  const std::filesystem::path & output,
  Rosbag2ExportSummary & summary,
  const Rosbag2ExportLimits & limits = {});

}  // namespace blackbox_capture
