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
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <type_traits>

namespace blackbox_capture
{

enum class AdmissionClass : uint8_t { kData = 0, kControl = 1 };

template<typename T>
class SpscRingBuffer
{
public:
  explicit SpscRingBuffer(std::size_t capacity, std::size_t control_reserve = 0)
  : capacity_(capacity), control_reserve_(control_reserve), storage_(allocate(capacity))
  {
    if (capacity_ == 0U) {
      throw std::invalid_argument("ring capacity must be greater than zero");
    }
    if (control_reserve_ >= capacity_) {
      throw std::invalid_argument("control reserve must be smaller than ring capacity");
    }
  }

  SpscRingBuffer(const SpscRingBuffer &) = delete;
  SpscRingBuffer & operator=(const SpscRingBuffer &) = delete;
  SpscRingBuffer(SpscRingBuffer &&) = delete;
  SpscRingBuffer & operator=(SpscRingBuffer &&) = delete;

  [[nodiscard]] bool try_push(
    const T & item,
    AdmissionClass admission = AdmissionClass::kData) noexcept
  {
    const uint64_t head = head_.load(std::memory_order_relaxed);
    const uint64_t tail = tail_.load(std::memory_order_acquire);
    const uint64_t depth = head - tail;
    const uint64_t limit = admission == AdmissionClass::kControl ?
      static_cast<uint64_t>(capacity_) :
      static_cast<uint64_t>(data_capacity());
    if (depth >= limit) {
      if (admission == AdmissionClass::kControl) {
        rejected_control_.fetch_add(1U, std::memory_order_relaxed);
      } else {
        rejected_data_.fetch_add(1U, std::memory_order_relaxed);
      }
      return false;
    }

    storage_[head % capacity_] = item;
    head_.store(head + 1U, std::memory_order_release);
    update_high_watermark(depth + 1U);
    return true;
  }

  [[nodiscard]] bool try_pop(T & item) noexcept
  {
    const uint64_t tail = tail_.load(std::memory_order_relaxed);
    const uint64_t head = head_.load(std::memory_order_acquire);
    if (tail == head) {
      return false;
    }

    item = storage_[tail % capacity_];
    tail_.store(tail + 1U, std::memory_order_release);
    return true;
  }

  [[nodiscard]] bool empty() const noexcept {return size() == 0U;}

  [[nodiscard]] std::size_t size() const noexcept
  {
    const uint64_t tail = tail_.load(std::memory_order_acquire);
    const uint64_t head = head_.load(std::memory_order_acquire);
    return static_cast<std::size_t>(head - tail);
  }

  [[nodiscard]] constexpr std::size_t capacity() const noexcept {return capacity_;}
  [[nodiscard]] constexpr std::size_t control_reserve() const noexcept
  {
    return control_reserve_;
  }
  [[nodiscard]] constexpr std::size_t data_capacity() const noexcept
  {
    return capacity_ - control_reserve_;
  }
  [[nodiscard]] uint64_t rejected_data() const noexcept
  {
    return rejected_data_.load(std::memory_order_relaxed);
  }
  [[nodiscard]] uint64_t rejected_control() const noexcept
  {
    return rejected_control_.load(std::memory_order_relaxed);
  }
  [[nodiscard]] std::size_t high_watermark() const noexcept
  {
    return static_cast<std::size_t>(high_watermark_.load(std::memory_order_relaxed));
  }
  [[nodiscard]] std::size_t memory_bytes() const noexcept
  {
    return sizeof(*this) + capacity_ * sizeof(T);
  }

private:
  static std::unique_ptr<T[]> allocate(std::size_t capacity)
  {
    if (capacity == 0U) {
      return nullptr;
    }
    return std::unique_ptr<T[]>(new T[capacity]);
  }

  void update_high_watermark(uint64_t depth) noexcept
  {
    uint64_t current = high_watermark_.load(std::memory_order_relaxed);
    while (current < depth &&
      !high_watermark_.compare_exchange_weak(
        current, depth, std::memory_order_relaxed,
        std::memory_order_relaxed))
    {
    }
  }

  const std::size_t capacity_;
  const std::size_t control_reserve_;
  std::unique_ptr<T[]> storage_;
  alignas(64) std::atomic<uint64_t> head_{0};
  alignas(64) std::atomic<uint64_t> tail_{0};
  alignas(64) std::atomic<uint64_t> rejected_data_{0};
  std::atomic<uint64_t> rejected_control_{0};
  std::atomic<uint64_t> high_watermark_{0};
};

}  // namespace blackbox_capture
