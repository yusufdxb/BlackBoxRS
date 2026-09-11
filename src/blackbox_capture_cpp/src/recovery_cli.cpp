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

#include <filesystem>
#include <iostream>

#include "blackbox_capture/segment_writer.hpp"

int main(int argc, char ** argv)
{
  if (argc != 3) {
    std::cerr << "Usage: blackbox_capture_recover INPUT.partial.mcap OUTPUT.mcap\n";
    return 2;
  }
  blackbox_capture::RecoveryResult result{};
  const auto status = blackbox_capture::SegmentWriter::recover_partial(
    std::filesystem::path(argv[1]), std::filesystem::path(argv[2]), result);
  if (!status.ok()) {
    std::cerr << "Recovery failed: " << status.message << '\n';
    return 1;
  }
  std::cout << "Recovered " << result.recovered_messages << " messages to "
            << result.output_path << "; discarded " << result.discarded_tail_bytes
            << " trailing bytes\n";
  return 0;
}
