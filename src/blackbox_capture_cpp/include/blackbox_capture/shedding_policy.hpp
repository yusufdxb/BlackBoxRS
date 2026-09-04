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

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace blackbox_capture
{

// Graduated shedding. A topic carries one priority tier: 0 is the most
// important and is never shed by policy, and each higher tier is shed earlier.
// Tier 0 traffic survives to the ring-full boundary, where it is dropped as
// kRingFull rather than kLowPriorityShed, so robot control and state evidence
// is the last thing lost under backpressure.
constexpr uint8_t kShedTierCount = 4U;
constexpr uint8_t kCriticalShedTier = 0U;
constexpr uint8_t kDefaultShedTier = kShedTierCount - 1U;

// Queue depth, in descriptors, at which each tier starts to shed. Index is the
// tier. Computed once at startup so the hot path only compares two integers.
using ShedWatermarks = std::array<std::size_t, kShedTierCount>;

/// Spread the tier watermarks between the configured high watermark and the
/// data capacity of the ring. The least important tier sheds at the configured
/// watermark, tier 0 never sheds, and the tiers in between are spaced evenly,
/// so utilization has to keep rising before more valuable topics are given up.
inline ShedWatermarks make_shed_watermarks(
  std::size_t data_capacity,
  double high_watermark_ratio) noexcept
{
  ShedWatermarks watermarks{};
  const double capacity = static_cast<double>(data_capacity);
  double base = std::ceil(high_watermark_ratio * capacity);
  if (!(base >= 1.0)) {
    base = 1.0;
  }
  if (base > capacity) {
    base = capacity;
  }
  const std::size_t base_mark = static_cast<std::size_t>(base);
  const double headroom = capacity - base;
  const double steps = static_cast<double>(kDefaultShedTier);
  // A depth can never exceed the data capacity, so this watermark is a
  // deliberate "never" rather than a threshold.
  watermarks[0] = data_capacity + 1U;
  for (uint8_t tier = 1U; tier <= kDefaultShedTier; ++tier) {
    const double remaining = static_cast<double>(kDefaultShedTier - tier);
    const double offset = std::ceil(headroom * remaining / steps);
    watermarks[static_cast<std::size_t>(tier)] =
      base_mark + static_cast<std::size_t>(offset);
  }
  return watermarks;
}

/// True when a message of this tier must be shed at the observed queue depth.
/// Unknown tiers are treated as the least important, which fails toward
/// shedding the traffic the deployment never claimed to care about.
inline bool should_shed(
  const ShedWatermarks & watermarks,
  uint8_t tier,
  std::size_t depth) noexcept
{
  const std::size_t index = tier < kShedTierCount ?
    static_cast<std::size_t>(tier) :
    static_cast<std::size_t>(kDefaultShedTier);
  return depth >= watermarks[index];
}

}  // namespace blackbox_capture
