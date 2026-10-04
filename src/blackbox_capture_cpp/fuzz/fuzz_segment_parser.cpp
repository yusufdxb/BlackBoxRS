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

#include <algorithm>
#include <cstddef>
#include <cstdint>

#include <mcap/reader.hpp>

namespace
{

class MemoryReader final : public mcap::IReadable
{
public:
  MemoryReader(const uint8_t * data, std::size_t size)
  : data_(reinterpret_cast<std::byte *>(const_cast<uint8_t *>(data))), size_(size) {}

  uint64_t size() const override {return size_;}

  uint64_t read(std::byte ** output, uint64_t offset, uint64_t requested) override
  {
    if (offset > size_ || requested > size_ - offset) {
      return 0U;
    }
    *output = data_ + offset;
    return requested;
  }

private:
  std::byte * data_;
  uint64_t size_;
};

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const uint8_t * data, std::size_t size)
{
  constexpr std::size_t kMaximumInput = 1024U * 1024U;
  if (data == nullptr || size == 0U || size > kMaximumInput) {
    return 0;
  }
  MemoryReader input(data, size);
  mcap::McapReader reader;
  if (!reader.open(input).ok()) {
    return 0;
  }
  std::size_t messages = 0U;
  const auto on_problem = [](const mcap::Status &) {};
  for (const mcap::MessageView & view : reader.readMessages(on_problem)) {
    messages += view.message.dataSize <= kMaximumInput ? 1U : 0U;
    if (messages >= 4096U) {
      break;
    }
  }
  reader.close();
  return 0;
}
