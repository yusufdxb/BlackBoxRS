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

#include "blackbox_capture/topic_registry.hpp"

namespace blackbox_capture
{
namespace
{

TEST(TopicRegistryTest, DuplicateExactTopicKeepsStableId) {
  TopicRegistry registry(4U, 128U);
  const auto first = registry.register_topic("/imu/data", "sensor_msgs/msg/Imu", "cdr");
  const auto duplicate = registry.register_topic("/imu/data", "sensor_msgs/msg/Imu", "cdr");
  ASSERT_TRUE(first.ok());
  ASSERT_TRUE(duplicate.ok());
  EXPECT_TRUE(first.created);
  EXPECT_FALSE(duplicate.created);
  EXPECT_EQ(first.topic_id, duplicate.topic_id);
  EXPECT_EQ(registry.size(), 1U);
}

TEST(TopicRegistryTest, TypeChangeGetsNewNonRecycledId) {
  TopicRegistry registry(4U, 128U);
  const auto first = registry.register_topic("/state", "example/msg/Old", "cdr");
  const auto changed = registry.register_topic("/state", "example/msg/New", "cdr");
  ASSERT_TRUE(first.ok());
  ASSERT_TRUE(changed.ok());
  EXPECT_TRUE(changed.created);
  EXPECT_TRUE(changed.type_changed);
  EXPECT_GT(changed.topic_id, first.topic_id);
  ASSERT_TRUE(registry.find_topic("/state").has_value());
  EXPECT_EQ(registry.find_topic("/state")->type, "example/msg/New");
  EXPECT_EQ(registry.by_id(first.topic_id)->type, "example/msg/Old");
}

TEST(TopicRegistryTest, EntryAndStringExhaustionAreExplicitAndAtomic) {
  TopicRegistry entries(1U, 128U);
  ASSERT_TRUE(entries.register_topic("/a", "x/msg/A", "cdr").ok());
  const auto entry_full = entries.register_topic("/b", "x/msg/B", "cdr");
  EXPECT_EQ(entry_full.code, TopicRegistrationCode::kEntryCapacityExceeded);
  EXPECT_EQ(entries.size(), 1U);

  TopicRegistry strings(3U, 10U);
  const auto string_full = strings.register_topic("/abcd", "Type", "cdr");
  EXPECT_EQ(string_full.code, TopicRegistrationCode::kStringCapacityExceeded);
  EXPECT_EQ(strings.size(), 0U);
  EXPECT_EQ(strings.string_bytes_used(), 0U);
}

TEST(TopicRegistryTest, InvalidAndUnknownLookupsDoNotUseIdZero) {
  TopicRegistry registry(2U, 32U);
  EXPECT_FALSE(registry.register_topic("", "Type", "cdr").ok());
  EXPECT_FALSE(registry.by_id(0U).has_value());
  EXPECT_FALSE(registry.by_id(1U).has_value());
  EXPECT_FALSE(registry.find_topic("/missing").has_value());
}

}  // namespace
}  // namespace blackbox_capture
