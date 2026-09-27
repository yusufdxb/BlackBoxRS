// Messages lost before the recorder core, by arrival time.
//
// A bundle must say whether anything inside its window was lost, including
// messages dropped while its trigger was still waiting in the ingest queue
// and messages dropped in its pre-trigger window. A running total sampled
// when the pipeline handles the trigger gets both wrong, so losses are kept
// in 100 ms buckets keyed by the arrival time of what was lost, and a bundle
// asks for the total over its own window. Buckets older than ten minutes are
// discarded. Recording a loss is the exceptional path (a full queue, a
// shutdown, a DDS-reported loss), so a mutex is fine there.
#pragma once

#include <cstdint>
#include <deque>
#include <mutex>
#include <utility>

namespace blackboxrs::recorder {

class LossLedger {
 public:
  static constexpr std::int64_t kBucketNs = 100'000'000;
  static constexpr std::int64_t kKeepNs = 600'000'000'000;

  void record(std::int64_t t_mono_ns, std::uint64_t n = 1) {
    if (n == 0) {
      return;
    }
    const std::int64_t b = bucket(t_mono_ns);
    std::lock_guard lock(mu_);
    // Arrivals are nearly in order; search from the back.
    auto it = buckets_.end();
    while (it != buckets_.begin() && std::prev(it)->first > b) {
      --it;
    }
    if (it != buckets_.begin() && std::prev(it)->first == b) {
      std::prev(it)->second += n;
    } else {
      buckets_.insert(it, {b, n});
    }
    total_ += n;
    const std::int64_t oldest = b - kKeepNs / kBucketNs;
    while (!buckets_.empty() && buckets_.front().first < oldest) {
      buckets_.pop_front();
    }
  }

  // Losses whose arrival bucket overlaps [from, to]. Edge buckets count in
  // full, so the answer errs towards reporting a loss.
  [[nodiscard]] std::uint64_t between(std::int64_t from_ns, std::int64_t to_ns) const {
    const std::int64_t lo = bucket(from_ns);
    const std::int64_t hi = bucket(to_ns);
    std::lock_guard lock(mu_);
    std::uint64_t n = 0;
    for (const auto& [b, c] : buckets_) {
      if (b >= lo && b <= hi) {
        n += c;
      }
    }
    return n;
  }

  [[nodiscard]] std::uint64_t total() const {
    std::lock_guard lock(mu_);
    return total_;
  }

 private:
  static std::int64_t bucket(std::int64_t t) {
    return t >= 0 ? t / kBucketNs : (t - kBucketNs + 1) / kBucketNs;
  }
  mutable std::mutex mu_;
  std::deque<std::pair<std::int64_t, std::uint64_t>> buckets_;
  std::uint64_t total_ = 0;
};

}  // namespace blackboxrs::recorder
