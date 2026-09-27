#include "blackboxrs/replay/liveness.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace blackboxrs::replay {

double py_round(double x, int ndigits) {
  if (!std::isfinite(x)) {
    return x;
  }
  // printf rounds the exact binary value, ties to even, which is what
  // Python's round(float, n) does; parsing the text back gives its float.
  char buf[128];
  std::snprintf(buf, sizeof buf, "%.*f", ndigits, x);
  return std::strtod(buf, nullptr);
}

// Collects every trigger the core hands to an incident sink.
class LivenessMonitor::TriggerCollector final : public recorder::IncidentSink {
 public:
  explicit TriggerCollector(std::shared_ptr<std::vector<recorder::Trigger>> out)
      : out_(std::move(out)) {}
  std::string open(const recorder::Trigger& primary, std::vector<RecordPtr>,
                   const recorder::PreWindow&) override {
    out_->push_back(primary);
    return "replay";
  }
  void append(const RecordPtr&) override {}
  void add_trigger(const recorder::Trigger& trigger) override { out_->push_back(trigger); }
  std::optional<std::string> close(const std::string&, const Json&) override {
    return std::nullopt;
  }

 private:
  std::shared_ptr<std::vector<recorder::Trigger>> out_;
};

LivenessMonitor::LivenessMonitor(const Profile& profile, const TopicTable& topics, MonoTime t0,
                                 std::int64_t wall0_ns)
    : topics_(topics),
      t0_(t0),
      wall0_(wall0_ns),
      triggers_(std::make_shared<std::vector<recorder::Trigger>>()) {
  Profile p = profile;
  std::set<std::string> known;
  for (auto& spec : p.topics) {
    known.insert(spec.name);
    if (const auto it = topics.find(spec.name); it != topics.end()) {
      spec.stale_after_sec = it->second.stale_after_s;
    }
  }
  for (const auto& [name, info] : topics) {
    if (known.count(name) == 0U && info.liveness == Liveness::periodic) {
      TopicSpec spec;
      spec.name = name;
      spec.type = "unknown";
      spec.role = info.role;
      spec.stale_after_sec = info.stale_after_s;
      p.topics.push_back(std::move(spec));
    }
  }
  // Only the triggers matter here: never stop opening incidents.
  p.triggers.max_incidents_per_run = 1'000'000;
  auto trig = triggers_;
  recorder::CoreOptions opts;
  opts.serialize = false;
  core_ = std::make_unique<recorder::FlightCore>(
      std::move(p), [trig] { return std::make_unique<TriggerCollector>(trig); },
      recorder::CanOpen{}, opts);
}

LivenessMonitor::~LivenessMonitor() = default;

void LivenessMonitor::on_event(const Event& e, Findings& out) {
  if (const GraphBody* g = e.graph()) {
    for (const auto& [topic, plist] : g->publishers) {
      // every node ever seen publishing it: a node that has just left must
      // still count as this topic's publisher
      publishers_[topic].insert(plist.begin(), plist.end());
    }
  }
  if (const MessageBody* m = e.message(); m != nullptr && !m->topic.empty()) {
    Seen& s = seen_[m->topic];
    if (s.last_ns) {
      s.longest_silence_ns = std::max(s.longest_silence_ns, e.t_ns() - *s.last_ns);
    }
    ++s.count;
    s.last_ns = e.t_ns();
    s.last_eid = e.eid;
    if (const auto it = open_.find(m->topic); it != open_.end()) {
      episodes_[it->second].end_ns = e.t_ns();
      open_.erase(it);
    }
  }
  // Feed the recorder core the record this event stands for (Python
  // events.to_record + flight.replay.RecordFeeder).
  const MonoTime t_mono = to_mono(e.t, t0_);
  const WallTime t_wall = e.rec_wall + (t_mono - e.rec_mono);
  if (const MessageBody* m = e.message()) {
    Record r;
    r.kind = RecordKind::msg;
    r.topic = m->topic;
    r.role = m->role;
    r.role_name = m->role_name;
    r.type = m->type;
    r.t_mono = t_mono;
    r.t_wall = t_wall;
    r.data = m->data;
    r.typed = m->typed;
    core_->ingest(std::move(r));
  } else if (std::holds_alternative<SysBody>(e.body)) {
    core_->ingest(make_event_record(RecordKind::sys, t_mono, t_wall));
  } else if (const GraphBody* g = e.graph()) {
    bool feed = true;
    if (g->full) {
      feeder_nodes_ = std::set<std::string>(g->nodes.begin(), g->nodes.end());
    } else if (!feeder_nodes_) {
      feed = false;  // no full snapshot yet: a diff alone does not give the node set
    } else {
      for (const auto& n : g->nodes_gone) {
        feeder_nodes_->erase(n);
      }
      feeder_nodes_->insert(g->nodes_new.begin(), g->nodes_new.end());
    }
    if (feed) {
      core_->graph(t_mono, t_wall,
                   std::vector<std::string>(feeder_nodes_->begin(), feeder_nodes_->end()),
                   Json::object(), g->publishers);
    }
  } else if (const MarkerBody* mk = e.marker()) {
    core_->mark(t_mono, t_wall, mk->note, mk->source);
  }
  drain(out);
}

void LivenessMonitor::tick(ReplayTime t, Findings& out) {
  core_->tick(to_mono(t, t0_), wall_ns(wall0_ + count_ns(t)));
  drain(out);
}

void LivenessMonitor::drain(Findings& out) {
  while (n_trig_ < triggers_->size()) {
    const recorder::Trigger& trig = (*triggers_)[n_trig_++];
    const std::int64_t t = count_ns(trig.t_mono) - count_ns(t0_);
    if (trig.type == "topic_stale") {
      const std::string& topic = *trig.topic;
      const auto sit = seen_.find(topic);
      Episode ep;
      ep.topic = topic;
      const auto info = topics_.find(topic);
      ep.host = info != topics_.end() ? info->second.host : "unknown";
      ep.start_ns = t;
      ep.last_eid = sit != seen_.end() ? sit->second.last_eid : "";
      if (const auto p = publishers_.find(topic); p != publishers_.end()) {
        ep.publishers.assign(p->second.begin(), p->second.end());
      }
      ep.limit_s = trig.limit_s.value_or(0.0);
      open_[topic] = episodes_.size();
      episodes_.push_back(std::move(ep));
    } else if (trig.type == "node_disappeared") {
      gone_nodes_.emplace_back(t, *trig.node);
      Finding f;
      f.t = replay_ns(t);
      f.monitor = "liveness";
      f.kind = "node_disappeared";
      f.severity = Severity::warning;
      f.subject = *trig.node;
      f.message = "node " + *trig.node + " left the ROS graph (recorder trigger)";
      out.push_back(std::move(f));
    } else {
      Finding f;
      f.t = replay_ns(t);
      f.monitor = "liveness";
      f.kind = "recorder_trigger:" + trig.type;
      f.severity = Severity::info;
      if (trig.topic && !trig.topic->empty()) {
        f.subject = *trig.topic;
      } else if (trig.note && !trig.note->empty()) {
        f.subject = *trig.note;
      } else {
        f.subject = trig.type;
      }
      f.message = "flight recorder trigger " + trig.type + " fired";
      Json data = Json::object();
      if (trig.action) data["action"] = *trig.action;
      if (trig.fault_id) data["fault_id"] = *trig.fault_id;
      if (trig.reason) data["reason"] = *trig.reason;
      if (trig.status) data["status"] = *trig.status;
      f.data = std::move(data);
      out.push_back(std::move(f));
    }
  }
}

std::string LivenessMonitor::graph_verdict(const std::vector<const Episode*>& members,
                                           std::int64_t t_ns) const {
  std::set<std::string> pubs;
  for (const Episode* ep : members) {
    pubs.insert(ep->publishers.begin(), ep->publishers.end());
  }
  if (pubs.empty()) {
    return "no_graph_evidence";
  }
  std::set<std::string> gone;
  for (const auto& [t, n] : gone_nodes_) {
    if (t <= t_ns) {
      gone.insert(n);
    }
  }
  return std::includes(gone.begin(), gone.end(), pubs.begin(), pubs.end())
             ? "publishers_gone"
             : "publishers_still_advertised";
}

void LivenessMonitor::finish(ReplayTime, Findings& out) {
  std::set<std::size_t> used;
  std::map<std::string, std::vector<std::string>> by_host;
  for (const auto& [name, info] : topics_) {
    if (info.liveness == Liveness::periodic && seen_.count(name) != 0U) {
      by_host[info.host].push_back(name);
    }
  }
  std::vector<std::size_t> eps(episodes_.size());
  for (std::size_t i = 0; i < eps.size(); ++i) {
    eps[i] = i;
  }
  std::stable_sort(eps.begin(), eps.end(), [&](std::size_t a, std::size_t b) {
    return std::tie(episodes_[a].start_ns, episodes_[a].topic) <
           std::tie(episodes_[b].start_ns, episodes_[b].topic);
  });
  for (const auto& [host, periodic] : by_host) {
    if (periodic.size() < 2) {
      continue;
    }
    double max_stale = 0.0;
    for (const auto& t : periodic) {
      max_stale = std::max(max_stale, topics_.at(t).stale_after_s.value_or(0.0));
    }
    const std::int64_t window = seconds_to_ns(max_stale);
    std::vector<std::size_t> host_eps;
    for (std::size_t i : eps) {
      if (episodes_[i].host == host && used.count(i) == 0U) {
        host_eps.push_back(i);
      }
    }
    const std::set<std::string> want(periodic.begin(), periodic.end());
    for (std::size_t i : host_eps) {
      if (used.count(i) != 0U) {
        continue;
      }
      const Episode& first = episodes_[i];
      std::map<std::string, std::size_t> group{{first.topic, i}};
      for (std::size_t j : host_eps) {
        const Episode& ep = episodes_[j];
        if (used.count(j) != 0U || group.count(ep.topic) != 0U) {
          continue;
        }
        if (first.start_ns <= ep.start_ns && ep.start_ns <= first.start_ns + window) {
          group.emplace(ep.topic, j);
        }
      }
      std::set<std::string> got;
      for (const auto& [topic, idx] : group) {
        got.insert(topic);
      }
      if (got != want) {
        continue;
      }
      std::int64_t latest_start = INT64_MIN;
      for (const auto& [topic, idx] : group) {
        latest_start = std::max(latest_start, episodes_[idx].start_ns);
      }
      const bool recovered_early = std::any_of(group.begin(), group.end(), [&](const auto& kv) {
        const auto& end = episodes_[kv.second].end_ns;
        return end && *end <= latest_start;
      });
      if (recovered_early) {
        continue;
      }
      std::vector<const Episode*> members;  // map iteration: sorted by topic
      for (const auto& [topic, idx] : group) {
        used.insert(idx);
        members.push_back(&episodes_[idx]);
      }
      const std::string graph = graph_verdict(members, latest_start);
      std::optional<std::int64_t> recovered;
      bool all_ended = true;
      std::int64_t max_end = INT64_MIN;
      std::string names;
      std::vector<std::string> topic_list;
      std::vector<std::string> evidence;
      for (const Episode* ep : members) {
        if (!ep->end_ns) {
          all_ended = false;
        } else {
          max_end = std::max(max_end, *ep->end_ns);
        }
        names += (names.empty() ? "" : ", ") + ep->topic;
        topic_list.push_back(ep->topic);
        if (!ep->last_eid.empty()) {
          evidence.push_back(ep->last_eid);
        }
      }
      if (all_ended) {
        recovered = max_end;
      }
      const std::string note =
          graph == "publishers_still_advertised"
              ? "publishers still advertised on the graph while no data arrives: matches the DDS "
                "bound-to-the-wrong-interface signature"
          : graph == "publishers_gone" ? "publisher nodes left the graph"
                                       : "no graph evidence for these publishers";
      Finding f;
      f.t = replay_ns(first.start_ns);
      f.monitor = "liveness";
      f.kind = "transport_loss";
      f.severity = Severity::warning;
      f.subject = host;
      f.message = "every periodic topic from host " + host + " went silent together (" + names +
                  "); " + note;
      f.evidence = std::move(evidence);
      f.data = {{"topics", topic_list},
                {"graph", graph},
                {"recovered_t_ns", recovered ? Json(*recovered) : Json()}};
      out.push_back(std::move(f));
    }
  }
  for (std::size_t i : eps) {
    if (used.count(i) != 0U) {
      continue;
    }
    const Episode& ep = episodes_[i];
    std::vector<std::string> gone;
    const std::int64_t limit_ns = static_cast<std::int64_t>(ep.limit_s * 1e9);
    for (const auto& [t, n] : gone_nodes_) {
      if (std::find(ep.publishers.begin(), ep.publishers.end(), n) != ep.publishers.end() &&
          t <= ep.start_ns + limit_ns) {
        gone.push_back(n);
      }
    }
    const auto bh = by_host.find(ep.host);
    const bool single = bh == by_host.end() || bh->second.size() < 2;
    std::string kind;
    std::string why;
    if (!gone.empty()) {
      kind = "publisher_lost";
      std::string list;
      for (const auto& n : gone) {
        list += (list.empty() ? "" : ", ") + n;
      }
      why = "its publisher " + list + " left the graph";
    } else {
      kind = "stale_telemetry";
      why = !single ? "other periodic topics from the same host kept arriving"
                    : "it is the only periodic topic from host " + ep.host +
                          ", so a transport loss cannot be told apart";
    }
    Finding f;
    f.t = replay_ns(ep.start_ns);
    f.monitor = "liveness";
    f.kind = kind;
    f.severity = Severity::warning;
    f.subject = ep.topic;
    f.message = ep.topic + " silent past " + fmt_g(ep.limit_s) + " s; " + why;
    if (!ep.last_eid.empty()) {
      f.evidence = {ep.last_eid};
    }
    f.data = {{"host", ep.host}, {"recovered_t_ns", ep.end_ns ? Json(*ep.end_ns) : Json()}};
    out.push_back(std::move(f));
  }
}

Json LivenessMonitor::summary(ReplayTime t_end) const {
  Json out = Json::object();
  const std::int64_t end = count_ns(t_end);
  for (const auto& [name, info] : topics_) {
    const auto s = seen_.find(name);
    Json row = {{"liveness", info.liveness == Liveness::periodic ? "periodic" : "event"},
                {"host", info.host},
                {"stale_after_s", info.stale_after_s ? Json(*info.stale_after_s) : Json()},
                {"messages", s != seen_.end() ? s->second.count : 0}};
    if (s != seen_.end() && s->second.last_ns) {
      const std::int64_t last = *s->second.last_ns;
      row["silent_at_end_s"] = py_round(static_cast<double>(end - last) / 1e9, 6);
      row["longest_silence_s"] = py_round(
          static_cast<double>(std::max(s->second.longest_silence_ns, end - last)) / 1e9, 6);
      if (info.liveness == Liveness::event) {
        row["classification"] = "event topic: silence is inactivity, never stale";
      }
    }
    out[name] = std::move(row);
  }
  return out;
}

// ---------------------------------------------------------------------------
// transport data quality
// ---------------------------------------------------------------------------

namespace {

struct SeqStats {
  std::int64_t lost = 0;
  std::int64_t duplicates = 0;
  std::int64_t reordered = 0;
  std::int64_t restarts = 0;
};

// Python flight.analysis._seq_loss: loss, duplicates, reordering and restarts
// from the publisher's own counter. isinstance(seq, int) there includes bool.
SeqStats seq_loss(const std::vector<const Event*>& recs, bool with_epoch) {
  SeqStats st;
  std::optional<std::pair<Json, std::int64_t>> top;  // (epoch, highest seq in the run)
  std::set<std::int64_t> seen;
  auto run_lost = [&]() -> std::int64_t {
    if (seen.empty()) {
      return 0;
    }
    return *seen.rbegin() - *seen.begin() + 1 - static_cast<std::int64_t>(seen.size());
  };
  for (const Event* e : recs) {
    const MessageBody* m = e->message();
    if (m->data == nullptr) {
      continue;
    }
    const Json& d = *m->data;
    const auto sit = d.find("seq");
    if (sit == d.end()) {
      continue;
    }
    std::int64_t seq = 0;
    if (sit->is_boolean()) {
      seq = sit->get<bool>() ? 1 : 0;
    } else if (sit->is_number_integer() || sit->is_number_unsigned()) {
      seq = sit->get<std::int64_t>();
    } else {
      continue;
    }
    Json ep = 0;
    if (with_epoch) {
      const auto eit = d.find("epoch");
      ep = eit == d.end() ? Json() : *eit;
    }
    if (top) {
      const auto& [te, ts] = *top;
      if (ep != te || (!with_epoch && seq < ts && ts - seq >= 1000)) {
        ++st.restarts;
        st.lost += run_lost();
        seen.clear();
        top.reset();
      } else if (seen.count(seq) != 0U) {
        ++st.duplicates;
        continue;
      } else if (seq < ts) {
        ++st.reordered;
      }
    }
    seen.insert(seq);
    if (!top) {
      top = std::pair(ep, seq);
    } else {
      top->second = std::max(top->second, seq);
    }
  }
  st.lost += run_lost();
  return st;
}

}  // namespace

TransportResult analyze_delivered(const Profile& profile, const std::vector<Event>& delivered,
                                  ReplayTime t_end) {
  std::map<std::string, std::vector<const Event*>> by_topic;
  for (const Event& e : delivered) {
    if (const MessageBody* m = e.message()) {
      by_topic[m->topic].push_back(&e);
    }
  }
  std::map<std::string, std::int64_t> dups;
  std::map<std::string, std::int64_t> ooo;
  std::map<std::string, std::int64_t> lost;
  std::map<std::string, SeqStats> sequences;
  for (const auto& [topic, recs] : by_topic) {
    std::int64_t dup = 0;
    std::int64_t out_of_order = 0;
    std::set<std::pair<std::int64_t, std::string>> seen;
    std::optional<double> last_stamp;
    for (const Event* e : recs) {
      const MessageBody* m = e->message();
      if (m->src && count_ns(*m->src) != 0 && m->data != nullptr) {
        if (!seen.emplace(count_ns(*m->src), canonical_json(*m->data)).second) {
          ++dup;
        }
      }
      if (m->pub_stamp_s) {
        const double s = *m->pub_stamp_s;
        if (last_stamp && s < *last_stamp) {
          ++out_of_order;
        }
        last_stamp = last_stamp ? std::max(*last_stamp, s) : s;
      }
    }
    if (dup != 0) {
      dups[topic] = dup;
    }
    if (out_of_order != 0) {
      ooo[topic] = out_of_order;
    }
    const TopicSpec* spec = profile.topic(topic);
    const Role role = spec != nullptr ? spec->role : recs.front()->msg().role;
    if (role == Role::helix_hold || role == Role::arbiter_status) {
      const SeqStats st = seq_loss(recs, role == Role::helix_hold);
      sequences[topic] = st;
      if (st.lost != 0) {
        lost[topic] = st.lost;
      }
    }
  }
  TransportResult out;
  auto add = [&](const char* kind, const std::string& subject, std::int64_t n, const char* what) {
    Finding f;
    f.t = t_end;
    f.monitor = "transport";
    f.kind = kind;
    f.severity = Severity::warning;
    f.subject = subject;
    f.message =
        subject + ": " + std::to_string(n) + " " + what + " (flight analyzer, whole-stream pass)";
    f.data = {{"count", n}};
    out.findings.push_back(std::move(f));
  };
  std::map<std::string, std::int64_t> all_dups = dups;
  for (const auto& [name, st] : sequences) {
    if (st.duplicates != 0) {
      all_dups[name] = std::max(all_dups[name], st.duplicates);
    }
    if (st.reordered != 0) {
      add("sequence_reordered", name, st.reordered,
          "message(s) arrived with an older publisher sequence number");
    }
  }
  for (const auto& [name, n] : all_dups) {
    add("duplicate_messages", name, n, "duplicate message(s)");
  }
  for (const auto& [name, n] : ooo) {
    add("out_of_order_stamps", name, n, "message(s) stamped earlier than a previous one");
  }
  for (const auto& [name, n] : lost) {
    add("sequence_loss", name, n, "message(s) missing from the publisher sequence");
  }
  auto to_json = [](const std::map<std::string, std::int64_t>& m) {
    Json j = Json::object();
    for (const auto& [k, v] : m) {
      j[k] = v;
    }
    return j;
  };
  Json seq = Json::object();
  for (const auto& [name, st] : sequences) {
    seq[name] = {{"lost", st.lost},
                 {"duplicates", st.duplicates},
                 {"reordered", st.reordered},
                 {"publisher_restarts", st.restarts},
                 {"method", "gaps in the publisher's own sequence counter"}};
  }
  out.summary = {{"duplicates", to_json(dups)},
                 {"out_of_order_stamps", to_json(ooo)},
                 {"publisher_sequence_lost", to_json(lost)},
                 {"sequence", seq}};
  return out;
}

}  // namespace blackboxrs::replay
