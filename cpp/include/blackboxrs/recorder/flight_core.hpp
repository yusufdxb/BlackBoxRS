// Recorder core: rolling window, triggers and incident lifecycle.
//
// ROS-free and clock-free: it sees only records and explicit times, so the
// live recorder, the offline re-trigger check and Replay Lab's liveness
// detection all run exactly this logic.
//
// Window model. Every record enters a ring keyed on recorder monotonic time.
// The ring keeps at least pre_trigger_sec of history (plus one second of
// margin), bounded by max_records and max_bytes; evictions forced by a cap are
// counted, separately when they cut into the pre-trigger window. When a trigger
// fires, the ring's pre-trigger window goes to a new incident sink at once,
// and every later record streams to it until post_trigger_sec after the last
// trigger attached to it, capped at 3 x post_trigger_sec after the first. A
// trigger while an incident is open is attached as secondary, so one fault
// chain gives one bundle.
//
// Single-threaded by design: one thread (the recorder pipeline, or a replay)
// owns a FlightCore. It holds no locks and needs none.
//
// C++ port of blackboxrs/flight/core.py.
#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "blackboxrs/evidence/record.hpp"
#include "blackboxrs/profile.hpp"

namespace blackboxrs::recorder {

// A trigger as fired (Python dict). Keys absent in Python are nullopt here.
struct Trigger {
  std::string type;
  MonoTime t_mono{};
  WallTime t_wall{};
  std::int64_t seq = 0;
  std::string role;  // primary, secondary, note (set when handed to a sink)
  std::optional<std::string> topic;
  std::optional<std::int64_t> record_seq;
  std::optional<std::string> node;
  std::optional<double> age_s;
  std::optional<double> limit_s;
  std::optional<bool> observed_edge;
  std::optional<Json> fault_id;
  std::optional<Json> reason;
  std::optional<Json> action;
  std::optional<Json> status;
  std::optional<Json> hold_fault_id;
  std::optional<std::string> note;
  std::optional<std::string> source;

  [[nodiscard]] OrderedJson to_json() const;
};

struct PreWindow {
  double requested_s = 0.0;
  double available_s = 0.0;
  std::int64_t evicted_by_cap_in_window = 0;
};

// What the core needs from an incident writer. The core calls these on its
// own thread; an implementation that does I/O hands the work to its own
// writer thread and returns at once.
class IncidentSink {
 public:
  IncidentSink() = default;
  virtual ~IncidentSink() = default;
  IncidentSink(const IncidentSink&) = delete;
  IncidentSink& operator=(const IncidentSink&) = delete;
  IncidentSink(IncidentSink&&) = delete;
  IncidentSink& operator=(IncidentSink&&) = delete;

  virtual std::string open(const Trigger& primary, std::vector<RecordPtr> pre,
                           const PreWindow& window) = 0;
  virtual void append(const RecordPtr& record) = 0;
  virtual void add_trigger(const Trigger& trigger) = 0;
  // Returns the path the incident will have once finalized, if any.
  virtual std::optional<std::string> close(const std::string& status, const Json& stats) = 0;
};

using SinkFactory = std::function<std::unique_ptr<IncidentSink>()>;
// (ok, reason) whether an incident may be opened now (disk space, ...).
using CanOpen = std::function<std::pair<bool, std::string>()>;

struct CoreStats {
  std::int64_t records_in = 0;
  std::int64_t ring_evicted_by_cap = 0;
  std::int64_t ring_evicted_by_cap_in_window = 0;
  std::int64_t incidents_opened = 0;
  std::int64_t incidents_closed = 0;
  std::int64_t incidents_skipped = 0;
  std::int64_t triggers_fired = 0;
  std::int64_t triggers_attached = 0;
  std::int64_t triggers_suppressed_limit = 0;
  std::map<std::string, std::int64_t> skip_reasons;
};

struct CoreOptions {
  // Serialize every record (the ring's byte cap needs the size, the writer
  // needs the line). A replay that only wants triggers can skip it.
  bool serialize = true;
  // Continuous capture (a C++ runtime addition; the Python recorder only has
  // triggered capture): one bundle opens with the first record (trigger
  // "recording_started") and stays open until shutdown, which closes it as
  // complete. Triggers still fire and are attached to it as secondary.
  bool continuous = false;
};

class FlightCore {
 public:
  FlightCore(Profile profile, SinkFactory factory, CanOpen can_open = {}, CoreOptions options = {});
  ~FlightCore();
  FlightCore(const FlightCore&) = delete;
  FlightCore& operator=(const FlightCore&) = delete;
  FlightCore(FlightCore&&) = delete;
  FlightCore& operator=(FlightCore&&) = delete;

  // Take one record (message or event); assigns its seq.
  void ingest(Record rec);
  // Manual marker: always recorded; triggers an incident if armed.
  void mark(MonoTime t_mono, WallTime t_wall, const std::string& note, const std::string& source);
  // Graph snapshot: records only the difference, plus a full snapshot at
  // least every 5 s; fires node_disappeared for watched nodes that left.
  void graph(MonoTime t_mono, WallTime t_wall, const std::vector<std::string>& nodes,
             const Json& topics, const std::map<std::string, std::vector<std::string>>& publishers);
  // Periodic: staleness checks and incident closing.
  void tick(MonoTime t_mono, WallTime t_wall);
  // Close any open incident as interrupted.
  void shutdown(const std::string& reason = "recorder_stopped");

  [[nodiscard]] bool incident_open() const noexcept { return open_.has_value(); }
  [[nodiscard]] const CoreStats& stats() const noexcept { return stats_; }
  [[nodiscard]] Json stats_json() const;
  [[nodiscard]] const std::vector<std::string>& closed_bundles() const noexcept {
    return closed_bundles_;
  }
  [[nodiscard]] std::size_t ring_records() const noexcept { return ring_.size(); }
  [[nodiscard]] std::int64_t ring_bytes() const noexcept { return ring_bytes_; }
  [[nodiscard]] const Profile& profile() const noexcept { return profile_; }

 private:
  struct Open {
    std::string bundle_id;
    std::unique_ptr<IncidentSink> sink;
    std::int64_t first_trigger_mono;
    std::int64_t close_at_mono;
    std::int64_t hard_close_mono;
    std::int64_t triggers = 1;
  };
  struct RingEntry {
    std::int64_t t_mono;
    std::size_t size;
    RecordPtr record;
  };

  std::optional<Record> clock_jump(const Record& rec);
  void push(const RecordPtr& rec);
  void evict();
  std::vector<Trigger> message_triggers(const Record& rec);
  void fire(Trigger trig);
  void maybe_close(std::int64_t now_mono);
  void close(const std::string& status);

  Profile profile_;
  SinkFactory factory_;
  CanOpen can_open_;
  CoreOptions options_;
  std::deque<RingEntry> ring_;
  std::int64_t ring_bytes_ = 0;
  std::int64_t seq_ = 0;
  std::optional<Open> open_;
  CoreStats stats_;
  std::vector<std::string> closed_bundles_;
  std::optional<std::pair<std::int64_t, std::int64_t>> last_wall_mono_;
  // trigger state
  std::optional<bool> hold_;
  bool arbiter_forced_ = false;
  bool session_started_ = false;
  std::map<std::string, std::int64_t> last_rx_;
  std::set<std::string> stale_;
  std::vector<std::pair<std::string, std::int64_t>> stale_after_;  // profile order
  std::optional<std::set<std::string>> nodes_;
  std::int64_t last_full_graph_ = 0;
  std::set<std::string> watched_nodes_;
  std::int64_t pre_ns_;
  std::int64_t post_ns_;
};

}  // namespace blackboxrs::recorder
