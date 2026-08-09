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

#include <atomic>
#include <cstdint>
#include <thread>

#include "blackbox_capture/event.hpp"
#include "blackbox_capture/metrics.hpp"
#include "blackbox_capture/ring_buffer.hpp"

namespace blackbox_capture
{
namespace
{

TEST(SpscRingBufferTest, StartsEmptyAndRejectsInvalidConfiguration) {
  EXPECT_THROW((SpscRingBuffer<uint64_t>(0U)), std::invalid_argument);
  EXPECT_THROW((SpscRingBuffer<uint64_t>(4U, 4U)), std::invalid_argument);

  SpscRingBuffer<uint64_t> ring(4U);
  uint64_t value = 99U;
  EXPECT_TRUE(ring.empty());
  EXPECT_EQ(ring.size(), 0U);
  EXPECT_FALSE(ring.try_pop(value));
  EXPECT_EQ(value, 99U);
}

TEST(SpscRingBufferTest, RejectsNewestWhenFullAndAccountsTheDrop) {
  SpscRingBuffer<uint64_t> ring(3U);
  EXPECT_TRUE(ring.try_push(10U));
  EXPECT_TRUE(ring.try_push(11U));
  EXPECT_TRUE(ring.try_push(12U));
  EXPECT_FALSE(ring.try_push(13U));
  EXPECT_EQ(ring.rejected_data(), 1U);
  EXPECT_EQ(ring.high_watermark(), 3U);

  uint64_t value = 0U;
  EXPECT_TRUE(ring.try_pop(value));
  EXPECT_EQ(value, 10U);
  EXPECT_TRUE(ring.try_pop(value));
  EXPECT_EQ(value, 11U);
  EXPECT_TRUE(ring.try_pop(value));
  EXPECT_EQ(value, 12U);
}

TEST(SpscRingBufferTest, WrapAroundPreservesSequenceOrder) {
  SpscRingBuffer<Event> ring(4U);
  for (uint64_t sequence = 1U; sequence <= 100U; ++sequence) {
    Event event{};
    event.header.sequence = sequence;
    ASSERT_TRUE(ring.try_push(event));
    Event popped{};
    ASSERT_TRUE(ring.try_pop(popped));
    EXPECT_EQ(popped.header.sequence, sequence);
  }
}

TEST(SpscRingBufferTest, ControlReserveCannotBeConsumedByData) {
  SpscRingBuffer<uint64_t> ring(5U, 2U);
  EXPECT_EQ(ring.data_capacity(), 3U);
  EXPECT_TRUE(ring.try_push(1U));
  EXPECT_TRUE(ring.try_push(2U));
  EXPECT_TRUE(ring.try_push(3U));
  EXPECT_FALSE(ring.try_push(4U));
  EXPECT_TRUE(ring.try_push(100U, AdmissionClass::kControl));
  EXPECT_TRUE(ring.try_push(101U, AdmissionClass::kControl));
  EXPECT_FALSE(ring.try_push(102U, AdmissionClass::kControl));
  EXPECT_EQ(ring.rejected_data(), 1U);
  EXPECT_EQ(ring.rejected_control(), 1U);
}

TEST(SpscRingBufferTest, ProducerAndConsumerCanRunConcurrently) {
  constexpr uint64_t kCount = 100000U;
  SpscRingBuffer<uint64_t> ring(1024U);
  std::atomic<bool> producer_done{false};
  std::thread producer([&]() {
      for (uint64_t sequence = 1U; sequence <= kCount; ) {
        if (ring.try_push(sequence)) {
          ++sequence;
        }
      }
      producer_done.store(true, std::memory_order_release);
    });

  uint64_t expected = 1U;
  uint64_t value = 0U;
  while (!producer_done.load(std::memory_order_acquire) || !ring.empty()) {
    if (ring.try_pop(value)) {
      ASSERT_EQ(value, expected);
      ++expected;
    }
  }
  producer.join();
  EXPECT_EQ(expected, kCount + 1U);
}

TEST(CaptureMetricsTest, CumulativeDropLedgerPreservesFirstAndLastEvidence) {
  CaptureMetrics metrics(2U);
  metrics.record_received(1U, 100U);
  metrics.record_received(1U, 200U);
  metrics.record_admitted(1U, 100U);
  metrics.record_committed(1U, 100U);
  metrics.record_durable();
  metrics.record_drop(1U, DropReason::kRingFull, 200U, 50U, 7U);
  metrics.record_drop(1U, DropReason::kRingFull, 300U, 70U, 9U);
  metrics.observe_queue_depth(7U, 10U);
  metrics.observe_queue_depth(3U, 10U);
  metrics.record_storage_error();
  metrics.record_clock_anomaly();

  const MetricsSnapshot aggregate = metrics.aggregate_snapshot();
  EXPECT_EQ(aggregate.received, 2U);
  EXPECT_EQ(aggregate.received_bytes, 300U);
  EXPECT_EQ(aggregate.admitted, 1U);
  EXPECT_EQ(aggregate.committed, 1U);
  EXPECT_EQ(aggregate.durable, 1U);
  EXPECT_EQ(aggregate.dropped, 2U);
  EXPECT_EQ(aggregate.dropped_bytes, 500U);
  EXPECT_EQ(aggregate.peak_queue_depth, 7U);
  EXPECT_EQ(aggregate.storage_errors, 1U);
  EXPECT_EQ(aggregate.clock_anomalies, 1U);

  const DropSnapshot drop = metrics.drop_snapshot(1U, DropReason::kRingFull);
  EXPECT_EQ(drop.count, 2U);
  EXPECT_EQ(drop.bytes, 500U);
  EXPECT_EQ(drop.first_monotonic_ns, 50U);
  EXPECT_EQ(drop.last_monotonic_ns, 70U);
  EXPECT_EQ(drop.first_sequence, 7U);
  EXPECT_EQ(drop.last_sequence, 9U);
}

}  // namespace
}  // namespace blackbox_capture
