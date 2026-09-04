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

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "blackbox_capture/payload_arena.hpp"

namespace blackbox_capture
{
namespace
{

std::vector<std::byte> bytes(std::size_t count)
{
  std::vector<std::byte> result(count);
  for (std::size_t index = 0; index < count; ++index) {
    result[index] = static_cast<std::byte>(index & 0xffU);
  }
  return result;
}

TEST(PayloadArenaTest, RejectsInvalidConfiguration) {
  EXPECT_THROW(PayloadArena((PayloadArenaConfig{0U, 1U, 1U})), std::invalid_argument);
  EXPECT_THROW(PayloadArena((PayloadArenaConfig{8U, 2U, 17U})), std::invalid_argument);
}

TEST(PayloadArenaTest, EmptyPayloadUsesNoBlock) {
  PayloadArena arena({16U, 4U, 32U});
  PayloadHandle handle{};
  EXPECT_EQ(arena.allocate_copy(nullptr, 0U, handle), PayloadAllocationResult::kSuccess);
  EXPECT_FALSE(handle.valid());
  EXPECT_EQ(arena.free_blocks(), 4U);
  EXPECT_TRUE(arena.copy_out(handle, nullptr, 0U));
  EXPECT_EQ(arena.release(handle), PayloadReleaseResult::kEmpty);
}

TEST(PayloadArenaTest, CopiesAcrossBlockBoundariesAndVisitsExactLengths) {
  PayloadArena arena({8U, 5U, 32U});
  const auto source = bytes(19U);
  PayloadHandle handle{};
  ASSERT_EQ(
    arena.allocate_copy(source.data(), source.size(), handle),
    PayloadAllocationResult::kSuccess);
  EXPECT_EQ(handle.block_count, 3U);
  EXPECT_EQ(arena.free_blocks(), 2U);

  std::array<std::size_t, 3> lengths{};
  std::size_t visited = 0U;
  ASSERT_TRUE(
    arena.for_each_block(
      handle, [&](const std::byte *, std::size_t size) {
        lengths[visited++] = size;
      }));
  EXPECT_EQ(lengths, (std::array<std::size_t, 3>{8U, 8U, 3U}));

  std::vector<std::byte> destination(source.size());
  ASSERT_TRUE(arena.copy_out(handle, destination.data(), destination.size()));
  EXPECT_EQ(destination, source);
  EXPECT_EQ(arena.release(handle), PayloadReleaseResult::kSuccess);
  EXPECT_EQ(arena.free_blocks(), 5U);
}

TEST(PayloadArenaTest, OversizedAndExhaustedAreDistinct) {
  PayloadArena arena({8U, 3U, 16U});
  const auto too_large = bytes(17U);
  PayloadHandle handle{};
  EXPECT_EQ(
    arena.allocate_copy(too_large.data(), too_large.size(), handle),
    PayloadAllocationResult::kOversized);

  const auto payload = bytes(16U);
  PayloadHandle first{};
  PayloadHandle second{};
  ASSERT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), first),
    PayloadAllocationResult::kSuccess);
  EXPECT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), second),
    PayloadAllocationResult::kExhausted);
  const PayloadArenaStats stats = arena.stats();
  EXPECT_EQ(stats.allocation_failures, 2U);
  EXPECT_EQ(stats.oversized_payloads, 1U);
}

TEST(PayloadArenaTest, GenerationRejectsStaleAndForgedHandles) {
  PayloadArena arena({8U, 2U, 16U});
  const auto payload = bytes(8U);
  PayloadHandle first{};
  ASSERT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), first),
    PayloadAllocationResult::kSuccess);

  PayloadHandle forged = first;
  forged.size = 7U;
  std::array<std::byte, 8> destination{};
  EXPECT_FALSE(arena.copy_out(forged, destination.data(), destination.size()));
  EXPECT_EQ(arena.release(forged), PayloadReleaseResult::kInvalidHandle);

  ASSERT_EQ(arena.release(first), PayloadReleaseResult::kSuccess);
  EXPECT_EQ(arena.release(first), PayloadReleaseResult::kStaleHandle);

  PayloadHandle second{};
  ASSERT_EQ(
    arena.allocate_copy(payload.data(), payload.size(), second),
    PayloadAllocationResult::kSuccess);
  EXPECT_NE(second.generation, first.generation);
  EXPECT_FALSE(arena.copy_out(first, destination.data(), destination.size()));
}

}  // namespace
}  // namespace blackbox_capture
