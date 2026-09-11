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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/byte_multi_array.hpp>

namespace
{

using ByteMessage = std_msgs::msg::ByteMultiArray;
using SteadyClock = std::chrono::steady_clock;

struct Options
{
  std::size_t topics{1};
  double aggregate_rate{100.0};
  bool aggregate_rate_set{false};
  double rate_per_topic{0.0};
  std::size_t payload_bytes{64};
  double duration_sec{60.0};
  double discovery_warmup_sec{1.0};
  std::string qos{"best_effort"};
  std::size_t qos_depth{10};
  std::string topic_prefix{"/blackbox_bench/topic"};
  std::string run_id{"local"};
  std::string result_json;
  double burst_every_sec{0.0};
  double burst_duration_sec{0.0};
  double burst_multiplier{1.0};
  double churn_every_sec{0.0};
  double churn_down_sec{0.0};
  std::size_t max_catch_up{4096};
};

std::string json_escape(const std::string & value)
{
  std::ostringstream output;
  for (const char raw_character : value) {
    const auto character = static_cast<unsigned char>(raw_character);
    switch (character) {
      case '\"': output << "\\\""; break;
      case '\\': output << "\\\\"; break;
      case '\b': output << "\\b"; break;
      case '\f': output << "\\f"; break;
      case '\n': output << "\\n"; break;
      case '\r': output << "\\r"; break;
      case '\t': output << "\\t"; break;
      default:
        if (character < 0x20U) {
          output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                 << static_cast<unsigned int>(character) << std::dec;
        } else {
          output << static_cast<char>(character);
        }
    }
  }
  return output.str();
}

template<typename IntegerT>
void encode_little_endian(
  std::vector<std::uint8_t> & destination, std::size_t offset,
  IntegerT value)
{
  using UnsignedT = std::make_unsigned_t<IntegerT>;
  UnsignedT encoded = static_cast<UnsignedT>(value);
  for (std::size_t index = 0; index < sizeof(IntegerT) && offset + index < destination.size();
    ++index)
  {
    destination[offset + index] = static_cast<std::uint8_t>((encoded >> (index * 8U)) & 0xffU);
  }
}

[[noreturn]] void usage_error(const std::string & message)
{
  throw std::invalid_argument(
          message +
          "\nUsage: publisher [--topics N] [--rate HZ | --aggregate-rate HZ | "
          "--rate-per-topic HZ] [--payload-bytes N] [--duration SEC] "
          "[--qos reliable|best_effort] [--burst-every-sec SEC "
          "--burst-duration-ms MS --burst-multiplier X] [--churn-every-sec SEC "
          "--churn-down-ms MS] [--result-json PATH]");
}

std::string require_value(const std::vector<std::string> & arguments, std::size_t & index)
{
  if (index + 1U >= arguments.size()) {
    usage_error("missing value for " + arguments[index]);
  }
  ++index;
  return arguments[index];
}

std::size_t parse_size(const std::string & flag, const std::string & text)
{
  std::size_t consumed = 0;
  const auto value = std::stoull(text, &consumed);
  if (consumed != text.size() || value > std::numeric_limits<std::size_t>::max()) {
    usage_error("invalid value for " + flag + ": " + text);
  }
  return static_cast<std::size_t>(value);
}

double parse_double(const std::string & flag, const std::string & text)
{
  std::size_t consumed = 0;
  const double value = std::stod(text, &consumed);
  if (consumed != text.size() || !std::isfinite(value)) {
    usage_error("invalid value for " + flag + ": " + text);
  }
  return value;
}

Options parse_options(const std::vector<std::string> & arguments)
{
  Options options;
  bool rate_per_topic_set = false;
  for (std::size_t index = 1; index < arguments.size(); ++index) {
    const auto & argument = arguments[index];
    if (argument == "--help" || argument == "-h") {
      std::cout
        << "BlackBoxRS ROS 2 load publisher\n\n"
        << "--rate is an alias for --aggregate-rate. Rates are messages per second.\n"
        <<
        "Payload bytes describe std_msgs/ByteMultiArray.data, before ROS serialization overhead.\n";
      std::exit(0);
    } else if (argument == "--topics") {
      options.topics = parse_size(argument, require_value(arguments, index));
    } else if (argument == "--rate" || argument == "--aggregate-rate") {
      options.aggregate_rate = parse_double(argument, require_value(arguments, index));
      options.aggregate_rate_set = true;
    } else if (argument == "--rate-per-topic") {
      options.rate_per_topic = parse_double(argument, require_value(arguments, index));
      rate_per_topic_set = true;
    } else if (argument == "--payload-bytes") {
      options.payload_bytes = parse_size(argument, require_value(arguments, index));
    } else if (argument == "--duration" || argument == "--duration-sec") {
      options.duration_sec = parse_double(argument, require_value(arguments, index));
    } else if (argument == "--discovery-warmup-sec") {
      options.discovery_warmup_sec = parse_double(argument, require_value(arguments, index));
    } else if (argument == "--qos") {
      options.qos = require_value(arguments, index);
    } else if (argument == "--qos-depth") {
      options.qos_depth = parse_size(argument, require_value(arguments, index));
    } else if (argument == "--topic-prefix") {
      options.topic_prefix = require_value(arguments, index);
    } else if (argument == "--run-id") {
      options.run_id = require_value(arguments, index);
    } else if (argument == "--result-json") {
      options.result_json = require_value(arguments, index);
    } else if (argument == "--burst-every-sec") {
      options.burst_every_sec = parse_double(argument, require_value(arguments, index));
    } else if (argument == "--burst-duration-ms") {
      options.burst_duration_sec = parse_double(argument, require_value(arguments, index)) / 1000.0;
    } else if (argument == "--burst-multiplier") {
      options.burst_multiplier = parse_double(argument, require_value(arguments, index));
    } else if (argument == "--churn-every-sec") {
      options.churn_every_sec = parse_double(argument, require_value(arguments, index));
    } else if (argument == "--churn-down-ms") {
      options.churn_down_sec = parse_double(argument, require_value(arguments, index)) / 1000.0;
    } else if (argument == "--max-catch-up") {
      options.max_catch_up = parse_size(argument, require_value(arguments, index));
    } else {
      usage_error("unknown argument: " + argument);
    }
  }

  if (options.topics == 0U || options.payload_bytes == 0U || options.qos_depth == 0U ||
    options.max_catch_up == 0U)
  {
    usage_error("topics, payload-bytes, qos-depth, and max-catch-up must be positive");
  }
  if (options.duration_sec <= 0.0 || options.discovery_warmup_sec < 0.0) {
    usage_error("duration must be positive and discovery warmup must be non-negative");
  }
  if (rate_per_topic_set && options.aggregate_rate_set) {
    usage_error("choose aggregate-rate or rate-per-topic, not both");
  }
  if (rate_per_topic_set) {
    options.aggregate_rate = options.rate_per_topic * static_cast<double>(options.topics);
  }
  if (options.aggregate_rate <= 0.0) {
    usage_error("message rate must be positive");
  }
  if (options.qos != "reliable" && options.qos != "best_effort") {
    usage_error("qos must be reliable or best_effort");
  }
  if (options.burst_every_sec < 0.0 || options.burst_duration_sec < 0.0 ||
    options.burst_multiplier < 1.0)
  {
    usage_error("burst values must be non-negative and multiplier must be at least one");
  }
  if ((options.burst_every_sec == 0.0) != (options.burst_duration_sec == 0.0)) {
    usage_error("burst interval and duration must both be zero or both be positive");
  }
  if (options.burst_duration_sec > options.burst_every_sec && options.burst_every_sec > 0.0) {
    usage_error("burst duration cannot exceed burst interval");
  }
  if (options.churn_every_sec < 0.0 || options.churn_down_sec < 0.0) {
    usage_error("churn values must be non-negative");
  }
  if ((options.churn_every_sec == 0.0) != (options.churn_down_sec == 0.0)) {
    usage_error("churn interval and down time must both be zero or both be positive");
  }
  if (options.churn_down_sec >= options.churn_every_sec && options.churn_every_sec > 0.0) {
    usage_error("churn down time must be shorter than churn interval");
  }
  return options;
}

class LoadPublisher final : public rclcpp::Node
{
public:
  explicit LoadPublisher(Options options)
  : Node("blackbox_capture_bench_publisher"), options_(std::move(options))
  {
    const auto reliability = options_.qos == "reliable" ?
      RMW_QOS_POLICY_RELIABILITY_RELIABLE : RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT;
    qos_ = rclcpp::QoS(rclcpp::KeepLast(options_.qos_depth)).reliability(reliability);
    publishers_.resize(options_.topics);
    messages_.resize(options_.topics);
    for (std::size_t index = 0; index < options_.topics; ++index) {
      messages_[index].data.assign(
        options_.payload_bytes,
        static_cast<std::uint8_t>(index & 0xffU));
      const std::string magic{"BBRSBEN1"};
      for (std::size_t byte = 0; byte < magic.size() && byte < options_.payload_bytes; ++byte) {
        messages_[index].data[byte] = static_cast<std::uint8_t>(magic[byte]);
      }
      create_publisher_at(index);
    }
  }

  int run()
  {
    const auto process_start = SteadyClock::now();
    const auto warmup_deadline = process_start + std::chrono::duration_cast<SteadyClock::duration>(
      std::chrono::duration<double>(options_.discovery_warmup_sec));
    while (rclcpp::ok() && SteadyClock::now() < warmup_deadline) {
      matched_topics_ = count_matched_topics();
      if (matched_topics_ == publishers_.size()) {
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    matched_topics_ = count_matched_topics();

    measurement_start_ = SteadyClock::now();
    const auto measurement_end = measurement_start_ +
      std::chrono::duration_cast<SteadyClock::duration>(
      std::chrono::duration<double>(options_.duration_sec));
    auto previous = measurement_start_;
    double tokens = 0.0;
    std::size_t round_robin = 0;

    while (rclcpp::ok() && SteadyClock::now() < measurement_end) {
      const auto now = SteadyClock::now();
      const double elapsed = std::chrono::duration<double>(now - measurement_start_).count();
      const double delta = std::chrono::duration<double>(now - previous).count();
      previous = now;
      update_churn(elapsed);

      double effective_rate = options_.aggregate_rate;
      if (options_.burst_every_sec > 0.0 &&
        std::fmod(elapsed, options_.burst_every_sec) < options_.burst_duration_sec)
      {
        effective_rate *= options_.burst_multiplier;
      }
      tokens += delta * effective_rate;
      const double token_limit = static_cast<double>(options_.max_catch_up);
      if (tokens > token_limit) {
        schedule_overruns_ += static_cast<std::uint64_t>(tokens - token_limit);
        tokens = token_limit;
      }

      const auto ready = static_cast<std::size_t>(tokens);
      if (ready > 0U) {
        tokens -= static_cast<double>(ready);
        for (std::size_t count = 0; count < ready; ++count) {
          const auto topic_index = round_robin++ % options_.topics;
          publish_one(topic_index, now);
        }
      }
      if (ready == 0U) {
        std::this_thread::sleep_for(std::chrono::microseconds(50));
      }
    }

    measurement_end_ = SteadyClock::now();
    return rclcpp::ok() ? 0 : 130;
  }

  std::string result_json() const
  {
    const double actual_duration = std::chrono::duration<double>(
      measurement_end_ - measurement_start_).count();
    std::ostringstream output;
    output << std::setprecision(17)
           << "{\n"
           << "  \"schema_version\": \"blackboxrs.capture_bench_publisher.v1\",\n"
           << "  \"run_id\": \"" << json_escape(options_.run_id) << "\",\n"
           << "  \"topic_prefix\": \"" << json_escape(options_.topic_prefix) << "\",\n"
           << "  \"topics\": " << options_.topics << ",\n"
           << "  \"aggregate_rate_hz\": " << options_.aggregate_rate << ",\n"
           << "  \"payload_bytes\": " << options_.payload_bytes << ",\n"
           << "  \"qos\": \"" << options_.qos << "\",\n"
           << "  \"qos_depth\": " << options_.qos_depth << ",\n"
           << "  \"matched_topics_before_measurement\": " << matched_topics_ << ",\n"
           << "  \"requested_duration_sec\": " << options_.duration_sec << ",\n"
           << "  \"actual_duration_sec\": " << actual_duration << ",\n"
           << "  \"sent\": " << sent_ << ",\n"
           << "  \"sent_bytes\": " << sent_bytes_ << ",\n"
           << "  \"skipped_during_churn\": " << skipped_during_churn_ << ",\n"
           << "  \"schedule_overruns\": " << schedule_overruns_ << ",\n"
           << "  \"burst_every_sec\": " << options_.burst_every_sec << ",\n"
           << "  \"burst_duration_sec\": " << options_.burst_duration_sec << ",\n"
           << "  \"burst_multiplier\": " << options_.burst_multiplier << ",\n"
           << "  \"churn_every_sec\": " << options_.churn_every_sec << ",\n"
           << "  \"churn_down_sec\": " << options_.churn_down_sec << "\n"
           << "}\n";
    return output.str();
  }

private:
  std::size_t count_matched_topics() const
  {
    return static_cast<std::size_t>(std::count_if(
             publishers_.begin(), publishers_.end(), [](const auto & publisher) {
               return publisher && publisher->get_subscription_count() > 0U;
             }));
  }

  void create_publisher_at(std::size_t index)
  {
    publishers_[index] = create_publisher<ByteMessage>(
      options_.topic_prefix + std::to_string(index), qos_);
  }

  void update_churn(double elapsed_sec)
  {
    if (options_.churn_every_sec <= 0.0) {
      return;
    }
    const auto cycle = static_cast<std::uint64_t>(elapsed_sec / options_.churn_every_sec);
    const auto topic_index = static_cast<std::size_t>(cycle % options_.topics);
    const bool down = std::fmod(elapsed_sec, options_.churn_every_sec) < options_.churn_down_sec;
    for (std::size_t index = 0; index < publishers_.size(); ++index) {
      const bool should_exist = !(down && index == topic_index);
      if (should_exist && !publishers_[index]) {
        create_publisher_at(index);
      } else if (!should_exist && publishers_[index]) {
        publishers_[index].reset();
      }
    }
  }

  void publish_one(std::size_t topic_index, SteadyClock::time_point timestamp)
  {
    auto & publisher = publishers_[topic_index];
    if (!publisher) {
      ++skipped_during_churn_;
      return;
    }
    auto & data = messages_[topic_index].data;
    encode_little_endian(data, 8U, sent_);
    const auto monotonic_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
      timestamp.time_since_epoch()).count();
    encode_little_endian(data, 16U, monotonic_ns);
    encode_little_endian(data, 24U, static_cast<std::uint32_t>(topic_index));
    publisher->publish(messages_[topic_index]);
    ++sent_;
    sent_bytes_ += static_cast<std::uint64_t>(options_.payload_bytes);
  }

  Options options_;
  rclcpp::QoS qos_{rclcpp::KeepLast(10)};
  std::vector<rclcpp::Publisher<ByteMessage>::SharedPtr> publishers_;
  std::vector<ByteMessage> messages_;
  std::uint64_t sent_{0};
  std::uint64_t sent_bytes_{0};
  std::uint64_t skipped_during_churn_{0};
  std::uint64_t schedule_overruns_{0};
  std::size_t matched_topics_{0};
  SteadyClock::time_point measurement_start_{SteadyClock::now()};
  SteadyClock::time_point measurement_end_{measurement_start_};
};

void write_result(const std::string & path, const std::string & contents)
{
  if (path.empty()) {
    return;
  }
  const std::filesystem::path destination(path);
  if (!destination.parent_path().empty()) {
    std::filesystem::create_directories(destination.parent_path());
  }
  const auto temporary = destination.string() + ".tmp";
  {
    std::ofstream stream(temporary, std::ios::out | std::ios::trunc);
    if (!stream) {
      throw std::runtime_error("cannot open result file: " + temporary);
    }
    stream << contents;
    stream.flush();
    if (!stream) {
      throw std::runtime_error("cannot write result file: " + temporary);
    }
  }
  std::filesystem::rename(temporary, destination);
}

}  // namespace

int main(int argc, char ** argv)
{
  try {
    rclcpp::init(argc, argv);
    const auto non_ros_arguments = rclcpp::remove_ros_arguments(argc, argv);
    const auto options = parse_options(non_ros_arguments);
    auto node = std::make_shared<LoadPublisher>(options);
    const int result = node->run();
    const auto summary = node->result_json();
    write_result(options.result_json, summary);
    std::cout << summary;
    rclcpp::shutdown();
    return result;
  } catch (const std::exception & error) {
    std::cerr << "blackbox_capture_bench publisher: " << error.what() << '\n';
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
    return 2;
  }
}
