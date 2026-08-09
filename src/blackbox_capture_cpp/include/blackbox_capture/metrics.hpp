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
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>

#include "blackbox_capture/event.hpp"

namespace blackbox_capture
{

struct DropSnapshot
{
  uint64_t count{0};
  uint64_t bytes{0};
  uint64_t first_monotonic_ns{0};
  uint64_t last_monotonic_ns{0};
  uint64_t first_sequence{0};
  uint64_t last_sequence{0};
};

struct TopicMetricsSnapshot
{
  uint64_t received{0};
  uint64_t received_bytes{0};
  uint64_t admitted{0};
  uint64_t admitted_bytes{0};
  uint64_t committed{0};
  uint64_t committed_bytes{0};
  uint64_t dropped{0};
  uint64_t dropped_bytes{0};
};

struct MetricsSnapshot : TopicMetricsSnapshot
{
  uint64_t durable{0};
  uint64_t storage_errors{0};
  uint64_t clock_anomalies{0};
  uint64_t peak_queue_depth{0};
  uint64_t peak_queue_capacity{0};
};

class CaptureMetrics
{
public:
  explicit CaptureMetrics(uint32_t max_topic_id)
  : max_topic_id_(validate_max_topic_id(max_topic_id)),
    topics_(new TopicCounters[static_cast<std::size_t>(max_topic_id_) + 1U]),
    drops_(new DropCounters[(static_cast<std::size_t>(max_topic_id_) + 1U) *
      reason_count()]) {}

  CaptureMetrics(const CaptureMetrics &) = delete;
  CaptureMetrics & operator=(const CaptureMetrics &) = delete;

  void record_received(uint32_t topic_id, uint64_t bytes) noexcept
  {
    update_pair(topic(topic_id).received, topic(topic_id).received_bytes, 1U, bytes);
    update_pair(total_.received, total_.received_bytes, 1U, bytes);
  }

  void record_admitted(uint32_t topic_id, uint64_t bytes) noexcept
  {
    update_pair(topic(topic_id).admitted, topic(topic_id).admitted_bytes, 1U, bytes);
    update_pair(total_.admitted, total_.admitted_bytes, 1U, bytes);
  }

  void record_committed(uint32_t topic_id, uint64_t bytes) noexcept
  {
    update_pair(topic(topic_id).committed, topic(topic_id).committed_bytes, 1U, bytes);
    update_pair(total_.committed, total_.committed_bytes, 1U, bytes);
  }

  void record_durable(uint64_t count = 1U) noexcept
  {
    durable_.fetch_add(count, std::memory_order_relaxed);
  }

  void record_drop(
    uint32_t topic_id, DropReason reason, uint64_t bytes,
    uint64_t monotonic_ns, uint64_t sequence) noexcept
  {
    TopicCounters & topic_counters = topic(topic_id);
    topic_counters.dropped.fetch_add(1U, std::memory_order_relaxed);
    topic_counters.dropped_bytes.fetch_add(bytes, std::memory_order_relaxed);
    total_.dropped.fetch_add(1U, std::memory_order_relaxed);
    total_.dropped_bytes.fetch_add(bytes, std::memory_order_relaxed);

    DropCounters & ledger = drop(topic_id, reason);
    const uint64_t previous = ledger.count.fetch_add(1U, std::memory_order_relaxed);
    ledger.bytes.fetch_add(bytes, std::memory_order_relaxed);
    if (previous == 0U) {
      ledger.first_monotonic_ns.store(monotonic_ns, std::memory_order_relaxed);
      ledger.first_sequence.store(sequence, std::memory_order_relaxed);
    }
    ledger.last_monotonic_ns.store(monotonic_ns, std::memory_order_release);
    ledger.last_sequence.store(sequence, std::memory_order_release);
  }

  void observe_queue_depth(uint64_t depth, uint64_t capacity) noexcept
  {
    update_max(peak_queue_depth_, depth);
    update_max(peak_queue_capacity_, capacity);
  }

  void record_storage_error() noexcept
  {
    storage_errors_.fetch_add(1U, std::memory_order_relaxed);
  }

  void record_clock_anomaly(uint64_t count = 1U) noexcept
  {
    clock_anomalies_.fetch_add(count, std::memory_order_relaxed);
  }

  [[nodiscard]] TopicMetricsSnapshot topic_snapshot(uint32_t topic_id) const noexcept
  {
    return snapshot(topic(topic_id));
  }

  [[nodiscard]] MetricsSnapshot aggregate_snapshot() const noexcept
  {
    const TopicMetricsSnapshot counters = snapshot(total_);
    MetricsSnapshot result{};
    static_cast<TopicMetricsSnapshot &>(result) = counters;
    result.durable = durable_.load(std::memory_order_acquire);
    result.storage_errors = storage_errors_.load(std::memory_order_acquire);
    result.clock_anomalies = clock_anomalies_.load(std::memory_order_acquire);
    result.peak_queue_depth = peak_queue_depth_.load(std::memory_order_acquire);
    result.peak_queue_capacity = peak_queue_capacity_.load(std::memory_order_acquire);
    return result;
  }

  [[nodiscard]] DropSnapshot drop_snapshot(
    uint32_t topic_id,
    DropReason reason) const noexcept
  {
    const DropCounters & ledger = drop(topic_id, reason);
    return DropSnapshot{ledger.count.load(std::memory_order_acquire),
      ledger.bytes.load(std::memory_order_acquire),
      ledger.first_monotonic_ns.load(std::memory_order_acquire),
      ledger.last_monotonic_ns.load(std::memory_order_acquire),
      ledger.first_sequence.load(std::memory_order_acquire),
      ledger.last_sequence.load(std::memory_order_acquire)};
  }

  [[nodiscard]] uint32_t max_topic_id() const noexcept {return max_topic_id_;}
  [[nodiscard]] std::size_t memory_bytes() const noexcept
  {
    const std::size_t topics = static_cast<std::size_t>(max_topic_id_) + 1U;
    return sizeof(*this) + topics * sizeof(TopicCounters) +
           topics * reason_count() * sizeof(DropCounters);
  }

private:
  struct TopicCounters
  {
    std::atomic<uint64_t> received{0};
    std::atomic<uint64_t> received_bytes{0};
    std::atomic<uint64_t> admitted{0};
    std::atomic<uint64_t> admitted_bytes{0};
    std::atomic<uint64_t> committed{0};
    std::atomic<uint64_t> committed_bytes{0};
    std::atomic<uint64_t> dropped{0};
    std::atomic<uint64_t> dropped_bytes{0};
  };

  struct DropCounters
  {
    std::atomic<uint64_t> count{0};
    std::atomic<uint64_t> bytes{0};
    std::atomic<uint64_t> first_monotonic_ns{0};
    std::atomic<uint64_t> last_monotonic_ns{0};
    std::atomic<uint64_t> first_sequence{0};
    std::atomic<uint64_t> last_sequence{0};
  };

  static constexpr std::size_t reason_count() noexcept
  {
    return static_cast<std::size_t>(DropReason::kCount);
  }

  static uint32_t validate_max_topic_id(uint32_t max_topic_id)
  {
    if (max_topic_id == UINT32_MAX) {
      throw std::invalid_argument("maximum topic ID is too large");
    }
    return max_topic_id;
  }

  [[nodiscard]] uint32_t normalize_topic(uint32_t topic_id) const noexcept
  {
    return topic_id <= max_topic_id_ ? topic_id : 0U;
  }

  TopicCounters & topic(uint32_t topic_id) noexcept {return topics_[normalize_topic(topic_id)];}
  const TopicCounters & topic(uint32_t topic_id) const noexcept
  {
    return topics_[normalize_topic(topic_id)];
  }

  DropCounters & drop(uint32_t topic_id, DropReason reason) noexcept
  {
    std::size_t reason_index = static_cast<std::size_t>(reason);
    if (reason_index >= reason_count()) {
      reason_index = static_cast<std::size_t>(DropReason::kInvariantFault);
    }
    return drops_[static_cast<std::size_t>(normalize_topic(topic_id)) * reason_count() +
             reason_index];
  }
  const DropCounters & drop(uint32_t topic_id, DropReason reason) const noexcept
  {
    std::size_t reason_index = static_cast<std::size_t>(reason);
    if (reason_index >= reason_count()) {
      reason_index = static_cast<std::size_t>(DropReason::kInvariantFault);
    }
    return drops_[static_cast<std::size_t>(normalize_topic(topic_id)) * reason_count() +
             reason_index];
  }

  static void update_pair(
    std::atomic<uint64_t> & count, std::atomic<uint64_t> & bytes,
    uint64_t count_delta, uint64_t byte_delta) noexcept
  {
    count.fetch_add(count_delta, std::memory_order_relaxed);
    bytes.fetch_add(byte_delta, std::memory_order_relaxed);
  }

  static void update_max(std::atomic<uint64_t> & target, uint64_t value) noexcept
  {
    uint64_t current = target.load(std::memory_order_relaxed);
    while (current < value && !target.compare_exchange_weak(
        current, value, std::memory_order_relaxed,
        std::memory_order_relaxed))
    {
    }
  }

  static TopicMetricsSnapshot snapshot(const TopicCounters & counters) noexcept
  {
    return TopicMetricsSnapshot{counters.received.load(std::memory_order_acquire),
      counters.received_bytes.load(std::memory_order_acquire),
      counters.admitted.load(std::memory_order_acquire),
      counters.admitted_bytes.load(std::memory_order_acquire),
      counters.committed.load(std::memory_order_acquire),
      counters.committed_bytes.load(std::memory_order_acquire),
      counters.dropped.load(std::memory_order_acquire),
      counters.dropped_bytes.load(std::memory_order_acquire)};
  }

  const uint32_t max_topic_id_;
  std::unique_ptr<TopicCounters[]> topics_;
  std::unique_ptr<DropCounters[]> drops_;
  TopicCounters total_{};
  std::atomic<uint64_t> durable_{0};
  std::atomic<uint64_t> storage_errors_{0};
  std::atomic<uint64_t> clock_anomalies_{0};
  std::atomic<uint64_t> peak_queue_depth_{0};
  std::atomic<uint64_t> peak_queue_capacity_{0};
};

}  // namespace blackbox_capture
