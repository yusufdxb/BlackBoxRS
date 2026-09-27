#include <gtest/gtest.h>

#include <type_traits>

#include "blackboxrs/time.hpp"

namespace blackboxrs {
namespace {

template <class A, class B>
concept Subtractable = requires(A a, B b) {
  a - b;
};
template <class A, class B>
concept Comparable = requires(A a, B b) {
  a < b;
};
template <class C>
concept HasNow = requires {
  C::now();
};

// Mixing clock domains must not compile.
static_assert(Subtractable<MonoTime, MonoTime>);
static_assert(!Subtractable<MonoTime, WallTime>);
static_assert(!Subtractable<ReplayTime, MonoTime>);
static_assert(!Subtractable<SourceTime, WallTime>);
static_assert(!Comparable<WallTime, SourceTime>);
static_assert(!Comparable<ReplayTime, MonoTime>);
// Replay code cannot read a clock: the replay domain has no now().
static_assert(HasNow<clock_domain::Mono>);
static_assert(!HasNow<clock_domain::Replay>);
static_assert(!HasNow<clock_domain::Source>);
static_assert(std::is_same_v<decltype(MonoTime{} - MonoTime{}), Nanos>);

TEST(Time, ReplayConversionRoundTrips) {
  const MonoTime t0 = mono_ns(5'000'000'000'000);
  const MonoTime t = mono_ns(5'000'001'371'498);
  const ReplayTime r = to_replay(t, t0);
  EXPECT_EQ(count_ns(r), 1'371'498);
  EXPECT_EQ(to_mono(r, t0), t);
}

TEST(Time, SecondsRoundHalfToEvenLikePython) {
  EXPECT_EQ(seconds_to_ns(4.0), 4'000'000'000);
  EXPECT_EQ(seconds_to_ns(0.0002), 200'000);
  EXPECT_EQ(seconds_to_ns(1.5), 1'500'000'000);
  // 2.5e-9 s is exactly representable as 2.5 ns only approximately; the
  // product decides, as in Python: int(round(2.5e-9 * 1e9)) == 2.
  EXPECT_EQ(seconds_to_ns(2.5e-9), 2);
  EXPECT_EQ(seconds_to_ns(-0.02), -20'000'000);
  EXPECT_THROW((void)seconds_to_ns(1e300), std::out_of_range);
}

TEST(Time, MonotonicNowNeverGoesBackwards) {
  MonoTime prev = clock_domain::Mono::now();
  for (int i = 0; i < 1000; ++i) {
    const MonoTime now = clock_domain::Mono::now();
    ASSERT_GE(now, prev);
    prev = now;
  }
}

}  // namespace
}  // namespace blackboxrs
