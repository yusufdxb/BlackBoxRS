// Deterministic fault injection on the replay event stream.
//
// A fault is a pure function from an ordered event list to a new ordered
// event list. There is no randomness, so no seed is taken. Faults apply in the
// order given, each to the output of the previous one. Every event a fault
// creates or changes carries the fault id (Event::injected), and the
// injection log lists, per fault, how many events it touched, on which topics
// and when. A fault that matches no event is an error, never a silent no-op.
//
// Times are seconds from the start of the evidence on the replay (receipt)
// clock; windows are half-open [from_s, to_s).
//
// C++ port of blackboxrs/lab/faults.py; the kinds, parameters, defaults and
// error conditions are the same, and tests/cpp/test_replay_parity.py checks the
// injection logs against the Python implementation.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "blackboxrs/event.hpp"
#include "blackboxrs/evidence/record.hpp"
#include "blackboxrs/json.hpp"
#include "blackboxrs/replay/types.hpp"

namespace blackboxrs::replay {

class FaultError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

enum class ParamType : std::uint8_t { f64, i64, boolean, str, list, json };

struct ParamSpec {
  std::string name;
  ParamType type = ParamType::json;
  bool required = false;
  Json default_value;  // null when absent
  std::string help;
};

enum class FaultKind : std::uint8_t {
  drop,
  gap,
  delay,
  duplicate,
  reorder,
  stale_redelivery,
  clock_skew,
  timestamp_jump,
  nan,
  inf,
  malformed,
  set_value,
  freeze,
  step,
  node_exit,
  inject_stream,
};

struct KindSpec {
  FaultKind kind;
  std::string name;
  std::string category;  // transport, data, control
  std::string summary;
  std::vector<ParamSpec> params;  // in declaration order
};

// All kinds, sorted by name.
[[nodiscard]] const std::vector<KindSpec>& fault_kinds();
[[nodiscard]] const KindSpec* find_kind(std::string_view name) noexcept;

// A parsed, validated fault. Every declared parameter is present in `params`
// (defaults filled in, absent optionals as null), already coerced to its type.
struct Fault {
  std::string id;
  FaultKind kind = FaultKind::drop;
  std::string kind_name;
  std::map<std::string, Json> params;
  // Python str() of each parameter as written (key order preserved), for
  // display text only.
  std::map<std::string, std::string> param_text;

  [[nodiscard]] Json describe() const;  // {"id", "kind", "params"}
  [[nodiscard]] double f64(const std::string& k) const;
  [[nodiscard]] std::optional<double> opt_f64(const std::string& k) const;
  [[nodiscard]] std::int64_t i64(const std::string& k) const;
  [[nodiscard]] bool flag(const std::string& k) const;
  [[nodiscard]] std::optional<std::string> opt_str(const std::string& k) const;
  [[nodiscard]] std::vector<std::string> list(const std::string& k) const;
  [[nodiscard]] const Json& json(const std::string& k) const;
};

// Parse a fault object ({"kind": ..., params}). `index` is its position,
// used for the default id "F<index+1>".
[[nodiscard]] Fault parse_fault(const Json& raw, std::size_t index);
// Same, keeping the source key order of object-valued parameters for display.
[[nodiscard]] Fault parse_fault_ordered(const OrderedJson& raw, std::size_t index);

// Parse "kind:key=value,key=value" (values read as JSON when they parse,
// lists separated by '|') or a JSON object.
[[nodiscard]] Fault parse_cli_fault(std::string_view text, std::size_t index);

struct InjectionLog {
  std::string id;
  std::string kind;
  std::string category;
  std::map<std::string, Json> params;
  std::map<std::string, std::string> param_text;
  std::int64_t events_affected = 0;
  std::vector<std::string> topics_affected;  // sorted
  ReplayTime first_t{};
  ReplayTime last_t{};
  std::vector<std::string> first_events;  // up to 10 event ids

  [[nodiscard]] Json to_json() const;
};

// Apply `faults` in order. `topics` may gain an entry (inject_stream on a topic
// that is not in the evidence). Throws FaultError.
[[nodiscard]] std::vector<Event> apply_faults(std::vector<Event> events,
                                              const std::vector<Fault>& faults, TopicTable& topics,
                                              std::vector<InjectionLog>& log);

// Refuse two events with one (time, order) key or one id.
void check_total_order(const std::vector<Event>& events);

}  // namespace blackboxrs::replay
