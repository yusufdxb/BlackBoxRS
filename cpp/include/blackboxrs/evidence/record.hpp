// A flight record: one line of records.jsonl.
//
// Every record carries the recorder's own clocks (t_mono_ns, t_wall_ns). A
// message record also carries up to three publisher-side times, each in its
// own clock domain (see time.hpp), and the payload: the extracted message
// fields as JSON, or null when the store rate decimated it. The line layout
// and field names are those of blackboxrs/flight/records.py, so the Python
// analysis reads C++ evidence unchanged.
//
// A Record is built on the recorder's pipeline thread, serialized once when
// FlightCore assigns its sequence number, and then shared read-only (through
// shared_ptr<const Record>) by the ring buffer and the evidence writer.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

#include "blackboxrs/json.hpp"
#include "blackboxrs/payload.hpp"
#include "blackboxrs/role.hpp"
#include "blackboxrs/time.hpp"

namespace blackboxrs {

using OrderedJson = nlohmann::ordered_json;

enum class RecordKind : std::uint8_t {
  msg,
  graph,
  sys,
  marker,
  trigger,
  health,
  clock_jump,
  recorder
};

[[nodiscard]] const char* record_kind_name(RecordKind k) noexcept;

struct Record {
  RecordKind kind = RecordKind::msg;
  std::int64_t seq = 0;
  MonoTime t_mono{};
  WallTime t_wall{};

  // message records
  std::string topic;
  Role role = Role::other;
  std::string role_name;
  std::string type;
  std::optional<RosTime> t_ros;
  std::optional<SourceTime> dds_src;
  std::optional<WallTime> dds_rx;
  std::optional<double> pub_stamp_s;
  std::optional<std::string> pub_stamp_domain;
  std::shared_ptr<const Json> data;  // null: not stored
  TypedPayload typed;

  // other kinds: payload fields in insertion order
  OrderedJson fields = OrderedJson::object();

  // Set by serialize(): the JSON line (no newline) and its length.
  std::string line;

  // Encode as one compact JSON object, key order as the Python recorder.
  void serialize();
  [[nodiscard]] std::size_t size_bytes() const noexcept { return line.size(); }
};

using RecordPtr = std::shared_ptr<const Record>;

// Message record with the publisher stamp resolved from the role
// (Python records.make_msg_record / publisher_stamp).
[[nodiscard]] Record make_msg_record(std::string topic, Role role, std::string type, Json data,
                                     bool stored, MonoTime t_mono, WallTime t_wall,
                                     std::optional<RosTime> t_ros,
                                     std::optional<SourceTime> dds_src,
                                     std::optional<WallTime> dds_rx);

[[nodiscard]] Record make_event_record(RecordKind kind, MonoTime t_mono, WallTime t_wall,
                                       OrderedJson fields = OrderedJson::object());

// Where a role's embedded publisher stamp lives and which clock wrote it.
struct StampSource {
  const char* path;
  const char* domain;
};
[[nodiscard]] std::optional<StampSource> stamp_source(Role role) noexcept;
// The stamp in seconds, or nullopt when absent, zero or non-finite.
[[nodiscard]] std::optional<double> publisher_stamp(Role role, const Json& data);

}  // namespace blackboxrs
