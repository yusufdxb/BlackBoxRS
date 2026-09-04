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
#include <cstddef>
#include <cstdint>
#include <vector>

#include "blackbox_capture/event.hpp"

namespace blackbox_capture
{

struct TopicTriggerConfig
{
  bool heartbeat_enabled{false};
  uint64_t dead_topic_ns{0};
  bool rate_enabled{false};
  float expected_rate_hz{0.0F};
  float low_rate_fraction{0.5F};
  float high_rate_fraction{2.0F};
  float hysteresis_fraction{0.1F};
  uint64_t rate_window_ns{1'000'000'000ULL};
  Severity severity{Severity::kWarning};
};

class TriggerEngine
{
public:
  explicit TriggerEngine(std::size_t max_topics);

  [[nodiscard]] bool configure_topic(
    uint32_t topic_id, const TopicTriggerConfig & config,
    uint64_t configured_at_ns = 0U) noexcept;
  [[nodiscard]] bool deconfigure_topic(uint32_t topic_id) noexcept;
  void observe_message(uint32_t topic_id, uint64_t monotonic_ns) noexcept;

  [[nodiscard]] std::size_t evaluate(
    uint64_t monotonic_ns, TriggerEvent * output,
    std::size_t output_capacity) noexcept;

  [[nodiscard]] bool evaluate_threshold(
    TriggerCode code, Severity severity, uint32_t topic_id,
    uint64_t monotonic_ns, float value, float trigger_threshold,
    float clear_threshold, TriggerEvent & output) noexcept;

  [[nodiscard]] std::size_t max_topics() const noexcept {return states_.size() - 1U;}
  [[nodiscard]] std::size_t memory_bytes() const noexcept
  {
    return sizeof(*this) + states_.capacity() * sizeof(TopicState);
  }

private:
  struct TopicState
  {
    TopicTriggerConfig config{};
    uint64_t configured_at_ns{0};
    uint64_t last_message_ns{0};
    uint64_t window_start_ns{0};
    uint64_t window_messages{0};
    uint64_t dead_first_seen_ns{0};
    uint64_t low_first_seen_ns{0};
    uint64_t high_first_seen_ns{0};
    TriggerEvent pending_rate_trigger{};
    bool configured{false};
    bool seen_message{false};
    bool dead_active{false};
    bool low_active{false};
    bool high_active{false};
    bool pending_rate_trigger_valid{false};
  };

  struct ThresholdState
  {
    uint64_t first_seen_ns{0};
    bool active{false};
  };

  static constexpr std::size_t kThresholdStateCount = 11U;
  static bool valid_config(const TopicTriggerConfig & config) noexcept;
  static std::size_t threshold_index(TriggerCode code) noexcept;

  std::vector<TopicState> states_;
  std::array<ThresholdState, kThresholdStateCount> thresholds_{};
};

}  // namespace blackbox_capture
