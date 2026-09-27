// Telemetry liveness: legitimate inactivity, stale telemetry, transport loss.
//
// Silence detection is not reimplemented here: the replayed stream is fed to
// the recorder's own FlightCore, and its topic_stale and node_disappeared
// triggers are the detections (its other triggers become info findings, so
// the timeline shows what the recorder would have fired on). This module only
// classifies silence:
//   * an event topic (joystick, teleop) is never judged stale;
//   * a periodic topic that goes stale while its host's other periodic topics
//     keep arriving is stale_telemetry (publisher_lost when the graph shows its
//     publisher leaving);
//   * every periodic topic of one host going stale together is one
//     transport_loss for the host. If the publishers are still advertised,
//     the finding says it matches the CycloneDDS wrong-interface signature
//     (a match, not a diagnosis).
//
// C++ port of blackboxrs/lab/liveness.py.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "blackboxrs/event.hpp"
#include "blackboxrs/profile.hpp"
#include "blackboxrs/recorder/flight_core.hpp"
#include "blackboxrs/replay/monitors.hpp"
#include "blackboxrs/replay/types.hpp"

namespace blackboxrs::replay {

class LivenessMonitor {
 public:
  LivenessMonitor(const Profile& profile, const TopicTable& topics, MonoTime t0,
                  std::int64_t wall0_ns);
  ~LivenessMonitor();
  LivenessMonitor(const LivenessMonitor&) = delete;
  LivenessMonitor& operator=(const LivenessMonitor&) = delete;
  LivenessMonitor(LivenessMonitor&&) = delete;
  LivenessMonitor& operator=(LivenessMonitor&&) = delete;

  void on_event(const Event& e, Findings& out);
  void tick(ReplayTime t, Findings& out);
  void finish(ReplayTime t_end, Findings& out);
  [[nodiscard]] Json summary(ReplayTime t_end) const;

 private:
  struct Episode {
    std::string topic;
    std::string host;
    std::int64_t start_ns = 0;
    std::string last_eid;
    std::vector<std::string> publishers;
    std::optional<std::int64_t> end_ns;
    double limit_s = 0.0;
  };
  struct Seen {
    std::int64_t count = 0;
    std::optional<std::int64_t> last_ns;
    std::string last_eid;
    std::int64_t longest_silence_ns = 0;
  };
  class TriggerCollector;

  void drain(Findings& out);
  [[nodiscard]] std::string graph_verdict(const std::vector<const Episode*>& members,
                                          std::int64_t t_ns) const;

  TopicTable topics_;
  MonoTime t0_;
  std::int64_t wall0_;
  std::shared_ptr<std::vector<recorder::Trigger>> triggers_;
  std::unique_ptr<recorder::FlightCore> core_;
  std::optional<std::set<std::string>> feeder_nodes_;
  std::size_t n_trig_ = 0;
  std::map<std::string, Seen> seen_;
  std::map<std::string, std::size_t> open_;  // topic -> index into episodes_
  std::vector<Episode> episodes_;
  std::vector<std::pair<std::int64_t, std::string>> gone_nodes_;
  std::map<std::string, std::set<std::string>> publishers_;
};

// Whole-stream transport data quality of the delivered stream (the subset of
// blackboxrs/flight/analysis.py that Replay Lab uses): duplicates, stamps
// earlier than a previous one, and publisher sequence loss and reordering.
struct TransportResult {
  Findings findings;
  Json summary;
};
[[nodiscard]] TransportResult analyze_delivered(const Profile& profile,
                                                const std::vector<Event>& delivered,
                                                ReplayTime t_end);

// Python round(x, ndigits) for display fields in results.
[[nodiscard]] double py_round(double x, int ndigits);

}  // namespace blackboxrs::replay
