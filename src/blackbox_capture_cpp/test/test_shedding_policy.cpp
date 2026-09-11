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

#include <cstddef>
#include <cstdint>

#include "blackbox_capture/shedding_policy.hpp"

namespace blackbox_capture
{
namespace
{

TEST(SheddingPolicyTest, WatermarksAreOrderedByPriority)
{
  const ShedWatermarks watermarks = make_shed_watermarks(100U, 0.8);

  EXPECT_EQ(watermarks[3], 80U);
  EXPECT_EQ(watermarks[2], 87U);
  EXPECT_EQ(watermarks[1], 94U);
  // Tier 0 cannot be reached by any depth, so it never sheds by policy.
  EXPECT_EQ(watermarks[0], 101U);
  EXPECT_GT(watermarks[0], watermarks[1]);
  EXPECT_GT(watermarks[1], watermarks[2]);
  EXPECT_GT(watermarks[2], watermarks[3]);
}

TEST(SheddingPolicyTest, EachTierShedsOnlyAtItsOwnWatermark)
{
  const ShedWatermarks watermarks = make_shed_watermarks(100U, 0.8);

  for (uint8_t tier = 0U; tier < kShedTierCount; ++tier) {
    const std::size_t mark = watermarks[static_cast<std::size_t>(tier)];
    if (mark > 100U) {
      // Tier 0: no reachable depth sheds it.
      EXPECT_FALSE(should_shed(watermarks, tier, 100U));
      continue;
    }
    EXPECT_FALSE(should_shed(watermarks, tier, mark - 1U));
    EXPECT_TRUE(should_shed(watermarks, tier, mark));
  }
}

TEST(SheddingPolicyTest, HigherPriorityTiersSurviveLowerTierWatermarks)
{
  const ShedWatermarks watermarks = make_shed_watermarks(100U, 0.8);

  // At the configured high watermark only the least important tier is shed.
  EXPECT_TRUE(should_shed(watermarks, 3U, 80U));
  EXPECT_FALSE(should_shed(watermarks, 2U, 80U));
  EXPECT_FALSE(should_shed(watermarks, 1U, 80U));
  EXPECT_FALSE(should_shed(watermarks, kCriticalShedTier, 80U));

  // Utilization keeps climbing: tier 2 goes, tier 1 and tier 0 stay.
  EXPECT_TRUE(should_shed(watermarks, 3U, 87U));
  EXPECT_TRUE(should_shed(watermarks, 2U, 87U));
  EXPECT_FALSE(should_shed(watermarks, 1U, 87U));
  EXPECT_FALSE(should_shed(watermarks, kCriticalShedTier, 87U));

  // Only the critical tier is left just below the ring-full boundary.
  EXPECT_TRUE(should_shed(watermarks, 1U, 94U));
  EXPECT_FALSE(should_shed(watermarks, kCriticalShedTier, 94U));
  EXPECT_FALSE(should_shed(watermarks, kCriticalShedTier, 100U));
}

TEST(SheddingPolicyTest, UnknownTiersAreTreatedAsLeastImportant)
{
  const ShedWatermarks watermarks = make_shed_watermarks(100U, 0.8);

  EXPECT_TRUE(should_shed(watermarks, 200U, 80U));
  EXPECT_FALSE(should_shed(watermarks, 200U, 79U));
}

TEST(SheddingPolicyTest, TinyRingsKeepAtLeastOneAdmissibleDepth)
{
  const ShedWatermarks watermarks = make_shed_watermarks(1U, 0.8);

  EXPECT_EQ(watermarks[3], 1U);
  EXPECT_FALSE(should_shed(watermarks, kDefaultShedTier, 0U));
  EXPECT_TRUE(should_shed(watermarks, kDefaultShedTier, 1U));
  EXPECT_FALSE(should_shed(watermarks, kCriticalShedTier, 1U));
}

TEST(SheddingPolicyTest, ShippedWatermarkRatioSpacesTheDefaultRing)
{
  // The shipped defaults: buffer.event_capacity 16384 with control_reserve 256,
  // so the data capacity is 16128, and buffer.high_watermark_ratio 0.8.
  const ShedWatermarks watermarks = make_shed_watermarks(16128U, 0.8);

  EXPECT_EQ(watermarks[3], 12903U);
  EXPECT_EQ(watermarks[2], 13978U);
  EXPECT_EQ(watermarks[1], 15053U);
  EXPECT_EQ(watermarks[0], 16129U);
}

}  // namespace
}  // namespace blackbox_capture
