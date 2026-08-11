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

#include "blackbox_capture/trigger_engine.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace blackbox_capture
{

TriggerEngine::TriggerEngine(std::size_t max_topics)
: states_(max_topics + 1U)
{
  if (max_topics == 0U || max_topics >= static_cast<std::size_t>(UINT32_MAX)) {
    throw std::invalid_argument("trigger topic capacity is outside the supported range");
  }
}

bool TriggerEngine::valid_config(const TopicTriggerConfig & config) noexcept
{
  if (config.heartbeat_enabled && config.dead_topic_ns == 0U) {
    return false;
  }
  if (config.rate_enabled &&
    (!(config.expected_rate_hz > 0.0F) || !(config.low_rate_fraction >= 0.0F) ||
    !(config.high_rate_fraction > config.low_rate_fraction) ||
    !(config.hysteresis_fraction >= 0.0F) || config.rate_window_ns == 0U ||
    !std::isfinite(config.expected_rate_hz) || !std::isfinite(config.low_rate_fraction) ||
    !std::isfinite(config.high_rate_fraction) ||
    !std::isfinite(config.hysteresis_fraction)))
  {
    return false;
  }
  return true;
}

bool TriggerEngine::configure_topic(
  uint32_t topic_id, const TopicTriggerConfig & config,
  uint64_t configured_at_ns) noexcept
{
  if (topic_id == 0U || topic_id >= states_.size() || !valid_config(config)) {
    return false;
  }
  TopicState replacement{};
  replacement.config = config;
  replacement.configured_at_ns = configured_at_ns;
  replacement.window_start_ns = configured_at_ns;
  replacement.configured = true;
  states_[topic_id] = replacement;
  return true;
}

bool TriggerEngine::deconfigure_topic(uint32_t topic_id) noexcept
{
  if (topic_id == 0U || topic_id >= states_.size()) {
    return false;
  }
  states_[topic_id] = TopicState{};
  return true;
}

void TriggerEngine::observe_message(uint32_t topic_id, uint64_t monotonic_ns) noexcept
{
  if (topic_id == 0U || topic_id >= states_.size()) {
    return;
  }
  TopicState & state = states_[topic_id];
  if (!state.configured) {
    return;
  }
  if (!state.seen_message) {
    state.window_start_ns = monotonic_ns;
  }
  state.seen_message = true;
  state.last_message_ns = monotonic_ns;
  ++state.window_messages;
  state.dead_active = false;
  state.dead_first_seen_ns = 0U;
}

std::size_t TriggerEngine::evaluate(
  uint64_t monotonic_ns, TriggerEvent * output,
  std::size_t output_capacity) noexcept
{
  if (output == nullptr || output_capacity == 0U) {
    return 0U;
  }

  std::size_t emitted = 0U;
  for (uint32_t topic_id = 1U; topic_id < states_.size(); ++topic_id) {
    TopicState & state = states_[topic_id];
    if (!state.configured || !state.pending_rate_trigger_valid) {
      continue;
    }
    if (emitted >= output_capacity) {
      return emitted;
    }
    output[emitted++] = state.pending_rate_trigger;
    state.pending_rate_trigger_valid = false;
  }

  for (uint32_t topic_id = 1U; topic_id < states_.size(); ++topic_id) {
    TopicState & state = states_[topic_id];
    if (!state.configured) {
      continue;
    }

    if (state.config.heartbeat_enabled) {
      const uint64_t anchor = state.seen_message ? state.last_message_ns : state.configured_at_ns;
      if (monotonic_ns >= anchor && monotonic_ns - anchor >= state.config.dead_topic_ns) {
        const uint64_t first_seen = anchor + state.config.dead_topic_ns;
        if (!state.dead_active && emitted < output_capacity) {
          output[emitted++] = TriggerEvent{TriggerCode::kDeadTopic,
            state.config.severity,
            topic_id,
            first_seen,
            monotonic_ns,
            static_cast<float>(monotonic_ns - anchor) / 1.0e9F,
            static_cast<float>(state.config.dead_topic_ns) / 1.0e9F};
          state.dead_active = true;
          state.dead_first_seen_ns = first_seen;
        }
      }
    }

    if (!state.config.rate_enabled || !state.seen_message ||
      monotonic_ns < state.window_start_ns ||
      monotonic_ns - state.window_start_ns < state.config.rate_window_ns)
    {
      continue;
    }

    const uint64_t elapsed_ns = monotonic_ns - state.window_start_ns;
    const float rate = elapsed_ns == 0U ?
      0.0F :
      static_cast<float>(state.window_messages) * 1.0e9F /
      static_cast<float>(elapsed_ns);
    const float low_threshold = state.config.expected_rate_hz * state.config.low_rate_fraction;
    const float high_threshold = state.config.expected_rate_hz * state.config.high_rate_fraction;
    const float hysteresis = state.config.expected_rate_hz * state.config.hysteresis_fraction;

    if (state.low_active && rate >= low_threshold + hysteresis) {
      state.low_active = false;
      state.low_first_seen_ns = 0U;
    }
    if (state.high_active && rate <= std::max(0.0F, high_threshold - hysteresis)) {
      state.high_active = false;
      state.high_first_seen_ns = 0U;
    }

    if (rate <= low_threshold && !state.low_active) {
      const TriggerEvent event{TriggerCode::kRateLow,
        state.config.severity,
        topic_id,
        state.window_start_ns,
        monotonic_ns,
        rate,
        low_threshold};
      if (emitted < output_capacity) {
        output[emitted++] = event;
      } else {
        state.pending_rate_trigger = event;
        state.pending_rate_trigger_valid = true;
      }
      state.low_active = true;
      state.low_first_seen_ns = state.window_start_ns;
    } else if (rate >= high_threshold && !state.high_active) {
      const TriggerEvent event{TriggerCode::kRateHigh,
        state.config.severity,
        topic_id,
        state.window_start_ns,
        monotonic_ns,
        rate,
        high_threshold};
      if (emitted < output_capacity) {
        output[emitted++] = event;
      } else {
        state.pending_rate_trigger = event;
        state.pending_rate_trigger_valid = true;
      }
      state.high_active = true;
      state.high_first_seen_ns = state.window_start_ns;
    }

    state.window_start_ns = monotonic_ns;
    state.window_messages = 0U;
  }
  return emitted;
}

std::size_t TriggerEngine::threshold_index(TriggerCode code) noexcept
{
  const std::size_t index = static_cast<std::size_t>(code);
  return index < kThresholdStateCount ? index : 0U;
}

bool TriggerEngine::evaluate_threshold(
  TriggerCode code, Severity severity, uint32_t topic_id,
  uint64_t monotonic_ns, float value,
  float trigger_threshold, float clear_threshold,
  TriggerEvent & output) noexcept
{
  const std::size_t index = threshold_index(code);
  if (index == 0U || !std::isfinite(value) || !std::isfinite(trigger_threshold) ||
    !std::isfinite(clear_threshold) || clear_threshold > trigger_threshold)
  {
    return false;
  }

  ThresholdState & state = thresholds_[index];
  if (state.active) {
    if (value <= clear_threshold) {
      state.active = false;
      state.first_seen_ns = 0U;
    }
    return false;
  }
  if (value < trigger_threshold) {
    return false;
  }

  state.active = true;
  state.first_seen_ns = monotonic_ns;
  output = TriggerEvent{code, severity, topic_id, monotonic_ns, monotonic_ns, value,
    trigger_threshold};
  return true;
}

}  // namespace blackbox_capture
