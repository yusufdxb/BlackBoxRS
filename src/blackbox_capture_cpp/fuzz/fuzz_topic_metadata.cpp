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
#include <string_view>

#include "blackbox_capture/topic_registry.hpp"

extern "C" int LLVMFuzzerTestOneInput(const uint8_t * data, std::size_t size)
{
  constexpr std::size_t kMaximumInput = 4096U;
  if (data == nullptr || size == 0U || size > kMaximumInput) {
    return 0;
  }
  const std::size_t first = size > 1U ? static_cast<std::size_t>(data[0]) % size : 0U;
  const std::size_t second =
    size > 2U ? first + (static_cast<std::size_t>(data[1]) % (size - first)) : first;
  const char * characters = reinterpret_cast<const char *>(data);
  blackbox_capture::TopicRegistry registry(8U, kMaximumInput * 3U);
  const auto result = registry.register_topic(
    std::string_view(characters, first),
    std::string_view(characters + first, second - first),
    std::string_view(characters + second, size - second));
  if (result.ok()) {
    (void)registry.by_id(result.topic_id);
    (void)registry.find_exact(
      std::string_view(characters, first),
      std::string_view(characters + first, second - first),
      std::string_view(characters + second, size - second));
  }
  return 0;
}
