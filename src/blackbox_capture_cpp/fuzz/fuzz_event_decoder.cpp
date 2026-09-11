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

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/payload_arena.hpp"

extern "C" int LLVMFuzzerTestOneInput(const uint8_t * data, std::size_t size)
{
  constexpr std::size_t kMaximumPayload = 64U * 1024U;
  if (data == nullptr || size < sizeof(blackbox_capture::EventHeader) ||
    size > sizeof(blackbox_capture::EventHeader) + kMaximumPayload)
  {
    return 0;
  }
  blackbox_capture::Event event{};
  std::memcpy(&event.header, data, sizeof(event.header));
  const std::size_t payload_size = size - sizeof(event.header);
  event.header.payload_size = static_cast<uint32_t>(payload_size);

  blackbox_capture::PayloadArena arena({256U, 256U, kMaximumPayload});
  if (arena.allocate_copy(
      reinterpret_cast<const std::byte *>(data + sizeof(event.header)),
      payload_size, event.payload) !=
    blackbox_capture::PayloadAllocationResult::kSuccess)
  {
    return 0;
  }
  std::vector<std::byte> output(payload_size);
  (void)arena.copy_out(event.payload, output.data(), output.size());
  (void)arena.release(event.payload);
  return 0;
}
