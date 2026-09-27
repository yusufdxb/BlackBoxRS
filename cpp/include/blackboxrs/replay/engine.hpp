// Replay engine: evidence -> faults -> ordered dispatch -> findings -> verdict.
//
// Dispatch order at one replay instant: every event at time t, in (t, order)
// order, then the arbitration tick at t if one falls there. Ticks are at
// window_start + k * period: the arbiter's timer and the moments the monitors
// sample the robot-facing command. Within one event handlers run as:
// timeline, system under test, monitors, liveness. Within one tick: system
// under test, timeline, monitors, liveness. No handler sees the wall clock:
// the only notion of now is the ReplayClock, which has no now() of its own
// and never moves backwards.
//
// A Pacer may sleep between steps (real time, scaled, or step mode). It is
// told how far the virtual clock advanced and nothing it does feeds back into
// the replay, so a paced result equals an unpaced one byte for byte.
//
// C++ port of blackboxrs/lab/engine.py and verdict.py. The result is the same
// JSON document (schema blackboxrs.lab.result.v1) with the same keys.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/replay/faults.hpp"
#include "blackboxrs/replay/timeline.hpp"

namespace blackboxrs::replay {

inline constexpr const char* kResultSchema = "blackboxrs.lab.result.v1";

// Exit codes shared with `robot-blackbox lab replay`.
enum class ExitCode : int {
  pass = 0,
  fail = 1,
  usage = 2,
  incomplete = 3,
  detected = 4,
  error = 5,     // malformed evidence, case, fault or configuration
  internal = 6,  // a bug in the replay itself; never a verdict
};

class ClockError : public std::logic_error {
 public:
  using std::logic_error::logic_error;
};

// The only notion of "now" inside a replay.
class ReplayClock {
 public:
  explicit ReplayClock(ReplayTime start) : now_(start) {}
  [[nodiscard]] ReplayTime now() const noexcept { return now_; }
  // Move to `t`; returns the step taken. Never moves backwards.
  Nanos advance_to(ReplayTime t);

 private:
  ReplayTime now_;
};

// Called with each virtual step; may sleep. Never changes a result.
using Pacer = std::function<void(Nanos step)>;

struct ReplayConfig {
  std::string sut_mode = "reference";  // reference | observed
  std::string preset = "helix_arbiter";
  Json overrides = Json::object();
  std::vector<Fault> faults;
  Json topics = Json::object();  // per-topic overrides: host, liveness, stale_after_s
  std::optional<double> from_s;
  std::optional<double> to_s;
  std::map<std::string, double> command_sources;  // observed mode
  double stop_grace_s = 0.05;
  double fresh_grace_s = 0.05;
  double clock_step_threshold_s = 0.2;
  double clock_offset_info_s = 0.5;
  double observed_period_s = 0.02;

  [[nodiscard]] Json describe() const;
};

// Throws std::invalid_argument for out-of-range settings.
void validate_config(const ReplayConfig& cfg);

// Per-topic lab metadata: profile defaults, then case overrides
// (Python lab.evidence.topic_table).
[[nodiscard]] TopicTable topic_table(const Evidence& ev, const Json& overrides);

struct ReplayOptions {
  Pacer pacer;                  // empty: as fast as possible
  Timeline::Observer observer;  // streamed timeline entries (step mode, live view)
};

// Run one replay. Throws EvidenceError, FaultError or std::invalid_argument
// for bad input; anything else is an internal error.
[[nodiscard]] Json replay(const Evidence& ev, const ReplayConfig& cfg,
                          const ReplayOptions& options = {});

[[nodiscard]] ExitCode exit_code_for(const Json& result);

// Warning/critical, non-invariant finding kinds, sorted and unique.
[[nodiscard]] std::vector<std::string> detection_kinds(const Json& result);

// Case files (schema blackboxrs.lab.case.v1).
class CaseError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct Case {
  std::string path;
  std::string name;
  std::string evidence;  // resolved path
  ReplayConfig config;
  Json expect;
  Json raw;
};

[[nodiscard]] Case load_case(const std::string& path);
[[nodiscard]] std::vector<std::string> list_case_files(const std::string& dir);
// Mismatches between a result and a case's expect block ([] = match).
[[nodiscard]] std::vector<std::string> check_expectations(const Json& result, const Json& expect);

// The deterministic core of a result: everything except the run metadata that
// may legitimately differ between runs of one build (none today; the function
// exists so the determinism gate never depends on that staying true).
[[nodiscard]] std::string semantic_digest(const Json& result);

}  // namespace blackboxrs::replay
