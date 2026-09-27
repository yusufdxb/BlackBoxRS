// Detectors and safety invariants over the replayed stream.
//
// A Monitor sees every delivered event and every robot-facing decision and
// may emit findings. Invariants live in their own InvariantState objects with
// four-valued status (PASS / FAIL / INCOMPLETE / NOT_EXERCISED); detectors
// only emit findings. The monitors are an oracle independent of the
// arbitration model: they never call it, parse payloads through their own
// typed views, and judge freshness on the receipt clock only.
//
// Safety invariants:
//   stop_dominance    while a HELIX hold is asserted (delivered or recorded
//                     stream), every robot-facing command from grace after the
//                     hold until its release is zero
//   finite_output     every robot-facing command component is finite
//   fresh_output      a nonzero robot-facing command equals the latest valid
//                     message of a command source received within its window,
//                     or one it replaced less than grace ago (the arbiter's
//                     next tick has not happened yet)
//   consistent_state  a recorded ArbiterStatus never reports hold_active with
//                     a nonzero output
//
// Monitors are deterministic state machines: no clock, no randomness, no
// global state. reset() returns one to its constructed state.
//
// C++ port of blackboxrs/lab/monitors.py.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "blackboxrs/event.hpp"
#include "blackboxrs/replay/types.hpp"

namespace blackboxrs::replay {

using Findings = std::vector<Finding>;

class Monitor {
 public:
  Monitor() = default;
  virtual ~Monitor() = default;
  Monitor(const Monitor&) = delete;
  Monitor& operator=(const Monitor&) = delete;
  Monitor(Monitor&&) = delete;
  Monitor& operator=(Monitor&&) = delete;

  [[nodiscard]] virtual std::string_view name() const noexcept = 0;
  virtual void reset() = 0;
  virtual void on_event(const Event& /*e*/, Findings& /*out*/) {}
  virtual void on_decision(const Decision& /*d*/, Findings& /*out*/) {}
  virtual void finish(ReplayTime /*t_end*/, Findings& /*out*/) {}
  [[nodiscard]] virtual std::vector<const InvariantState*> invariants() const { return {}; }
};

// The HELIX hold state as one stream of hold messages shows it, ordered by the
// publisher's (epoch, seq). An older state is ignored while the current one
// is fresh; once stale, any state is accepted (HELIX P8).
class HoldTracker {
 public:
  explicit HoldTracker(std::int64_t timeout_ns) : timeout_ns_(timeout_ns) {}
  // Apply one hold message; true when the held state changed.
  bool update(const Event& e);

  [[nodiscard]] bool held() const noexcept { return held_; }
  [[nodiscard]] std::optional<std::int64_t> assert_t() const noexcept { return assert_t_; }
  [[nodiscard]] const std::string& assert_eid() const noexcept { return assert_eid_; }
  [[nodiscard]] const std::string& fault_id() const noexcept { return fault_id_; }
  [[nodiscard]] std::int64_t ignored_older() const noexcept { return ignored_older_; }
  [[nodiscard]] bool ever_asserted() const noexcept { return ever_asserted_; }

 private:
  std::int64_t timeout_ns_;
  std::optional<std::pair<std::int64_t, std::int64_t>> key_;
  std::optional<std::int64_t> last_rx_;
  bool held_ = false;
  std::optional<std::int64_t> assert_t_;
  std::string assert_eid_;
  std::string fault_id_;
  std::int64_t ignored_older_ = 0;
  bool ever_asserted_ = false;
};

class StopDominance final : public Monitor {
 public:
  StopDominance(std::set<std::string> hold_topics, std::set<std::string> source_topics,
                double grace_s, double hold_timeout_s);
  [[nodiscard]] std::string_view name() const noexcept override { return "stop_dominance"; }
  void reset() override;
  // A hold message from the evidence as recorded (before any fault).
  void on_recorded(const Event& e);
  void on_event(const Event& e, Findings& out) override;
  void on_decision(const Decision& d, Findings& out) override;
  void finish(ReplayTime t_end, Findings& out) override;
  [[nodiscard]] std::vector<const InvariantState*> invariants() const override { return {&inv_}; }
  [[nodiscard]] std::int64_t ignored_older() const noexcept { return delivered_.ignored_older(); }

 private:
  [[nodiscard]] const HoldTracker* active() const;

  std::set<std::string> hold_topics_;
  std::set<std::string> source_topics_;
  double grace_s_;
  std::int64_t grace_ns_;
  std::int64_t timeout_ns_;
  HoldTracker delivered_;
  HoldTracker recorded_;
  InvariantState inv_;
  bool in_violation_ = false;
  std::set<std::string> asked_;
};

class CommandPath final : public Monitor {
 public:
  CommandPath(std::map<std::string, double> sources, double grace_s);
  [[nodiscard]] std::string_view name() const noexcept override { return "command_path"; }
  void reset() override;
  void on_event(const Event& e, Findings& out) override;
  void on_decision(const Decision& d, Findings& out) override;
  void finish(ReplayTime t_end, Findings& out) override;
  [[nodiscard]] std::vector<const InvariantState*> invariants() const override {
    return {&finite_, &fresh_};
  }

 private:
  struct Latest {
    std::int64_t t_ns = 0;
    std::string eid;
    bool valid = false;
    std::optional<std::array<double, 3>> cmd;
    bool stale_reported = false;
  };
  [[nodiscard]] bool justified(const std::array<double, 3>& cmd, std::int64_t t) const;
  void set_latest(const std::string& topic, Latest next, std::int64_t t);

  std::map<std::string, std::int64_t> sources_;  // topic -> window ns
  std::map<std::string, double> sources_s_;
  double grace_s_;
  std::int64_t grace_ns_;
  std::map<std::string, Latest> latest_;
  // Messages a newer one replaced, with the time they were replaced; each can
  // still justify an output for grace after that (propagation through a
  // timer-driven arbiter). Pruned as time advances, so it stays small.
  std::map<std::string, std::vector<std::pair<Latest, std::int64_t>>> replaced_;
  std::map<std::string, bool> bad_run_;
  InvariantState fresh_;
  InvariantState finite_;
  bool stale_ep_ = false;
  bool nonfinite_ep_ = false;
  std::int64_t outputs_seen_ = 0;
};

class ConsistentState final : public Monitor {
 public:
  explicit ConsistentState(std::set<std::string> status_topics);
  [[nodiscard]] std::string_view name() const noexcept override { return "consistent_state"; }
  void reset() override;
  void on_event(const Event& e, Findings& out) override;
  [[nodiscard]] std::vector<const InvariantState*> invariants() const override { return {&inv_}; }

 private:
  std::set<std::string> status_topics_;
  InvariantState inv_;
  bool ep_ = false;
};

class ClockMonitor final : public Monitor {
 public:
  static constexpr std::size_t kBaselineSamples = 5;
  ClockMonitor(std::map<std::string, std::string> hosts, double step_threshold_s,
               double offset_info_s);
  [[nodiscard]] std::string_view name() const noexcept override { return "clock"; }
  void reset() override;
  void on_event(const Event& e, Findings& out) override;
  void finish(ReplayTime t_end, Findings& out) override;
  [[nodiscard]] Json summary() const;

 private:
  std::map<std::string, std::string> hosts_;
  std::int64_t thr_;
  std::int64_t offset_info_;
  std::map<std::string, std::vector<std::int64_t>> samples_;
  std::map<std::string, std::int64_t> baseline_;
  std::map<std::string, std::int64_t> baseline_t_;
  std::map<std::string, std::string> episode_;
  std::set<std::string> untimed_;
};

class OdometryConsistency final : public Monitor {
 public:
  explicit OdometryConsistency(std::set<std::string> topics, double moving_mps = 0.05,
                               int frozen_samples = 5, double jump_tolerance_m = 0.1);
  [[nodiscard]] std::string_view name() const noexcept override { return "odometry"; }
  void reset() override;
  void on_event(const Event& e, Findings& out) override;

 private:
  struct Sample {
    double t, x, y, speed;
  };
  std::set<std::string> topics_;
  double moving_;
  int frozen_n_;
  double tol_;
  std::map<std::string, Sample> prev_;
  std::map<std::string, int> frozen_;
  std::map<std::string, bool> bad_;
  std::map<std::string, bool> jump_;
};

// Python statistics.median_low.
[[nodiscard]] std::int64_t median_low(std::vector<std::int64_t> v);

// Python "{:.3f}"-style fixed formatting and "%g".
[[nodiscard]] std::string fmt_fixed(double v, int decimals);
[[nodiscard]] std::string fmt_g(double v);
[[nodiscard]] std::string fmt_signed_fixed(double v, int decimals);
[[nodiscard]] std::string py_list(const std::optional<RawCommand>& raw);
[[nodiscard]] std::string py_list(const std::array<double, 3>& v);

}  // namespace blackboxrs::replay
