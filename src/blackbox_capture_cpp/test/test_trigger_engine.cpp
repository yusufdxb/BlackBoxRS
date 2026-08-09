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

#include <array>

#include "blackbox_capture/trigger_engine.hpp"

namespace blackbox_capture
{
namespace
{

TEST(TriggerEngineTest, RejectsInvalidConfigurationAndTopicId) {
  TriggerEngine engine(2U);
  TopicTriggerConfig invalid{};
  invalid.rate_enabled = true;
  invalid.expected_rate_hz = 0.0F;
  EXPECT_FALSE(engine.configure_topic(1U, invalid));
  EXPECT_FALSE(engine.configure_topic(0U, TopicTriggerConfig{}));
  EXPECT_FALSE(engine.configure_topic(3U, TopicTriggerConfig{}));
}

TEST(TriggerEngineTest, HealthyHeartbeatDoesNotTrigger) {
  TriggerEngine engine(1U);
  TopicTriggerConfig config{};
  config.heartbeat_enabled = true;
  config.dead_topic_ns = 100U;
  ASSERT_TRUE(engine.configure_topic(1U, config, 10U));
  engine.observe_message(1U, 90U);
  std::array<TriggerEvent, 2> output{};
  EXPECT_EQ(engine.evaluate(189U, output.data(), output.size()), 0U);
}

TEST(TriggerEngineTest, DeadTopicTriggersAtBoundaryOnceAndRearms) {
  TriggerEngine engine(1U);
  TopicTriggerConfig config{};
  config.heartbeat_enabled = true;
  config.dead_topic_ns = 100U;
  ASSERT_TRUE(engine.configure_topic(1U, config, 10U));
  std::array<TriggerEvent, 2> output{};
  EXPECT_EQ(engine.evaluate(109U, output.data(), output.size()), 0U);
  ASSERT_EQ(engine.evaluate(110U, output.data(), output.size()), 1U);
  EXPECT_EQ(output[0].code, TriggerCode::kDeadTopic);
  EXPECT_EQ(output[0].first_seen_ns, 110U);
  EXPECT_EQ(engine.evaluate(200U, output.data(), output.size()), 0U);

  engine.observe_message(1U, 210U);
  ASSERT_EQ(engine.evaluate(310U, output.data(), output.size()), 1U);
  EXPECT_EQ(output[0].first_seen_ns, 310U);
}

TEST(TriggerEngineTest, DeconfigureDisarmsHeartbeat) {
  TriggerEngine engine(1U);
  TopicTriggerConfig config{};
  config.heartbeat_enabled = true;
  config.dead_topic_ns = 100U;
  ASSERT_TRUE(engine.configure_topic(1U, config, 10U));
  ASSERT_TRUE(engine.deconfigure_topic(1U));
  std::array<TriggerEvent, 1> output{};
  EXPECT_EQ(engine.evaluate(1000U, output.data(), output.size()), 0U);
  EXPECT_FALSE(engine.deconfigure_topic(0U));
}

TEST(TriggerEngineTest, RateLowUsesHysteresisBeforeRetriggering) {
  TriggerEngine engine(1U);
  TopicTriggerConfig config{};
  config.rate_enabled = true;
  config.expected_rate_hz = 10.0F;
  config.low_rate_fraction = 0.5F;
  config.high_rate_fraction = 2.0F;
  config.hysteresis_fraction = 0.1F;
  config.rate_window_ns = 1'000'000'000ULL;
  ASSERT_TRUE(engine.configure_topic(1U, config));
  engine.observe_message(1U, 0U);
  engine.observe_message(1U, 100U);
  engine.observe_message(1U, 200U);
  engine.observe_message(1U, 300U);

  std::array<TriggerEvent, 2> output{};
  ASSERT_EQ(engine.evaluate(1'000'000'000ULL, output.data(), output.size()), 1U);
  EXPECT_EQ(output[0].code, TriggerCode::kRateLow);
  EXPECT_FLOAT_EQ(output[0].value, 4.0F);

  for (uint64_t index = 0U; index < 5U; ++index) {
    engine.observe_message(1U, 1'000'000'000ULL + index);
  }
  EXPECT_EQ(engine.evaluate(2'000'000'000ULL, output.data(), output.size()), 0U);
  for (uint64_t index = 0U; index < 4U; ++index) {
    engine.observe_message(1U, 2'000'000'000ULL + index);
  }
  EXPECT_EQ(engine.evaluate(3'000'000'000ULL, output.data(), output.size()), 0U);
  engine.observe_message(1U, 3'000'000'001ULL);
  for (uint64_t index = 0U; index < 5U; ++index) {
    engine.observe_message(1U, 3'000'000'100ULL + index);
  }
  EXPECT_EQ(engine.evaluate(4'000'000'000ULL, output.data(), output.size()), 0U);
  for (uint64_t index = 0U; index < 4U; ++index) {
    engine.observe_message(1U, 4'000'000'000ULL + index);
  }
  EXPECT_EQ(engine.evaluate(5'000'000'000ULL, output.data(), output.size()), 1U);
}

TEST(TriggerEngineTest, BurstProducesHighRateTrigger) {
  TriggerEngine engine(1U);
  TopicTriggerConfig config{};
  config.rate_enabled = true;
  config.expected_rate_hz = 10.0F;
  config.low_rate_fraction = 0.5F;
  config.high_rate_fraction = 2.0F;
  config.rate_window_ns = 1'000'000'000ULL;
  ASSERT_TRUE(engine.configure_topic(1U, config));
  for (uint64_t index = 0U; index < 21U; ++index) {
    engine.observe_message(1U, index);
  }
  TriggerEvent output{};
  ASSERT_EQ(engine.evaluate(1'000'000'000ULL, &output, 1U), 1U);
  EXPECT_EQ(output.code, TriggerCode::kRateHigh);
}

TEST(TriggerEngineTest, ThresholdTriggerHasDeterministicHysteresis) {
  TriggerEngine engine(1U);
  TriggerEvent output{};
  EXPECT_FALSE(
    engine.evaluate_threshold(
      TriggerCode::kQueueHighWatermark, Severity::kWarning,
      0U, 10U, 0.79F, 0.8F, 0.6F, output));
  EXPECT_TRUE(
    engine.evaluate_threshold(
      TriggerCode::kQueueHighWatermark, Severity::kWarning,
      0U, 11U, 0.8F, 0.8F, 0.6F, output));
  EXPECT_EQ(output.first_seen_ns, 11U);
  EXPECT_FALSE(
    engine.evaluate_threshold(
      TriggerCode::kQueueHighWatermark, Severity::kWarning,
      0U, 12U, 0.9F, 0.8F, 0.6F, output));
  EXPECT_FALSE(
    engine.evaluate_threshold(
      TriggerCode::kQueueHighWatermark, Severity::kWarning,
      0U, 13U, 0.6F, 0.8F, 0.6F, output));
  EXPECT_TRUE(
    engine.evaluate_threshold(
      TriggerCode::kQueueHighWatermark, Severity::kWarning,
      0U, 14U, 0.8F, 0.8F, 0.6F, output));
}

}  // namespace
}  // namespace blackbox_capture
