#include "blackboxrs/replay/timeline.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <numeric>

#include "blackboxrs/replay/liveness.hpp"
#include "blackboxrs/replay/monitors.hpp"

namespace blackboxrs::replay {
namespace {

std::string repr_scalar(const Json& v) {
  if (v.is_null()) return "None";
  if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
  if (v.is_string()) return "'" + v.get<std::string>() + "'";
  return v.dump();
}

std::string str_of(const Json* v) {
  if (v == nullptr || v->is_null()) return "None";
  if (v->is_boolean()) return v->get<bool>() ? "True" : "False";
  if (v->is_string()) return v->get<std::string>();
  return v->dump();
}

}  // namespace

std::string fmt_cmd(const std::optional<RawCommand>& raw) {
  if (!raw) {
    return "none";
  }
  std::string out = "(";
  for (std::size_t i = 0; i < raw->size(); ++i) {
    const Json& v = (*raw)[i];
    const Numeric n = as_number(&v);
    std::string part;
    if (n.state == NumState::malformed) {
      part = repr_scalar(v);
    } else if (n.state == NumState::ok) {
      part = fmt_fixed(n.value, 3);
    } else {
      part = std::isnan(n.value) ? "nan" : (n.value > 0 ? "inf" : "-inf");
    }
    out += (i != 0U ? ", " : "") + part;
  }
  return out + ")";
}

Timeline::Timeline(const std::vector<InjectionLog>& faults, std::set<std::string> hold_topics,
                   std::set<std::string> source_topics, Observer observer)
    : hold_topics_(std::move(hold_topics)),
      source_topics_(std::move(source_topics)),
      observer_(std::move(observer)) {
  std::vector<const InjectionLog*> sorted;
  for (const auto& f : faults) {
    sorted.push_back(&f);
  }
  std::sort(sorted.begin(), sorted.end(), [](const InjectionLog* a, const InjectionLog* b) {
    return std::tie(a->first_t, a->id) < std::tie(b->first_t, b->id);
  });
  pending_.assign(sorted.begin(), sorted.end());
}

std::size_t Timeline::add(TimelineEntry entry) {
  std::sort(entry.caused_by.begin(), entry.caused_by.end());
  entry.caused_by.erase(std::unique(entry.caused_by.begin(), entry.caused_by.end()),
                        entry.caused_by.end());
  entries_.push_back(std::move(entry));
  if (observer_) {
    observer_(entries_.back());
  }
  return entries_.size() - 1;
}

void Timeline::advance(std::int64_t t_ns) {
  while (!pending_.empty() && count_ns(pending_.front()->first_t) <= t_ns) {
    const InjectionLog* f = pending_.front();
    pending_.pop_front();
    std::string params;
    for (const auto& [k, v] : f->params) {
      if (v.is_null()) {
        continue;
      }
      const auto t = f->param_text.find(k);
      params += (params.empty() ? "" : ", ") + k + "=" +
                (t != f->param_text.end() ? t->second : py_str_any(v));
    }
    TimelineEntry e;
    e.t_ns = count_ns(f->first_t);
    e.layer = "fault";
    e.text = "fault " + f->id + " " + f->kind + " (" + params +
             "): " + std::to_string(f->events_affected) + " event(s) affected";
    for (std::size_t i = 0; i < f->first_events.size() && i < 3; ++i) {
      e.events.push_back(f->first_events[i]);
    }
    e.fault = f->id;
    fault_entry_[f->id] = add(std::move(e));
  }
}

std::vector<std::size_t> Timeline::fault_links(const Event& e) const {
  std::vector<std::size_t> out;
  for (const auto& f : e.injected) {
    if (const auto it = fault_entry_.find(f); it != fault_entry_.end()) {
      out.push_back(it->second);
    }
  }
  return out;
}

std::string Timeline::describe(const Event& e) const {
  const MessageBody* m = e.message();
  if (m == nullptr) {
    return std::string(e.kind_name()) + " message";
  }
  static const Json kEmpty = Json::object();
  const Json& d = m->data != nullptr ? *m->data : kEmpty;
  if (hold_topics_.count(m->topic) != 0U) {
    std::string hold = str_of(get_path(d, "hold"));
    std::transform(hold.begin(), hold.end(), hold.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const Json* fid = get_path(d, "fault_id");
    const bool fid_truthy = fid != nullptr && json_truthy(*fid);
    return m->topic + " hold=" + hold + " fault_id=" + (fid_truthy ? str_of(fid) : "-") +
           " epoch=" + str_of(get_path(d, "epoch")) + " seq=" + str_of(get_path(d, "seq"));
  }
  if (source_topics_.count(m->topic) != 0U) {
    const VelocityCommand v = std::get<VelocityCommand>(decode_payload(Role::cmd_vel_source, d));
    return m->topic + " command " +
           fmt_cmd(RawCommand{v.forwarded[0], v.forwarded[1], v.forwarded[2]});
  }
  return (m->topic.empty() ? std::string(e.kind_name()) : m->topic) + " message";
}

std::string Timeline::text(const Event& e) const {
  std::string t = describe(e);
  if (!e.injected.empty()) {
    std::string ids;
    for (const auto& f : e.injected) {
      ids += (ids.empty() ? "" : ", ") + f;
    }
    t += " [injected by " + ids + "]";
  }
  return t;
}

void Timeline::input(const Event& e) {
  events_.insert_or_assign(e.eid, e);
  const MessageBody* m = e.message();
  if (m == nullptr || m->data == nullptr) {
    return;
  }
  bool changed = false;
  if (hold_topics_.count(m->topic) != 0U) {
    const Flag hold = read_flag(get_path(*m->data, "hold"));
    if (flag_is_bool(hold)) {
      const bool h = hold == Flag::is_true;
      if (!last_hold_ || *last_hold_ != h) {
        last_hold_ = h;
        changed = true;
      }
    }
  } else if (source_topics_.count(m->topic) != 0U) {
    const Json* lx = get_path(*m->data, "linear.x");
    const Json* ly = get_path(*m->data, "linear.y");
    const Json* az = get_path(*m->data, "angular.z");
    const std::string sig =
        canonical_json(Json::array({lx != nullptr ? *lx : Json(), ly != nullptr ? *ly : Json(),
                                    az != nullptr ? *az : Json()}));
    const auto it = last_src_.find(m->topic);
    if (it == last_src_.end() || it->second != sig) {
      last_src_[m->topic] = sig;
      changed = true;
    }
  }
  if (changed) {
    TimelineEntry entry;
    entry.t_ns = e.t_ns();
    entry.layer = "input";
    entry.text = text(e);
    entry.caused_by = fault_links(e);
    entry.events = {e.eid};
    by_eid_[e.eid] = add(std::move(entry));
  }
}

std::optional<std::size_t> Timeline::entry_for(const std::string& eid) {
  if (const auto it = by_eid_.find(eid); it != by_eid_.end()) {
    return it->second;
  }
  const auto ev = events_.find(eid);
  if (ev == events_.end()) {
    return std::nullopt;
  }
  TimelineEntry entry;
  entry.t_ns = ev->second.t_ns();
  entry.layer = "input";
  entry.text = text(ev->second);
  entry.caused_by = fault_links(ev->second);
  entry.events = {eid};
  const std::size_t idx = add(std::move(entry));
  by_eid_[eid] = idx;
  return idx;
}

std::vector<std::size_t> Timeline::cause_of(const Decision& d) {
  if (d.cause == "clock") {
    return {};
  }
  if (const auto idx = entry_for(d.cause)) {
    return {*idx};
  }
  return {};
}

void Timeline::decision(const Decision& d) {
  std::optional<std::size_t> dec_entry;
  const auto key = std::pair(d.reason, d.source);
  if (!last_dec_ || *last_dec_ != key) {
    last_dec_ = key;
    TimelineEntry entry;
    entry.caused_by = cause_of(d);
    const std::string why = d.cause == "clock" ? "time-driven, no new input" : "after " + d.cause;
    const std::string silent =
        d.reason == "SILENT"
            ? ": no live input, nothing is published, the robot-facing sink keeps " +
                  fmt_cmd(d.robot_raw)
            : "";
    entry.t_ns = count_ns(d.t);
    entry.layer = "decision";
    entry.text = "arbitration " + d.reason + (d.source.empty() ? "" : ", winner " + d.source) +
                 " (" + why + ")" + silent;
    if (d.cause != "clock") {
      entry.events = {d.cause};
    }
    dec_entry = add(std::move(entry));
  }
  const std::string out = canonical_json(raw_json(d.robot_raw));
  if (!last_out_ || *last_out_ != out) {
    last_out_ = out;
    TimelineEntry entry;
    entry.t_ns = count_ns(d.t);
    entry.layer = "output";
    entry.text = "robot-facing command " + fmt_cmd(d.robot_raw);
    entry.caused_by = dec_entry ? std::vector<std::size_t>{*dec_entry} : cause_of(d);
    last_out_entry_ = add(std::move(entry));
  }
}

void Timeline::finding(const Finding& f, const std::string& tmp_id) {
  TimelineEntry entry;
  for (const auto& eid : f.evidence) {
    if (const auto idx = entry_for(eid)) {
      entry.caused_by.push_back(*idx);
    }
  }
  if (f.invariant && last_out_entry_) {
    entry.caused_by.push_back(*last_out_entry_);
  }
  entry.t_ns = count_ns(f.t);
  entry.layer = f.invariant ? "invariant" : "detector";
  entry.text = std::string("[") + severity_name(f.severity) + "] " + f.kind + " " + f.subject +
               ": " + f.message;
  entry.events = f.evidence;
  entry.finding = tmp_id;
  add(std::move(entry));
}

Json Timeline::result(const std::map<std::string, std::string>& finding_ids,
                      const std::map<std::string, std::vector<std::string>>& related) const {
  std::vector<std::size_t> order(entries_.size());
  std::iota(order.begin(), order.end(), 0U);
  std::stable_sort(order.begin(), order.end(), [&](std::size_t a, std::size_t b) {
    return std::pair(entries_[a].t_ns, a) < std::pair(entries_[b].t_ns, b);
  });
  std::vector<std::string> new_id(entries_.size());
  for (std::size_t n = 0; n < order.size(); ++n) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "T%04zu", n + 1);
    new_id[order[n]] = buf;
  }
  Json out = Json::array();
  for (std::size_t old : order) {
    const TimelineEntry& e = entries_[old];
    std::vector<std::string> caused;
    for (std::size_t c : e.caused_by) {
      caused.push_back(new_id[c]);
    }
    std::sort(caused.begin(), caused.end());
    Json j = {{"t_ns", e.t_ns},
              {"layer", e.layer},
              {"text", e.text},
              {"caused_by", caused},
              {"events", e.events},
              {"id", new_id[old]},
              {"t_s", py_round(static_cast<double>(e.t_ns) / 1e9, 9)}};
    if (e.fault) {
      j["fault"] = *e.fault;
    }
    if (e.finding) {
      const std::string& fid = finding_ids.at(*e.finding);
      j["finding"] = fid;
      if (const auto r = related.find(fid); r != related.end() && !r->second.empty()) {
        j["related_faults"] = r->second;
      }
    }
    out.push_back(std::move(j));
  }
  return out;
}

}  // namespace blackboxrs::replay
