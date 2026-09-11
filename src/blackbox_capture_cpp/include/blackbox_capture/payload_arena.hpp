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

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <vector>

#include "blackbox_capture/event.hpp"

namespace blackbox_capture
{

struct PayloadArenaConfig
{
  uint32_t block_size{4096};
  uint32_t block_count{4096};
  uint32_t max_payload_bytes{4U * 1024U * 1024U};
};

enum class PayloadAllocationResult
{
  kSuccess = 0,
  kInvalidArgument,
  kOversized,
  kExhausted,
};

enum class PayloadReleaseResult
{
  kSuccess = 0,
  kEmpty,
  kInvalidHandle,
  kStaleHandle,
  kCorruptChain,
};

struct PayloadArenaStats
{
  uint32_t block_size{0};
  uint32_t block_count{0};
  uint32_t free_blocks{0};
  uint32_t high_watermark_blocks{0};
  uint64_t allocation_attempts{0};
  uint64_t allocation_failures{0};
  uint64_t oversized_payloads{0};
  uint64_t bytes_in_use{0};
};

class PayloadArena
{
public:
  explicit PayloadArena(PayloadArenaConfig config)
  : config_(validate(config))
  {
    const std::size_t byte_count = static_cast<std::size_t>(config_.block_size) *
      static_cast<std::size_t>(config_.block_count);
    bytes_.resize(byte_count);
    blocks_.resize(config_.block_count);
    for (uint32_t index = 0; index < config_.block_count; ++index) {
      blocks_[index].next = index + 1U < config_.block_count ? index + 1U : kInvalidBlockIndex;
    }
    free_head_ = 0U;
    free_blocks_ = config_.block_count;
  }

  PayloadArena(const PayloadArena &) = delete;
  PayloadArena & operator=(const PayloadArena &) = delete;
  PayloadArena(PayloadArena &&) = delete;
  PayloadArena & operator=(PayloadArena &&) = delete;

  [[nodiscard]] PayloadAllocationResult allocate_copy(
    const std::byte * source, std::size_t size,
    PayloadHandle & handle) noexcept
  {
    handle = {};
    ++allocation_attempts_;
    if (size == 0U) {
      return PayloadAllocationResult::kSuccess;
    }
    if (source == nullptr) {
      ++allocation_failures_;
      return PayloadAllocationResult::kInvalidArgument;
    }
    if (size > config_.max_payload_bytes || size > std::numeric_limits<uint32_t>::max()) {
      ++allocation_failures_;
      ++oversized_payloads_;
      return PayloadAllocationResult::kOversized;
    }

    const auto required = static_cast<uint32_t>(
      (size + static_cast<std::size_t>(config_.block_size) - 1U) / config_.block_size);
    if (required > free_blocks_) {
      ++allocation_failures_;
      return PayloadAllocationResult::kExhausted;
    }

    const uint32_t first = free_head_;
    uint32_t current = first;
    std::size_t copied = 0U;
    for (uint32_t used = 0; used < required; ++used) {
      BlockMetadata & block = blocks_[current];
      const uint32_t next_free = block.next;
      block.in_use = true;
      block.generation = next_generation(block.generation);

      const std::size_t amount =
        std::min<std::size_t>(config_.block_size, static_cast<std::size_t>(size) - copied);
      std::memcpy(block_data(current), source + copied, amount);
      copied += amount;

      if (used + 1U == required) {
        block.next = kInvalidBlockIndex;
        free_head_ = next_free;
      } else {
        block.next = next_free;
        current = next_free;
      }
    }

    free_blocks_ -= required;
    bytes_in_use_ += size;
    high_watermark_blocks_ =
      std::max<uint32_t>(high_watermark_blocks_, config_.block_count - free_blocks_);
    handle.first_block = first;
    handle.block_count = required;
    handle.size = static_cast<uint32_t>(size);
    handle.generation = blocks_[first].generation;
    blocks_[first].allocation_blocks = required;
    blocks_[first].allocation_size = static_cast<uint32_t>(size);
    return PayloadAllocationResult::kSuccess;
  }

  [[nodiscard]] bool copy_out(
    const PayloadHandle & handle, std::byte * destination,
    std::size_t capacity) const noexcept
  {
    if (handle.size == 0U && !handle.valid()) {
      return true;
    }
    if (destination == nullptr || capacity < handle.size || !valid_start(handle)) {
      return false;
    }

    std::size_t copied = 0U;
    uint32_t current = handle.first_block;
    for (uint32_t traversed = 0; traversed < handle.block_count; ++traversed) {
      if (current >= config_.block_count || !blocks_[current].in_use) {
        return false;
      }
      const std::size_t amount =
        std::min<std::size_t>(config_.block_size, handle.size - copied);
      std::memcpy(destination + copied, block_data(current), amount);
      copied += amount;
      current = blocks_[current].next;
    }
    return copied == handle.size && current == kInvalidBlockIndex;
  }

  template<typename Visitor>
  [[nodiscard]] bool for_each_block(const PayloadHandle & handle, Visitor && visitor) const
  {
    if (handle.size == 0U && !handle.valid()) {
      return true;
    }
    if (!valid_start(handle)) {
      return false;
    }

    std::size_t visited = 0U;
    uint32_t current = handle.first_block;
    for (uint32_t traversed = 0; traversed < handle.block_count; ++traversed) {
      if (current >= config_.block_count || !blocks_[current].in_use) {
        return false;
      }
      const std::size_t amount =
        std::min<std::size_t>(config_.block_size, handle.size - visited);
      visitor(block_data(current), amount);
      visited += amount;
      current = blocks_[current].next;
    }
    return visited == handle.size && current == kInvalidBlockIndex;
  }

  [[nodiscard]] PayloadReleaseResult release(const PayloadHandle & handle) noexcept
  {
    if (handle.size == 0U && !handle.valid()) {
      return PayloadReleaseResult::kEmpty;
    }
    if (handle.first_block >= config_.block_count || handle.block_count == 0U ||
      handle.block_count > config_.block_count || handle.size == 0U ||
      handle.size > config_.max_payload_bytes)
    {
      return PayloadReleaseResult::kInvalidHandle;
    }
    if (!blocks_[handle.first_block].in_use ||
      blocks_[handle.first_block].generation != handle.generation)
    {
      return PayloadReleaseResult::kStaleHandle;
    }
    if (blocks_[handle.first_block].allocation_blocks != handle.block_count ||
      blocks_[handle.first_block].allocation_size != handle.size)
    {
      return PayloadReleaseResult::kInvalidHandle;
    }

    uint32_t current = handle.first_block;
    for (uint32_t traversed = 0; traversed < handle.block_count; ++traversed) {
      if (current >= config_.block_count || !blocks_[current].in_use) {
        return PayloadReleaseResult::kCorruptChain;
      }
      const uint32_t next = blocks_[current].next;
      if (traversed + 1U < handle.block_count && next == kInvalidBlockIndex) {
        return PayloadReleaseResult::kCorruptChain;
      }
      if (traversed + 1U == handle.block_count && next != kInvalidBlockIndex) {
        return PayloadReleaseResult::kCorruptChain;
      }
      current = next;
    }

    current = handle.first_block;
    uint32_t released_head = kInvalidBlockIndex;
    uint32_t released_tail = kInvalidBlockIndex;
    for (uint32_t traversed = 0; traversed < handle.block_count; ++traversed) {
      const uint32_t next = blocks_[current].next;
      blocks_[current].in_use = false;
      blocks_[current].next = kInvalidBlockIndex;
      blocks_[current].allocation_blocks = 0U;
      blocks_[current].allocation_size = 0U;
      if (released_head == kInvalidBlockIndex) {
        released_head = current;
      } else {
        blocks_[released_tail].next = current;
      }
      released_tail = current;
      current = next;
    }

    blocks_[released_tail].next = free_head_;
    free_head_ = released_head;
    free_blocks_ += handle.block_count;
    bytes_in_use_ -= handle.size;
    return PayloadReleaseResult::kSuccess;
  }

  [[nodiscard]] PayloadArenaStats stats() const noexcept
  {
    return PayloadArenaStats{config_.block_size,
      config_.block_count,
      free_blocks_,
      high_watermark_blocks_,
      allocation_attempts_,
      allocation_failures_,
      oversized_payloads_,
      bytes_in_use_};
  }

  [[nodiscard]] uint32_t block_size() const noexcept {return config_.block_size;}
  [[nodiscard]] uint32_t block_count() const noexcept {return config_.block_count;}
  [[nodiscard]] uint32_t free_blocks() const noexcept {return free_blocks_;}
  [[nodiscard]] uint32_t max_payload_bytes() const noexcept {return config_.max_payload_bytes;}
  [[nodiscard]] std::size_t memory_bytes() const noexcept
  {
    return bytes_.size() + blocks_.size() * sizeof(BlockMetadata);
  }

private:
  struct BlockMetadata
  {
    uint32_t next{kInvalidBlockIndex};
    uint32_t generation{0};
    uint32_t allocation_blocks{0};
    uint32_t allocation_size{0};
    bool in_use{false};
  };

  static PayloadArenaConfig validate(PayloadArenaConfig config)
  {
    if (config.block_size == 0U || config.block_count == 0U ||
      config.max_payload_bytes == 0U)
    {
      throw std::invalid_argument("payload arena dimensions must be greater than zero");
    }
    if (config.max_payload_bytes >
      static_cast<uint64_t>(config.block_size) * config.block_count)
    {
      throw std::invalid_argument("maximum payload exceeds total arena capacity");
    }
    if (static_cast<std::size_t>(config.block_count) >
      std::numeric_limits<std::size_t>::max() / config.block_size)
    {
      throw std::invalid_argument("payload arena byte size overflows size_t");
    }
    return config;
  }

  static uint32_t next_generation(uint32_t generation) noexcept
  {
    ++generation;
    return generation == 0U ? 1U : generation;
  }

  [[nodiscard]] bool valid_start(const PayloadHandle & handle) const noexcept
  {
    return handle.first_block < config_.block_count && handle.block_count != 0U &&
           handle.block_count <= config_.block_count && handle.size != 0U &&
           handle.size <= config_.max_payload_bytes && blocks_[handle.first_block].in_use &&
           blocks_[handle.first_block].generation == handle.generation &&
           blocks_[handle.first_block].allocation_blocks == handle.block_count &&
           blocks_[handle.first_block].allocation_size == handle.size;
  }

  std::byte * block_data(uint32_t index) noexcept
  {
    return bytes_.data() + static_cast<std::size_t>(index) * config_.block_size;
  }
  const std::byte * block_data(uint32_t index) const noexcept
  {
    return bytes_.data() + static_cast<std::size_t>(index) * config_.block_size;
  }

  PayloadArenaConfig config_;
  std::vector<std::byte> bytes_;
  std::vector<BlockMetadata> blocks_;
  uint32_t free_head_{kInvalidBlockIndex};
  uint32_t free_blocks_{0};
  uint32_t high_watermark_blocks_{0};
  uint64_t allocation_attempts_{0};
  uint64_t allocation_failures_{0};
  uint64_t oversized_payloads_{0};
  uint64_t bytes_in_use_{0};
};

}  // namespace blackbox_capture
