// Replay events and their total order.
//
// An Event is a value: replay time, a total-order key, an id, the ids of the
// faults that created or changed it, the recorder clocks of the record it came
// from, and a typed body. Copying an Event copies no payload bytes: the
// recorded payload is an immutable shared buffer, replaced (never mutated)
// when a fault changes it.
//
// Order: (t, origin, a, b), compared lexicographically.
//   evidence event          origin 0, a = recorder seq, b = 0
//   copy of evidence event  origin 0, a = seq of the original, b = 1, 2, ...
//   synthesized by a fault  origin 1, a = fault index, b = generation index
// At one instant, evidence (and its copies) comes before anything a fault
// synthesized, in fault order. Two events with one key are refused.
#pragma once

#include <compare>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <tuple>
#include <variant>
#include <vector>

#include "blackboxrs/json.hpp"
#include "blackboxrs/payload.hpp"
#include "blackboxrs/role.hpp"
#include "blackboxrs/time.hpp"

namespace blackboxrs {

struct OrderKey {
  std::int32_t origin = 0;
  std::int64_t a = 0;
  std::int64_t b = 0;
  // NOLINTNEXTLINE(modernize-use-nullptr): clang-tidy 14 false positive on a defaulted <=>
  friend constexpr auto operator<=>(const OrderKey&, const OrderKey&) = default;
};

struct MessageBody {
  std::string topic;
  Role role = Role::other;
  std::string role_name;  // as recorded (an unknown role is kept verbatim)
  std::string type;
  std::optional<SourceTime> src;      // DDS source timestamp (publisher host wall clock)
  std::optional<WallTime> rx_wall;    // DDS reception, else callback wall time
  std::optional<double> pub_stamp_s;  // stamp embedded in the message
  std::optional<std::string> pub_stamp_domain;
  std::shared_ptr<const Json> data;  // null: payload not stored (decimated)
  TypedPayload typed;                // decode_payload(role, *data); Opaque when no data

  void set_data(Json d) {
    typed = decode_payload(role, d);
    data = std::make_shared<const Json>(std::move(d));
  }
};

struct GraphBody {
  bool full = false;
  std::vector<std::string> nodes;
  std::vector<std::string> nodes_gone;
  std::vector<std::string> nodes_new;
  std::map<std::string, std::vector<std::string>> publishers;
  bool has_publishers = false;  // the record carried a publishers key
};

struct MarkerBody {
  std::string note;
  std::string source;
};

struct SysBody {};

using EventBody = std::variant<MessageBody, GraphBody, MarkerBody, SysBody>;

struct Event {
  ReplayTime t{};
  OrderKey order;
  std::string eid;
  EventBody body;
  std::vector<std::string> injected;  // fault ids, in the order they touched it
  // Clocks of the record this event came from (or was synthesized as).
  MonoTime rec_mono{};
  WallTime rec_wall{};
  // The record as stored, for provenance. Logic reads `body`.
  std::shared_ptr<const Json> record;

  [[nodiscard]] const MessageBody* message() const noexcept {
    return std::get_if<MessageBody>(&body);
  }
  [[nodiscard]] MessageBody* message() noexcept { return std::get_if<MessageBody>(&body); }
  [[nodiscard]] const GraphBody* graph() const noexcept { return std::get_if<GraphBody>(&body); }
  [[nodiscard]] GraphBody* graph() noexcept { return std::get_if<GraphBody>(&body); }
  [[nodiscard]] const MarkerBody* marker() const noexcept { return std::get_if<MarkerBody>(&body); }
  [[nodiscard]] bool is_message() const noexcept { return message() != nullptr; }
  // The message body of an event the caller has already selected as a
  // message. Throws std::logic_error when that invariant is broken.
  [[nodiscard]] const MessageBody& msg() const;
  [[nodiscard]] MessageBody& msg();
  // Topic of a message event, empty otherwise.
  [[nodiscard]] const std::string& topic() const noexcept;
  [[nodiscard]] std::string_view kind_name() const noexcept;

  [[nodiscard]] std::int64_t t_ns() const noexcept { return count_ns(t); }
  friend bool operator<(const Event& x, const Event& y) noexcept {
    return std::tie(x.t, x.order) < std::tie(y.t, y.order);
  }

  // Record that fault `fault_id` created or changed this event.
  void touch(const std::string& fault_id);
};

// Build a replay event from a validated flight record.
[[nodiscard]] Event event_from_record(const Json& record, MonoTime evidence_start);

}  // namespace blackboxrs
