// Causal timeline: input -> detector -> safety state -> decision -> output.
//
// The timeline keeps state changes, not every message: the first event of
// each fault, hold messages that change the hold value, command-source
// messages that change that source's command, arbitration decisions whose
// reason or winner changed, robot-facing commands that changed, and every
// finding. Each entry lists the entries it follows from (caused_by) and the
// replay event ids that are its evidence, so a violation can be walked back
// to the injected fault. Ids are assigned after the replay, in time order.
//
// C++ port of blackboxrs/lab/timeline.py.
#pragma once

#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "blackboxrs/event.hpp"
#include "blackboxrs/replay/faults.hpp"
#include "blackboxrs/replay/types.hpp"

namespace blackboxrs::replay {

struct TimelineEntry {
  std::int64_t t_ns = 0;
  std::string layer;  // fault, input, detector, decision, output, invariant
  std::string text;
  std::vector<std::size_t> caused_by;  // indices into the entry list (before renumbering)
  std::vector<std::string> events;
  std::optional<std::string> finding;  // temporary id until renumbered
  std::optional<std::string> fault;
};

class Timeline {
 public:
  using Observer = std::function<void(const TimelineEntry&)>;

  Timeline(const std::vector<InjectionLog>& faults, std::set<std::string> hold_topics,
           std::set<std::string> source_topics, Observer observer = {});

  void advance(std::int64_t t_ns);
  void input(const Event& e);
  void decision(const Decision& d);
  void finding(const Finding& f, const std::string& tmp_id);

  // Entries in time order with ids T0001..., caused_by as ids, finding ids
  // mapped through `finding_ids`, related faults attached.
  [[nodiscard]] Json result(const std::map<std::string, std::string>& finding_ids,
                            const std::map<std::string, std::vector<std::string>>& related) const;

 private:
  std::size_t add(TimelineEntry entry);
  [[nodiscard]] std::vector<std::size_t> fault_links(const Event& e) const;
  [[nodiscard]] std::string describe(const Event& e) const;
  [[nodiscard]] std::string text(const Event& e) const;
  std::optional<std::size_t> entry_for(const std::string& eid);
  std::vector<std::size_t> cause_of(const Decision& d);

  std::vector<TimelineEntry> entries_;
  std::map<std::string, std::size_t> by_eid_;
  std::map<std::string, std::size_t> fault_entry_;
  std::deque<const InjectionLog*> pending_;
  std::set<std::string> hold_topics_;
  std::set<std::string> source_topics_;
  Observer observer_;
  std::optional<bool> last_hold_;
  std::map<std::string, std::string> last_src_;
  std::optional<std::pair<std::string, std::string>> last_dec_;
  std::optional<std::string> last_out_;
  std::optional<std::size_t> last_out_entry_;
  std::map<std::string, Event> events_;
};

[[nodiscard]] std::string fmt_cmd(const std::optional<RawCommand>& raw);

}  // namespace blackboxrs::replay
