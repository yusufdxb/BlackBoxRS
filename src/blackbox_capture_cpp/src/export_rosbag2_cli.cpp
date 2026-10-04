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

#include <iostream>

#include "blackbox_capture/rosbag2_exporter.hpp"

int main(int argc, char ** argv)
{
  if (argc != 3) {
    std::cerr <<
      "Usage: blackbox_capture_export_rosbag2 INPUT.mcap|SESSION_DIR OUTPUT.mcap\n";
    return 2;
  }

  blackbox_capture::Rosbag2ExportSummary summary{};
  const blackbox_capture::CaptureStatus status =
    blackbox_capture::export_rosbag2_data(argv[1], argv[2], summary);
  if (!status.ok()) {
    std::cerr << "rosbag2 export failed: " << status.message;
    if (status.system_errno != 0) {
      std::cerr << " (errno=" << status.system_errno << ')';
    }
    std::cerr << '\n';
    return 1;
  }

  std::cout << "EXPORTED_ROSBAG2 segments=" << summary.input_segments
            << " topics=" << summary.topics
            << " messages=" << summary.messages
            << " excluded_control_messages=" << summary.control_messages_excluded
            << " bytes=" << summary.output_bytes
            << " output=" << summary.output_path.string() << '\n';
  return 0;
}
