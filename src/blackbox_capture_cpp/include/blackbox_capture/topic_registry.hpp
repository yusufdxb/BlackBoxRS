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

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace blackbox_capture
{

enum class TopicRegistrationCode
{
  kSuccess = 0,
  kInvalidArgument,
  kEntryCapacityExceeded,
  kStringCapacityExceeded,
};

struct TopicRegistration
{
  TopicRegistrationCode code{TopicRegistrationCode::kInvalidArgument};
  uint32_t topic_id{0};
  bool created{false};
  bool type_changed{false};

  [[nodiscard]] bool ok() const noexcept {return code == TopicRegistrationCode::kSuccess;}
};

struct TopicView
{
  uint32_t topic_id{0};
  std::string_view topic{};
  std::string_view type{};
  std::string_view serialization_format{};
};

class TopicRegistry
{
public:
  TopicRegistry(std::size_t entry_capacity, std::size_t string_capacity)
  : entries_(entry_capacity), strings_(string_capacity)
  {
    if (entry_capacity == 0U || string_capacity == 0U) {
      throw std::invalid_argument("topic registry capacities must be greater than zero");
    }
    if (entry_capacity > static_cast<std::size_t>(UINT32_MAX - 1U)) {
      throw std::invalid_argument("topic registry capacity exceeds topic ID range");
    }
    if (string_capacity > static_cast<std::size_t>(UINT32_MAX)) {
      throw std::invalid_argument("topic registry string capacity exceeds offset range");
    }
  }

  [[nodiscard]] TopicRegistration register_topic(
    std::string_view topic, std::string_view type,
    std::string_view serialization_format) noexcept
  {
    if (topic.empty() || type.empty() || serialization_format.empty()) {
      return {TopicRegistrationCode::kInvalidArgument, 0U, false, false};
    }

    bool same_name_different_type = false;
    for (std::size_t index = 0; index < size_; ++index) {
      const TopicView view = make_view(entries_[index]);
      if (view.topic == topic) {
        if (view.type == type && view.serialization_format == serialization_format) {
          return {TopicRegistrationCode::kSuccess, view.topic_id, false, false};
        }
        same_name_different_type = true;
      }
    }

    if (size_ == entries_.size()) {
      return {TopicRegistrationCode::kEntryCapacityExceeded, 0U, false,
        same_name_different_type};
    }
    if (topic.size() > UINT32_MAX || type.size() > UINT32_MAX ||
      serialization_format.size() > UINT32_MAX ||
      type.size() > std::numeric_limits<std::size_t>::max() - topic.size() ||
      serialization_format.size() >
      std::numeric_limits<std::size_t>::max() - topic.size() - type.size())
    {
      return {TopicRegistrationCode::kStringCapacityExceeded, 0U, false,
        same_name_different_type};
    }
    const std::size_t needed = topic.size() + type.size() + serialization_format.size();
    if (needed > strings_.size() - string_size_) {
      return {TopicRegistrationCode::kStringCapacityExceeded, 0U, false,
        same_name_different_type};
    }

    Entry & entry = entries_[size_];
    entry.topic_id = static_cast<uint32_t>(size_ + 1U);
    entry.topic_offset = copy_string(topic);
    entry.topic_size = static_cast<uint32_t>(topic.size());
    entry.type_offset = copy_string(type);
    entry.type_size = static_cast<uint32_t>(type.size());
    entry.serialization_offset = copy_string(serialization_format);
    entry.serialization_size = static_cast<uint32_t>(serialization_format.size());
    ++size_;
    return {TopicRegistrationCode::kSuccess, entry.topic_id, true, same_name_different_type};
  }

  [[nodiscard]] std::optional<TopicView> by_id(uint32_t topic_id) const noexcept
  {
    if (topic_id == 0U || topic_id > size_) {
      return std::nullopt;
    }
    return make_view(entries_[topic_id - 1U]);
  }

  [[nodiscard]] std::optional<TopicView> find_exact(
    std::string_view topic, std::string_view type,
    std::string_view serialization_format) const noexcept
  {
    for (std::size_t index = 0; index < size_; ++index) {
      const TopicView view = make_view(entries_[index]);
      if (view.topic == topic && view.type == type &&
        view.serialization_format == serialization_format)
      {
        return view;
      }
    }
    return std::nullopt;
  }

  [[nodiscard]] std::optional<TopicView> find_topic(std::string_view topic) const noexcept
  {
    for (std::size_t index = size_; index > 0U; --index) {
      const TopicView view = make_view(entries_[index - 1U]);
      if (view.topic == topic) {
        return view;
      }
    }
    return std::nullopt;
  }

  [[nodiscard]] std::size_t size() const noexcept {return size_;}
  [[nodiscard]] std::size_t capacity() const noexcept {return entries_.size();}
  [[nodiscard]] std::size_t string_bytes_used() const noexcept {return string_size_;}
  [[nodiscard]] std::size_t string_capacity() const noexcept {return strings_.size();}
  [[nodiscard]] std::size_t memory_bytes() const noexcept
  {
    return sizeof(*this) + entries_.capacity() * sizeof(Entry) + strings_.capacity();
  }

private:
  struct Entry
  {
    uint32_t topic_id{0};
    uint32_t topic_offset{0};
    uint32_t topic_size{0};
    uint32_t type_offset{0};
    uint32_t type_size{0};
    uint32_t serialization_offset{0};
    uint32_t serialization_size{0};
  };

  uint32_t copy_string(std::string_view value) noexcept
  {
    const auto offset = static_cast<uint32_t>(string_size_);
    std::memcpy(strings_.data() + string_size_, value.data(), value.size());
    string_size_ += value.size();
    return offset;
  }

  [[nodiscard]] std::string_view string_at(uint32_t offset, uint32_t size) const noexcept
  {
    return {strings_.data() + offset, size};
  }

  [[nodiscard]] TopicView make_view(const Entry & entry) const noexcept
  {
    return TopicView{entry.topic_id,
      string_at(entry.topic_offset, entry.topic_size),
      string_at(entry.type_offset, entry.type_size),
      string_at(entry.serialization_offset, entry.serialization_size)};
  }

  std::vector<Entry> entries_;
  std::vector<char> strings_;
  std::size_t size_{0};
  std::size_t string_size_{0};
};

}  // namespace blackbox_capture
