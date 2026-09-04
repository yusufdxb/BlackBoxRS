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

#include <cstdint>
#include <limits>
#include <type_traits>

namespace blackbox_capture
{

enum class EventFlag : uint32_t
{
  kNone = 0,
  kSerializedMessage = 1U << 0U,
  kGraphEvent = 1U << 1U,
  kDropEvent = 1U << 2U,
  kTriggerEvent = 1U << 3U,
  kClockEvent = 1U << 4U,
  kProcessEvent = 1U << 5U,
  kStatusEvent = 1U << 6U,
  kRosTimeValid = 1U << 16U,
  kHighPriority = 1U << 17U,
};

constexpr uint32_t to_underlying(EventFlag flag) noexcept
{
  return static_cast<uint32_t>(flag);
}

constexpr EventFlag operator|(EventFlag lhs, EventFlag rhs) noexcept
{
  return static_cast<EventFlag>(to_underlying(lhs) | to_underlying(rhs));
}

constexpr EventFlag operator&(EventFlag lhs, EventFlag rhs) noexcept
{
  return static_cast<EventFlag>(to_underlying(lhs) & to_underlying(rhs));
}

constexpr bool has_flag(uint32_t flags, EventFlag flag) noexcept
{
  return (flags & to_underlying(flag)) != 0U;
}

struct EventHeader
{
  uint64_t monotonic_ns{0};
  int64_t ros_time_ns{0};
  uint64_t sequence{0};
  uint32_t topic_id{0};
  uint32_t payload_size{0};
  uint32_t flags{0};
  uint32_t reserved{0};
};

constexpr uint32_t kInvalidBlockIndex = std::numeric_limits<uint32_t>::max();

struct PayloadHandle
{
  uint32_t first_block{kInvalidBlockIndex};
  uint32_t block_count{0};
  uint32_t size{0};
  uint32_t generation{0};

  constexpr bool valid() const noexcept
  {
    return first_block != kInvalidBlockIndex && block_count != 0U;
  }
};

struct Event
{
  EventHeader header{};
  PayloadHandle payload{};
};

enum class TriggerCode : uint16_t
{
  kDeadTopic = 1,
  kRateLow = 2,
  kRateHigh = 3,
  kQueueHighWatermark = 4,
  kQueueOverflow = 5,
  kPayloadExhausted = 6,
  kWriterLag = 7,
  kStorageFault = 8,
  kClockBackward = 9,
  kClockForward = 10,
};

enum class Severity : uint16_t
{
  kInfo = 0,
  kWarning = 1,
  kError = 2,
  kCritical = 3,
};

struct TriggerEvent
{
  TriggerCode code{TriggerCode::kDeadTopic};
  Severity severity{Severity::kWarning};
  uint32_t topic_id{0};
  uint64_t first_seen_ns{0};
  uint64_t confirmed_ns{0};
  float value{0.0F};
  float threshold{0.0F};
};

enum class DropReason : uint16_t
{
  kRingFull = 0,
  kControlReserveFull = 1,
  kPayloadExhausted = 2,
  kPayloadOversized = 3,
  kRegistryExhausted = 4,
  kStorageFault = 5,
  kShutdownCutoff = 6,
  kLowPriorityShed = 7,
  kMalformedPayload = 8,
  kInvariantFault = 9,
  kCount = 10,
};

struct DropEvent
{
  DropReason reason{DropReason::kRingFull};
  uint16_t reserved{0};
  uint32_t topic_id{0};
  uint64_t count{0};
  uint64_t bytes{0};
  uint64_t first_monotonic_ns{0};
  uint64_t last_monotonic_ns{0};
  uint64_t first_sequence{0};
  uint64_t last_sequence{0};
};

static_assert(sizeof(EventHeader) == 40U, "EventHeader layout is part of the native ABI");
static_assert(sizeof(PayloadHandle) == 16U, "PayloadHandle must remain compact");
static_assert(sizeof(Event) == 56U, "Event descriptors are budgeted at 56 bytes");
static_assert(std::is_trivially_copyable<Event>::value, "Ring events must be trivially copyable");
static_assert(std::is_trivially_copyable<TriggerEvent>::value, "Trigger payload must be POD");
static_assert(std::is_trivially_copyable<DropEvent>::value, "Drop payload must be POD");

}  // namespace blackbox_capture
