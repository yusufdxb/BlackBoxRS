// A bounded multi-producer / single-consumer queue.
//
// Deliberately simple: one mutex and one condition variable. Producers never
// block (try_push returns Full instead) because the producers are ROS
// executor callbacks, which must not stall on the recorder. The consumer pops
// in batches, so it takes the lock once per batch rather than once per item.
// Whether a lock-free ring would pay for itself is a benchmark question
// (blackboxrs benchmark, "queue" section), not a design default.
//
// Capacity is split into two lanes. Data items may use `capacity - reserve`
// slots; control items (graph snapshots, markers, samples) may use all of
// them, so a data overload cannot starve the recorder's own bookkeeping.
//
// Storage is a ring of `capacity` slots allocated once at construction, so
// the queue itself never allocates after startup.
//
// Every rejected push is counted per lane. Nothing is dropped silently: the
// caller learns about each rejection from the return value and the counters
// are part of the recorder metrics.
#pragma once

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <stop_token>
#include <vector>

namespace blackboxrs::recorder {

enum class Lane : std::uint8_t { data, control };
enum class PushResult : std::uint8_t { ok, full, closed };

struct QueueStats {
  std::size_t capacity = 0;
  std::size_t control_reserve = 0;
  std::size_t depth = 0;
  std::size_t high_water = 0;
  std::uint64_t pushed = 0;
  std::uint64_t popped = 0;
  std::uint64_t rejected_full_data = 0;
  std::uint64_t rejected_full_control = 0;
  std::uint64_t rejected_closed = 0;
  bool closed = false;
};

template <class T>
class BoundedQueue {
 public:
  explicit BoundedQueue(std::size_t capacity, std::size_t control_reserve = 0)
      : capacity_(capacity), reserve_(control_reserve) {
    if (capacity == 0 || control_reserve >= capacity) {
      throw std::invalid_argument("queue capacity must be > 0 and larger than its control reserve");
    }
    slots_.resize(capacity);
  }
  BoundedQueue(const BoundedQueue&) = delete;
  BoundedQueue& operator=(const BoundedQueue&) = delete;
  BoundedQueue(BoundedQueue&&) = delete;
  BoundedQueue& operator=(BoundedQueue&&) = delete;
  ~BoundedQueue() = default;

  // Never blocks. On Full or Closed the item is not taken (`item` is left
  // intact) so the caller can account for it.
  [[nodiscard]] PushResult try_push(T& item, Lane lane = Lane::data) {
    {
      std::lock_guard lock(mu_);
      if (closed_) {
        ++stats_.rejected_closed;
        return PushResult::closed;
      }
      const std::size_t limit = lane == Lane::data ? capacity_ - reserve_ : capacity_;
      if (count_ >= limit) {
        ++(lane == Lane::data ? stats_.rejected_full_data : stats_.rejected_full_control);
        return PushResult::full;
      }
      put(std::move(item));
    }
    cv_.notify_one();
    return PushResult::ok;
  }

  // Blocks until space frees up, the queue closes, or `stop` is requested.
  // For internal hand-offs where waiting is the correct back-pressure.
  [[nodiscard]] PushResult push_wait(T& item, std::stop_token stop) {
    {
      std::unique_lock lock(mu_);
      space_cv_.wait(lock, stop, [&] { return closed_ || count_ < capacity_; });
      if (closed_ || count_ >= capacity_) {
        ++stats_.rejected_closed;
        return PushResult::closed;
      }
      put(std::move(item));
    }
    cv_.notify_one();
    return PushResult::ok;
  }

  // Like push_wait, but gives up at `deadline` (returns Full, item intact).
  // For hand-offs that must not hang when the consumer is stuck.
  [[nodiscard]] PushResult push_wait_until(T& item,
                                           std::chrono::steady_clock::time_point deadline) {
    {
      std::unique_lock lock(mu_);
      space_cv_.wait_until(lock, std::stop_token{}, deadline,
                           [&] { return closed_ || count_ < capacity_; });
      if (closed_) {
        ++stats_.rejected_closed;
        return PushResult::closed;
      }
      if (count_ >= capacity_) {
        ++stats_.rejected_full_control;
        return PushResult::full;
      }
      put(std::move(item));
    }
    cv_.notify_one();
    return PushResult::ok;
  }

  // Move up to `max` items into `out`. Waits until at least one item is
  // available, the deadline passes, the queue is closed, or `stop` is
  // requested. Returns the number of items moved; 0 with closed() true and an
  // empty queue means the producer side is finished.
  std::size_t pop_batch(std::vector<T>& out, std::size_t max,
                        std::chrono::steady_clock::time_point deadline, std::stop_token stop) {
    std::size_t n = 0;
    {
      std::unique_lock lock(mu_);
      cv_.wait_until(lock, stop, deadline, [&] { return count_ != 0 || closed_; });
      n = std::min(max, count_);
      for (std::size_t i = 0; i < n; ++i) {
        out.push_back(take());
      }
      stats_.popped += n;
    }
    if (n != 0) {
      space_cv_.notify_all();
    }
    return n;
  }

  // Take everything left, without waiting (shutdown drain).
  std::size_t drain(std::vector<T>& out) {
    std::lock_guard lock(mu_);
    const std::size_t n = count_;
    while (count_ != 0) {
      out.push_back(take());
    }
    stats_.popped += n;
    space_cv_.notify_all();
    return n;
  }

  // No more pushes are accepted; the consumer still drains what is queued.
  void close() {
    {
      std::lock_guard lock(mu_);
      closed_ = true;
    }
    cv_.notify_all();
    space_cv_.notify_all();
  }

  [[nodiscard]] QueueStats stats() const {
    std::lock_guard lock(mu_);
    QueueStats s = stats_;
    s.capacity = capacity_;
    s.control_reserve = reserve_;
    s.depth = count_;
    s.closed = closed_;
    return s;
  }

  [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

 private:
  // Callers hold mu_.
  void put(T&& item) {
    slots_[(head_ + count_) % capacity_].emplace(std::move(item));
    ++count_;
    ++stats_.pushed;
    stats_.high_water = std::max(stats_.high_water, count_);
  }
  T take() {
    T item = std::move(*slots_[head_]);
    slots_[head_].reset();
    head_ = (head_ + 1) % capacity_;
    --count_;
    return item;
  }

  const std::size_t capacity_;
  const std::size_t reserve_;
  mutable std::mutex mu_;
  std::condition_variable_any cv_;        // consumer: items available
  std::condition_variable_any space_cv_;  // push_wait: space available
  std::vector<std::optional<T>> slots_;
  std::size_t head_ = 0;
  std::size_t count_ = 0;
  QueueStats stats_;
  bool closed_ = false;
};

}  // namespace blackboxrs::recorder
