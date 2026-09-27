// Clock domains as distinct types.
//
// BlackBoxRS handles five clocks that must never be mixed silently:
//
//   MonoTime    recorder CLOCK_MONOTONIC. Never jumps. Orders records and
//               measures every receipt-domain interval.
//   WallTime    recorder CLOCK_REALTIME at the same instant. Used to detect
//               wall-clock steps and to line bundles up with other logs.
//   RosTime     the recorder node's ROS clock (sim time under use_sim_time).
//   SourceTime  DDS source timestamp: the publisher host's wall clock when it
//               wrote the sample. Another host, another clock.
//   ReplayTime  nanoseconds from the start of the replayed evidence, on the
//               recorder's receipt (monotonic) clock.
//
// Each domain is a std::chrono::time_point over its own tag clock, so
// `ReplayTime - MonoTime` or `WallTime < SourceTime` does not compile.
// Differences inside one domain are ordinary std::chrono::nanoseconds.
//
// Only MonoClock and WallClock have now(). ReplayClock deliberately has none:
// replay logic cannot read a clock, it is handed the time.
#pragma once

#include <chrono>
#include <cstdint>
#include <optional>

namespace blackboxrs {

using Nanos = std::chrono::nanoseconds;

namespace clock_domain {

template <bool Steady>
struct TagClock {
  using rep = Nanos::rep;
  using period = Nanos::period;
  using duration = Nanos;
  static constexpr bool is_steady = Steady;
};

struct Mono : TagClock<true> {
  using time_point = std::chrono::time_point<Mono, Nanos>;
  // Recorder monotonic time. steady_clock is CLOCK_MONOTONIC on Linux.
  static time_point now() noexcept {
    return time_point{
        std::chrono::duration_cast<Nanos>(std::chrono::steady_clock::now().time_since_epoch())};
  }
};

struct Wall : TagClock<false> {
  using time_point = std::chrono::time_point<Wall, Nanos>;
  static time_point now() noexcept {
    return time_point{
        std::chrono::duration_cast<Nanos>(std::chrono::system_clock::now().time_since_epoch())};
  }
};

struct Ros : TagClock<false> {
  using time_point = std::chrono::time_point<Ros, Nanos>;
};

struct Source : TagClock<false> {
  using time_point = std::chrono::time_point<Source, Nanos>;
};

struct Replay : TagClock<true> {
  using time_point = std::chrono::time_point<Replay, Nanos>;
};

}  // namespace clock_domain

using MonoTime = clock_domain::Mono::time_point;
using WallTime = clock_domain::Wall::time_point;
using RosTime = clock_domain::Ros::time_point;
using SourceTime = clock_domain::Source::time_point;
using ReplayTime = clock_domain::Replay::time_point;

// Raw nanosecond constructors, for decoding stored integers.
constexpr MonoTime mono_ns(std::int64_t ns) noexcept {
  return MonoTime{Nanos{ns}};
}
constexpr WallTime wall_ns(std::int64_t ns) noexcept {
  return WallTime{Nanos{ns}};
}
constexpr RosTime ros_ns(std::int64_t ns) noexcept {
  return RosTime{Nanos{ns}};
}
constexpr SourceTime source_ns(std::int64_t ns) noexcept {
  return SourceTime{Nanos{ns}};
}
constexpr ReplayTime replay_ns(std::int64_t ns) noexcept {
  return ReplayTime{Nanos{ns}};
}

template <class TimePoint>
constexpr std::int64_t count_ns(TimePoint t) noexcept {
  return t.time_since_epoch().count();
}

// Recorder monotonic time -> replay time, given the evidence start.
constexpr ReplayTime to_replay(MonoTime t, MonoTime evidence_start) noexcept {
  return ReplayTime{t - evidence_start};
}
constexpr MonoTime to_mono(ReplayTime t, MonoTime evidence_start) noexcept {
  return evidence_start + t.time_since_epoch();
}

// Seconds (as used in case files and the CLI) -> nanoseconds, rounded to the
// nearest integer exactly as Python's int(round(s * 1e9)) does for the finite
// values we accept (round-half-to-even).
std::int64_t seconds_to_ns(double seconds);

constexpr double ns_to_seconds(std::int64_t ns) noexcept {
  return static_cast<double>(ns) / 1e9;
}

inline constexpr std::int64_t kNsPerSecond = 1'000'000'000;

}  // namespace blackboxrs
