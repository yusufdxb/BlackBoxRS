// Wall-clock pacing for a replay at `speed` times real time (--speed).
//
// A paced replay should take (virtual time) / speed of wall time, however
// long the replay spends on each step. The pacer keeps one schedule: a
// steady-clock origin, taken at the first step, plus the virtual time paced
// so far divided by the speed. Each step moves that deadline forward and the
// pacer sleeps until it when it is ahead. When it is behind (the replay or a
// previous wake-up ran late) it returns at once, and later deadlines are
// still measured from the origin, so processing time and sleep overshoot do
// not accumulate the way they do when every step sleeps its own delta. The
// deadline is derived from the cumulative virtual time, never from rounded
// per-step intervals.
//
// Like every Pacer it only waits: it never feeds back into the replay, so
// virtual time, event order and ticks are exactly those of an unpaced run.
#pragma once

#include <chrono>
#include <functional>
#include <optional>

#include "blackboxrs/time.hpp"

namespace blackboxrs::replay {

class DeadlinePacer {
 public:
  using Clock = std::chrono::steady_clock;
  using Now = std::function<Clock::time_point()>;
  using SleepUntil = std::function<void(Clock::time_point)>;

  // The steady clock and std::this_thread::sleep_until. Throws
  // std::invalid_argument unless speed > 0.
  explicit DeadlinePacer(double speed);
  // An injected clock and sleep (tests).
  DeadlinePacer(double speed, Now now, SleepUntil sleep_until);

  // Pace one virtual step (the ReplayClock's advance, >= 0).
  void pace(Nanos step);

  // Start the schedule again from the current time, with no virtual time
  // behind it. For an interactive pause (--step): the time spent waiting for
  // the user is not caught up afterwards.
  void rebase();

  // The wall-clock deadline of the virtual time paced so far; nullopt before
  // the first step.
  [[nodiscard]] std::optional<Clock::time_point> deadline() const;

 private:
  [[nodiscard]] Clock::duration scaled(Nanos virtual_elapsed) const;

  double speed_;
  Now now_;
  SleepUntil sleep_until_;
  std::optional<Clock::time_point> origin_;
  Nanos elapsed_{0};
};

}  // namespace blackboxrs::replay
