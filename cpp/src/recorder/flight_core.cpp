#include "blackboxrs/recorder/flight_core.hpp"

#include <algorithm>
#include <array>
#include <cstdlib>

namespace blackboxrs::recorder {
namespace {

constexpr std::int64_t kNs = 1'000'000'000;
constexpr std::int64_t kFullGraphEveryNs = 5 * kNs;
constexpr std::int64_t kClockJumpNs = 50'000'000;
// Nodes that come and go as part of normal tooling, never a trigger.
constexpr std::array<std::string_view, 4> kIgnoredNodePrefixes{"/_ros2cli", "/blackbox", "/_",
                                                               "/launch_ros"};

// Python int(x * 1e9): truncation toward zero, not rounding.
std::int64_t trunc_ns(double s) {
  return static_cast<std::int64_t>(s * 1e9);
}

bool contains(const std::vector<std::string>& v, const Json* x) {
  if (x == nullptr || !x->is_string()) {
    return false;
  }
  return std::find(v.begin(), v.end(), x->get<std::string>()) != v.end();
}

Json get_or(const Json& d, const char* key, const Json& def) {
  const auto it = d.find(key);
  return it == d.end() ? def : *it;
}

}  // namespace

OrderedJson Trigger::to_json() const {
  OrderedJson j = OrderedJson::object();
  j["type"] = type;
  j["t_mono_ns"] = count_ns(t_mono);
  j["t_wall_ns"] = count_ns(t_wall);
  if (topic) j["topic"] = *topic;
  if (record_seq) j["record_seq"] = *record_seq;
  if (node) j["node"] = *node;
  if (age_s) j["age_s"] = *age_s;
  if (limit_s) j["limit_s"] = *limit_s;
  if (observed_edge) j["observed_edge"] = *observed_edge;
  if (fault_id) j["fault_id"] = *fault_id;
  if (reason) j["reason"] = *reason;
  if (action) j["action"] = *action;
  if (status) j["status"] = *status;
  if (hold_fault_id) j["hold_fault_id"] = *hold_fault_id;
  if (note) j["note"] = *note;
  if (source) j["source"] = *source;
  j["seq"] = seq;
  if (!role.empty()) j["role"] = role;
  return j;
}

FlightCore::FlightCore(Profile profile, SinkFactory factory, CanOpen can_open, CoreOptions options)
    : profile_(std::move(profile)),
      factory_(std::move(factory)),
      can_open_(std::move(can_open)),
      options_(options),
      pre_ns_(trunc_ns(profile_.buffer.pre_trigger_sec)),
      post_ns_(trunc_ns(profile_.buffer.post_trigger_sec)) {
  for (const auto& t : profile_.topics) {
    if (t.stale_after_sec) {
      stale_after_.emplace_back(t.name, trunc_ns(*t.stale_after_sec));
    }
  }
  watched_nodes_.insert(profile_.expected_nodes.begin(), profile_.expected_nodes.end());
}

FlightCore::~FlightCore() = default;

void FlightCore::ingest(Record rec) {
  if (options_.continuous && !session_started_) {
    session_started_ = true;
    Trigger start;
    start.type = "recording_started";
    start.t_mono = rec.t_mono;
    start.t_wall = rec.t_wall;
    fire(std::move(start));
    // Nothing can close a continuous incident before shutdown. If it could
    // not open (disk floor), stats().incidents_skipped says so and the
    // recorder refuses to report the session as recording.
    if (open_) {
      open_->close_at_mono = INT64_MAX;
      open_->hard_close_mono = INT64_MAX;
    }
  }
  rec.seq = ++seq_;
  ++stats_.records_in;
  std::optional<Record> jump = clock_jump(rec);
  const std::int64_t t = count_ns(rec.t_mono);
  const bool is_msg = rec.kind == RecordKind::msg;
  // Trigger conditions are evaluated on the record as ingested (they read
  // its payload and update the hold / arbiter state); they fire below, after
  // the record is in the ring, so it is part of its own pre-trigger window.
  std::vector<Trigger> triggers;
  if (is_msg) {
    triggers = message_triggers(rec);
  }
  if (options_.serialize) {
    rec.serialize();
    // Past this point only the serialized line is used (by the ring and the
    // writer): drop the decoded payload so a ring of records does not also
    // hold a JSON tree per message.
    rec.data.reset();
    rec.typed = OpaquePayload{};
    rec.fields = OrderedJson::object();
  }
  last_mono_ = rec.t_mono;
  last_wall_ = rec.t_wall;
  auto ptr = std::make_shared<const Record>(std::move(rec));
  // A continuous capture's one incident is open from the first record, so no
  // later trigger can need a pre-trigger window: nothing is kept in the ring.
  if (!(options_.continuous && open_)) {
    push(ptr);
  }
  if (open_) {
    open_->sink->append(ptr);
  }
  if (is_msg) {
    last_rx_[ptr->topic] = t;
    stale_.erase(ptr->topic);
    for (auto& trig : triggers) {
      fire(std::move(trig));
    }
  }
  if (jump) {
    ingest(std::move(*jump));
  }
  maybe_close(t);
}

std::optional<Record> FlightCore::clock_jump(const Record& rec) {
  if (rec.kind == RecordKind::clock_jump) {
    return std::nullopt;
  }
  const std::int64_t mono = count_ns(rec.t_mono);
  const std::int64_t wall = count_ns(rec.t_wall);
  const auto last = last_wall_mono_;
  last_wall_mono_ = std::pair(mono, wall);
  if (!last) {
    return std::nullopt;
  }
  const std::int64_t step = (wall - last->second) - (mono - last->first);
  if (std::llabs(step) <= kClockJumpNs) {
    return std::nullopt;
  }
  OrderedJson f = OrderedJson::object();
  f["wall_step_ns"] = step;
  f["note"] = "recorder wall clock stepped against its monotonic clock";
  return make_event_record(RecordKind::clock_jump, rec.t_mono, rec.t_wall, std::move(f));
}

void FlightCore::push(const RecordPtr& rec) {
  const std::int64_t newest = count_ns(rec->t_mono);
  ring_.push_back(RingEntry{newest, rec->size_bytes(), rec});
  ring_bytes_ += static_cast<std::int64_t>(rec->size_bytes());
  // Keep one extra second so a trigger at the edge still sees a full window.
  const std::int64_t horizon = newest - pre_ns_ - kNs;
  while (!ring_.empty() && ring_.front().t_mono < horizon) {
    evict();
  }
  const auto max_records = static_cast<std::size_t>(profile_.buffer.max_records);
  while (!ring_.empty() &&
         (ring_.size() > max_records || ring_bytes_ > profile_.buffer.max_bytes)) {
    ++stats_.ring_evicted_by_cap;
    if (ring_.front().t_mono >= newest - pre_ns_) {
      ++stats_.ring_evicted_by_cap_in_window;
    }
    evict();
  }
}

void FlightCore::evict() {
  ring_bytes_ -= static_cast<std::int64_t>(ring_.front().size);
  ring_.pop_front();
}

std::vector<Trigger> FlightCore::message_triggers(const Record& rec) {
  std::vector<Trigger> out;
  if (!rec.data) {
    return out;
  }
  const TriggerSpec& tr = profile_.triggers;
  const Json& data = *rec.data;
  auto base = [&](std::string type) {
    Trigger t;
    t.type = std::move(type);
    t.t_mono = rec.t_mono;
    t.t_wall = rec.t_wall;
    t.topic = rec.topic;
    t.record_seq = rec.seq;
    return t;
  };
  if (rec.role == Role::helix_hold) {
    const auto it = data.find("hold");
    const bool hold = it != data.end() && json_truthy(*it);
    if (hold && hold_ != true && tr.helix_hold_asserted) {
      Trigger t = base("helix_hold_asserted");
      t.observed_edge = hold_ == false;
      t.fault_id = get_or(data, "fault_id", "");
      t.reason = get_or(data, "reason", "");
      out.push_back(std::move(t));
    }
    hold_ = hold;
  } else if (rec.role == Role::recovery_action && tr.recovery_action_stop) {
    const auto a = data.find("action");
    const auto s = data.find("status");
    if (contains(tr.recovery_actions, a == data.end() ? nullptr : &*a) &&
        contains(tr.recovery_statuses, s == data.end() ? nullptr : &*s)) {
      Trigger t = base("recovery_action_stop");
      t.action = *a;
      t.status = *s;
      t.fault_id = get_or(data, "fault_id", "");
      out.push_back(std::move(t));
    }
  } else if (rec.role == Role::arbiter_status) {
    const auto r = data.find("reason");
    const bool forced = contains(tr.arbiter_reasons, r == data.end() ? nullptr : &*r);
    if (forced && !arbiter_forced_ && tr.arbiter_forced_zero) {
      Trigger t = base("arbiter_forced_zero");
      t.reason = *r;
      t.hold_fault_id = get_or(data, "hold_fault_id", "");
      out.push_back(std::move(t));
    }
    arbiter_forced_ = forced;
  }
  return out;
}

void FlightCore::mark(MonoTime t_mono, WallTime t_wall, const std::string& note,
                      const std::string& source) {
  OrderedJson f = OrderedJson::object();
  f["note"] = note;
  f["source"] = source;
  const std::int64_t marker_seq = seq_ + 1;  // ingest assigns it first; a clock_jump follows
  ingest(make_event_record(RecordKind::marker, t_mono, t_wall, std::move(f)));
  if (profile_.triggers.manual_marker) {
    Trigger t;
    t.type = "manual_marker";
    t.t_mono = t_mono;
    t.t_wall = t_wall;
    t.note = note;
    t.source = source;
    t.record_seq = marker_seq;
    fire(std::move(t));
  }
}

void FlightCore::graph(MonoTime t_mono, WallTime t_wall, const std::vector<std::string>& nodes,
                       const Json& topics,
                       const std::map<std::string, std::vector<std::string>>& publishers) {
  const std::set<std::string> now(nodes.begin(), nodes.end());
  for (const auto& [topic, plist] : publishers) {
    watched_nodes_.insert(plist.begin(), plist.end());
  }
  const std::int64_t t = count_ns(t_mono);
  std::vector<std::string> gone;
  std::vector<std::string> added;
  auto diff = [](const std::set<std::string>& a, const std::set<std::string>& b) {
    std::vector<std::string> out;
    std::set_difference(a.begin(), a.end(), b.begin(), b.end(), std::back_inserter(out));
    return out;
  };
  OrderedJson pubs = OrderedJson::object();
  for (const auto& [topic, plist] : publishers) {
    pubs[topic] = plist;
  }
  if (!nodes_ || t - last_full_graph_ >= kFullGraphEveryNs) {
    // A full snapshot at least every few seconds guarantees that any
    // pre-trigger window starts from a known node set.
    if (nodes_) {
      gone = diff(*nodes_, now);
      added = diff(now, *nodes_);
    }
    OrderedJson f = OrderedJson::object();
    f["full"] = true;
    f["nodes"] = std::vector<std::string>(now.begin(), now.end());
    f["topics"] =
        topics.is_null()
            ? OrderedJson::object()
            : OrderedJson::parse(topics.dump(-1, ' ', false, Json::error_handler_t::replace));
    f["publishers"] = pubs;
    f["nodes_gone"] = gone;
    f["nodes_new"] = added;
    ingest(make_event_record(RecordKind::graph, t_mono, t_wall, std::move(f)));
    last_full_graph_ = t;
    const bool first = !nodes_;
    nodes_ = now;
    if (first || gone.empty()) {
      return;
    }
  } else {
    gone = diff(*nodes_, now);
    added = diff(now, *nodes_);
    nodes_ = now;
    if (gone.empty() && added.empty()) {
      return;
    }
    OrderedJson f = OrderedJson::object();
    f["full"] = false;
    f["nodes_gone"] = gone;
    f["nodes_new"] = added;
    f["publishers"] = pubs;
    ingest(make_event_record(RecordKind::graph, t_mono, t_wall, std::move(f)));
  }
  if (!profile_.triggers.node_disappeared) {
    return;
  }
  for (const auto& node : gone) {
    const bool ignored = std::any_of(kIgnoredNodePrefixes.begin(), kIgnoredNodePrefixes.end(),
                                     [&](std::string_view p) { return node.starts_with(p); });
    if (ignored || watched_nodes_.count(node) == 0U) {
      continue;
    }
    Trigger trig;
    trig.type = "node_disappeared";
    trig.node = node;
    trig.t_mono = t_mono;
    trig.t_wall = t_wall;
    fire(std::move(trig));
  }
}

void FlightCore::tick(MonoTime t_mono, WallTime t_wall) {
  const std::int64_t t = count_ns(t_mono);
  if (profile_.triggers.topic_stale) {
    for (const auto& [topic, limit] : stale_after_) {
      const auto last = last_rx_.find(topic);
      if (last == last_rx_.end() || stale_.count(topic) != 0U) {
        continue;
      }
      const std::int64_t age = t - last->second;
      if (age > limit) {
        stale_.insert(topic);
        const double age_s = static_cast<double>(age) / 1e9;
        const double limit_s = static_cast<double>(limit) / 1e9;
        OrderedJson f = OrderedJson::object();
        f["event"] = "topic_stale";
        f["topic"] = topic;
        f["age_s"] = age_s;
        f["limit_s"] = limit_s;
        ingest(make_event_record(RecordKind::health, t_mono, t_wall, std::move(f)));
        Trigger trig;
        trig.type = "topic_stale";
        trig.topic = topic;
        trig.age_s = age_s;
        trig.limit_s = limit_s;
        trig.t_mono = t_mono;
        trig.t_wall = t_wall;
        fire(std::move(trig));
      }
    }
  }
  maybe_close(t);
}

void FlightCore::fire(Trigger trig) {
  ++stats_.triggers_fired;
  trig.seq = ++seq_;
  const std::int64_t t = count_ns(trig.t_mono);
  if (open_) {
    Open& o = *open_;
    ++o.triggers;
    if (!options_.continuous) {
      o.close_at_mono = std::min(std::max(o.close_at_mono, t + post_ns_), o.hard_close_mono);
    }
    trig.role = "secondary";
    o.sink->add_trigger(trig);
    ++stats_.triggers_attached;
    return;
  }
  if (stats_.incidents_opened >= profile_.triggers.max_incidents_per_run) {
    ++stats_.triggers_suppressed_limit;
    return;
  }
  if (can_open_) {
    const auto [ok, why] = can_open_();
    if (!ok) {
      ++stats_.incidents_skipped;
      ++stats_.skip_reasons[why];
      return;
    }
  }
  const std::int64_t start = t - pre_ns_;
  std::vector<RecordPtr> pre;
  for (const auto& e : ring_) {
    if (e.t_mono >= start) {
      pre.push_back(e.record);
    }
  }
  const std::int64_t ring_start = ring_.empty() ? t : ring_.front().t_mono;
  PreWindow window;
  window.requested_s = profile_.buffer.pre_trigger_sec;
  window.available_s = std::max(0.0, static_cast<double>(t - std::max(start, ring_start)) / 1e9);
  window.evicted_by_cap_in_window = stats_.ring_evicted_by_cap_in_window;
  std::unique_ptr<IncidentSink> sink = factory_();
  trig.role = "primary";
  std::string id = sink->open(trig, std::move(pre), window);
  ++stats_.incidents_opened;
  open_ = Open{std::move(id), std::move(sink), t, t + post_ns_, t + 3 * post_ns_, 1};
}

void FlightCore::maybe_close(std::int64_t now_mono) {
  if (open_ && now_mono >= open_->close_at_mono) {
    close("complete");
  }
}

void FlightCore::close(const std::string& status) {
  if (!open_) {
    return;
  }
  Open o = std::move(*open_);
  open_.reset();
  const auto path = o.sink->close(status, stats_json());
  ++stats_.incidents_closed;
  if (path) {
    closed_bundles_.push_back(*path);
  }
}

void FlightCore::shutdown(const std::string& reason, const std::string& status) {
  if (!open_) {
    return;
  }
  Trigger t;
  t.type = options_.continuous ? "recording_stopped" : "recorder_shutdown";
  t.reason = reason;
  t.role = "note";
  t.seq = ++seq_;
  // The last record ingested (Python: the newest ring entry, which is the
  // same record; the ring is empty in continuous mode).
  t.t_mono = last_mono_;
  t.t_wall = last_wall_;
  open_->sink->add_trigger(t);
  // Stopping ends a continuous capture normally; a triggered incident whose
  // post-trigger window was cut short is interrupted.
  close(!status.empty() ? status : options_.continuous ? "complete" : "interrupted");
}

Json FlightCore::stats_json() const {
  Json reasons = Json::object();
  for (const auto& [k, v] : stats_.skip_reasons) {
    reasons[k] = v;
  }
  return {{"records_in", stats_.records_in},
          {"ring_records", ring_.size()},
          {"ring_bytes", ring_bytes_},
          {"ring_evicted_by_cap", stats_.ring_evicted_by_cap},
          {"ring_evicted_by_cap_in_window", stats_.ring_evicted_by_cap_in_window},
          {"incidents_opened", stats_.incidents_opened},
          {"incidents_skipped", stats_.incidents_skipped},
          {"skip_reasons", reasons},
          {"triggers_fired", stats_.triggers_fired},
          {"triggers_attached", stats_.triggers_attached},
          {"triggers_suppressed_limit", stats_.triggers_suppressed_limit}};
}

}  // namespace blackboxrs::recorder
