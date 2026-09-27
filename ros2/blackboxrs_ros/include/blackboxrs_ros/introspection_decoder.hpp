// Decode serialized ROS 2 messages of any type into record data, at run time.
//
// The recorder is told topic types by its profile (unitree_go, helix_msgs,
// std/geometry/nav msgs ...) and must not need them at compile time. For each
// type the decoder loads the C++ type support (to deserialize CDR) and the
// introspection type support (to walk the fields), keeps one reusable message
// buffer per type, and converts either the profile's dotted fields or the
// whole message into JSON with the Python recorder's rules
// (blackboxrs/flight/records.py to_plain / extract_fields): non-finite floats
// as "NaN"/"Infinity" strings, sequences longer than 256 kept as
// {"len", "head", "truncated"}, missing profile fields reported.
//
// Deserialization happens on the recorder's pipeline thread, never in the
// subscription callback. A decoder instance is used by one thread.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <rclcpp/serialized_message.hpp>

#include "blackboxrs/profile.hpp"
#include "blackboxrs/recorder/recorder.hpp"

namespace blackboxrs_ros {

// The executor's serialized message, shared (no copy of the bytes).
class SerializedPayload final : public blackboxrs::recorder::Payload {
 public:
  explicit SerializedPayload(std::shared_ptr<const rclcpp::SerializedMessage> msg)
      : msg_(std::move(msg)) {}
  [[nodiscard]] std::size_t size_bytes() const noexcept override { return msg_->size(); }
  [[nodiscard]] const rclcpp::SerializedMessage& message() const noexcept { return *msg_; }

 private:
  std::shared_ptr<const rclcpp::SerializedMessage> msg_;
};

class IntrospectionDecoder final : public blackboxrs::recorder::MessageDecoder {
 public:
  explicit IntrospectionDecoder(const blackboxrs::Profile& profile);
  ~IntrospectionDecoder() override;

  blackboxrs::recorder::DecodeResult decode(std::size_t topic,
                                            const blackboxrs::recorder::Payload& payload) override;

  // Why a topic's type could not be loaded (the package is not installed),
  // or nullopt when it can be decoded.
  [[nodiscard]] std::optional<std::string> unavailable(std::size_t topic) const;

 private:
  struct Type;
  std::vector<std::shared_ptr<Type>> by_topic_;
  std::vector<std::vector<std::string>> fields_;
  std::map<std::string, std::string> errors_;  // type -> load error
  std::vector<std::string> topic_types_;
};

// Serialize a typed message and decode it (tests and the decode check tool).
[[nodiscard]] blackboxrs::Json decode_to_json(const std::string& type,
                                              const rclcpp::SerializedMessage& msg,
                                              const std::vector<std::string>& fields,
                                              std::vector<std::string>* missing = nullptr);

}  // namespace blackboxrs_ros
