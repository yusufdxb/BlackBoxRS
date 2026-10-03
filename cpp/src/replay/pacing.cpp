#include "blackboxrs/replay/pacing.hpp"

#include <algorithm>
#include <stdexcept>
#include <thread>
#include <utility>

namespace blackboxrs::replay {

DeadlinePacer::DeadlinePacer(double speed)
    : DeadlinePacer(
          speed, [] { return Clock::now(); },
          [](Clock::time_point t) { std::this_thread::sleep_until(t); }) {}

DeadlinePacer::DeadlinePacer(double speed, Now now, SleepUntil sleep_until)
    : speed_(speed), now_(std::move(now)), sleep_until_(std::move(sleep_until)) {
  if (!(speed > 0)) {
    throw std::invalid_argument("speed must be > 0");
  }
}

DeadlinePacer::Clock::duration DeadlinePacer::scaled(Nanos virtual_elapsed) const {
  // Capped far beyond any replay (about 31 years of wall time), so a tiny
  // speed cannot overflow the time point.
  const double ns = std::min(static_cast<double>(virtual_elapsed.count()) / speed_, 1e18);
  return std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double, std::nano>(ns));
}

void DeadlinePacer::pace(Nanos step) {
  if (!origin_) {
    origin_ = now_();
  }
  if (step.count() <= 0) {
    return;
  }
  elapsed_ += step;
  const Clock::time_point target = *origin_ + scaled(elapsed_);
  if (now_() < target) {
    sleep_until_(target);
  }
}

void DeadlinePacer::rebase() {
  origin_ = now_();
  elapsed_ = Nanos{0};
}

std::optional<DeadlinePacer::Clock::time_point> DeadlinePacer::deadline() const {
  if (!origin_) {
    return std::nullopt;
  }
  return *origin_ + scaled(elapsed_);
}

}  // namespace blackboxrs::replay
