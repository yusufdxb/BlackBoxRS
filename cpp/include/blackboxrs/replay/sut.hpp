// The system under test: what command reaches the robot-facing boundary.
//
// observed   the arbitration output recorded in the evidence (/cmd_vel, or
//            ArbiterStatus.out_* when the profile left /cmd_vel alone).
// reference  a model of the arbitration path, run on the replayed and
//            possibly faulted inputs, so an input fault can change the outcome:
//   helix_arbiter     the policies of HELIX helix_arbiter/arbiter_core.py
//                     (P1-P10, docs/MOTION_ARBITRATION.md in HELIX). Checked
//                     decision by decision against that module
//                     (tests/cpp/test_helix_arbiter_parity.py).
//   twist_mux_legacy  the behaviour measured on twist_mux 4.3.0: STOP is a
//                     zero-twist input at priority 100 below teleop at 200,
//                     NaN is forwarded, nothing is published when every input
//                     is stale, and the sink keeps the last command.
//
// Everything runs on integer nanoseconds of the replay clock, supplied by the
// caller on every call. Nothing here reads a clock.
//
// C++ port of blackboxrs/lab/sut.py.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "blackboxrs/event.hpp"
#include "blackboxrs/replay/types.hpp"

namespace blackboxrs::replay {

struct SourceSpec {
  std::string name;
  std::string topic;
  std::int64_t priority = 0;
  double timeout_s = 0.5;
};

enum class StopMode : std::uint8_t { state, source };
enum class NonFinitePolicy : std::uint8_t { reject, forward };
enum class PublishMode : std::uint8_t { timer, on_input };
enum class FreshnessClock : std::uint8_t { receipt, source_timestamp };

struct ArbiterConfig {
  std::string preset;
  std::vector<SourceSpec> sources;
  std::string hold_topic = "/helix/hold";
  double hold_timeout_s = 0.5;
  double period_s = 0.02;
  double max_abs_linear = 1.0;
  double max_abs_angular = 1.5;
  StopMode stop_mode = StopMode::state;
  std::int64_t stop_priority = 100;
  double stop_timeout_s = 0.5;
  NonFinitePolicy nonfinite = NonFinitePolicy::reject;
  PublishMode publish = PublishMode::timer;
  FreshnessClock freshness_clock = FreshnessClock::receipt;

  [[nodiscard]] Json to_json() const;
};

// A preset with overrides applied and validated (Python sut.build_config).
[[nodiscard]] ArbiterConfig build_arbiter_config(const std::string& preset, const Json& overrides);

inline constexpr const char* kReasonSource = "SOURCE";
inline constexpr const char* kReasonHold = "HELIX_HOLD";
inline constexpr const char* kReasonStale = "HELIX_STATE_STALE";
inline constexpr const char* kReasonMissing = "HELIX_STATE_MISSING";
inline constexpr const char* kReasonNoInput = "NO_LIVE_INPUT";
inline constexpr const char* kReasonSilent = "SILENT";

class ReferenceArbiter {
 public:
  ReferenceArbiter(ArbiterConfig cfg, std::int64_t wall0_ns);

  void on_event(const Event& e, ReplayTime t);
  // A command published from an input callback at `t` (on_input publishers).
  [[nodiscard]] std::optional<Decision> take_publication(ReplayTime t);
  [[nodiscard]] Decision tick(ReplayTime t);
  [[nodiscard]] Json state() const;
  [[nodiscard]] const ArbiterConfig& config() const noexcept { return cfg_; }

 private:
  struct Slot {
    SourceSpec spec;
    std::optional<Command> cmd;
    std::optional<RawCommand> raw;
    std::optional<std::int64_t> rx_ns;
    std::optional<std::int64_t> fresh_ref_ns;
    std::int64_t order = -1;
    std::string eid;
  };
  struct Hold {
    bool hold = false;
    std::string fault_id;
    std::int64_t epoch = 0;
    std::int64_t seq = 0;
    std::int64_t rx_ns = 0;
    std::string eid;
  };

  void on_source(Slot& slot, const std::array<Json, 6>& values, const Event& e, std::int64_t t);
  void on_hold(const Event& e, std::int64_t t);
  [[nodiscard]] bool hold_fresh(std::int64_t t) const;
  [[nodiscard]] bool effective_hold(std::int64_t t) const;
  void clear();
  [[nodiscard]] bool fresh(const Slot& s, std::int64_t t) const;
  [[nodiscard]] Slot* winner(std::int64_t t);
  void publish(std::optional<Command> cmd, std::optional<RawCommand> raw, const std::string& eid);
  [[nodiscard]] Decision tick_state(std::int64_t t, const std::string& cause);
  [[nodiscard]] Decision tick_source(std::int64_t t);

  ArbiterConfig cfg_;
  std::int64_t wall0_ns_;
  std::vector<Slot> slots_;
  std::map<std::string, std::size_t> by_topic_;
  std::optional<Hold> hold_;
  std::int64_t order_ = 0;
  std::optional<Command> sink_;
  std::optional<RawCommand> sink_raw_;
  std::string sink_eid_;
  std::string pending_pub_;
  bool published_now_ = false;
  std::string rejected_;
  // counters, in the order they are reported
  std::int64_t rejected_count_ = 0;
  std::int64_t hold_reordered_ = 0;
  std::int64_t hold_transitions_ = 0;
  std::int64_t hold_malformed_ = 0;
  std::int64_t published_ = 0;
};

class ObservedOutput {
 public:
  explicit ObservedOutput(const std::map<std::string, std::vector<std::string>>& topics_by_role);

  [[nodiscard]] bool available() const noexcept { return source_label_.has_value(); }
  [[nodiscard]] const std::optional<std::string>& source_label() const noexcept {
    return source_label_;
  }
  void on_event(const Event& e, ReplayTime t);
  [[nodiscard]] std::optional<Decision> take_publication(ReplayTime t);
  [[nodiscard]] Decision tick(ReplayTime t);
  [[nodiscard]] Json state() const;

 private:
  void set(const RawCommand& raw, const Event& e);

  std::vector<std::string> out_topics_;
  std::vector<std::string> status_topics_;
  bool use_status_ = false;
  std::optional<std::string> source_label_;
  std::optional<Command> cmd_;
  std::optional<RawCommand> raw_;
  bool published_ = false;
  std::string reason_;
  std::string src_;
  std::optional<bool> hold_;
  std::string cause_;
  std::string pending_;
};

using SystemUnderTest = std::variant<ReferenceArbiter, ObservedOutput>;

// Typed views regardless of how the record labelled the topic.
[[nodiscard]] VelocityCommand twist_view(const MessageBody& m);
[[nodiscard]] HoldState hold_view(const MessageBody& m);
[[nodiscard]] ArbiterStatus status_view(const MessageBody& m);

}  // namespace blackboxrs::replay
