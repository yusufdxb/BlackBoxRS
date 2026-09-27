#include "blackboxrs/replay/monitors.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>

#include "blackboxrs/replay/sut.hpp"

namespace blackboxrs::replay {

// ---------------------------------------------------------------------------
// formatting helpers (display text only; nothing branches on it)
// ---------------------------------------------------------------------------

std::string fmt_fixed(double v, int decimals) {
  char buf[64];
  std::snprintf(buf, sizeof buf, "%.*f", decimals, v);
  return buf;
}

std::string fmt_signed_fixed(double v, int decimals) {
  char buf[64];
  std::snprintf(buf, sizeof buf, "%+.*f", decimals, v);
  return buf;
}

std::string fmt_g(double v) {
  char buf[64];
  std::snprintf(buf, sizeof buf, "%g", v);
  return buf;
}

namespace {

// Python repr() of a JSON scalar as it appears inside a printed list.
std::string py_repr(const Json& v) {
  if (v.is_null()) {
    return "None";
  }
  if (v.is_boolean()) {
    return v.get<bool>() ? "True" : "False";
  }
  if (v.is_string()) {
    return "'" + v.get<std::string>() + "'";
  }
  if (v.is_number_float()) {
    const double d = v.get<double>();
    if (std::isnan(d)) {
      return "nan";
    }
    if (std::isinf(d)) {
      return d > 0 ? "inf" : "-inf";
    }
  }
  return v.dump();
}

std::string join_list(const std::vector<std::string>& parts) {
  std::string s = "[";
  for (std::size_t i = 0; i < parts.size(); ++i) {
    s += (i != 0U ? ", " : "") + parts[i];
  }
  return s + "]";
}

bool nonzero(const std::optional<RawCommand>& raw) {
  if (!raw) {
    return false;
  }
  for (const Json& v : *raw) {
    const Numeric n = as_number(&v);
    if (n.problem() || n.value != 0.0) {
      return true;
    }
  }
  return false;
}

// (vx, vy, wz), zero where a component is not finite, and whether any of the
// six components is not a finite number (Python monitors._twist_values).
std::pair<std::array<double, 3>, bool> twist_values(const VelocityCommand& v) {
  bool bad = false;
  for (const auto& a : v.axes) {
    bad = bad || a.problem();
  }
  auto pick = [](const Numeric& n) { return n.problem() ? 0.0 : n.value; };
  return {{pick(v.lx()), pick(v.ly()), pick(v.az())}, bad};
}

// ("", {}) when all six are finite, else the worst problem and the axes.
std::pair<std::string, std::vector<std::string>> twist_problem(const VelocityCommand& v) {
  std::vector<std::string> bad;
  std::string worst;
  for (std::size_t i = 0; i < v.axes.size(); ++i) {
    const NumState s = v.axes[i].state;
    if (s != NumState::ok) {
      bad.emplace_back(kTwistAxes[i]);
      worst = (s == NumState::malformed || worst == "malformed") ? "malformed" : "nonfinite";
    }
  }
  return {worst, bad};
}

bool close3(const std::array<double, 3>& a, const std::array<double, 3>& b) {
  for (std::size_t i = 0; i < 3; ++i) {
    if (!(std::abs(a[i] - b[i]) <= 1e-9)) {
      return false;
    }
  }
  return true;
}

Json json3(const std::array<double, 3>& v) {
  return Json::array({v[0], v[1], v[2]});
}

}  // namespace

std::string py_list(const std::optional<RawCommand>& raw) {
  if (!raw) {
    return "[]";
  }
  return join_list({py_repr((*raw)[0]), py_repr((*raw)[1]), py_repr((*raw)[2])});
}

std::string py_list(const std::array<double, 3>& v) {
  return join_list({py_repr(Json(v[0])), py_repr(Json(v[1])), py_repr(Json(v[2]))});
}

std::int64_t median_low(std::vector<std::int64_t> v) {
  std::sort(v.begin(), v.end());
  return v[(v.size() - 1) / 2];
}

// ---------------------------------------------------------------------------
// hold tracking and stop dominance
// ---------------------------------------------------------------------------

bool HoldTracker::update(const Event& e) {
  const MessageBody* m = e.message();
  if (m == nullptr || m->data == nullptr) {
    return false;
  }
  const HoldState h = hold_view(*m);
  if (!flag_is_bool(h.hold)) {
    return false;
  }
  const bool hold = h.hold == Flag::is_true;
  std::optional<std::pair<std::int64_t, std::int64_t>> key;
  if (h.epoch.value && h.seq.value) {
    key = std::pair(*h.epoch.value, *h.seq.value);
  }
  const bool fresh = last_rx_ && e.t_ns() - *last_rx_ <= timeout_ns_;
  if (key && key_ && *key <= *key_ && fresh) {
    ++ignored_older_;
    return false;
  }
  if (key) {
    key_ = key;
  }
  last_rx_ = e.t_ns();
  if (hold && !held_) {
    held_ = true;
    assert_t_ = e.t_ns();
    assert_eid_ = e.eid;
    fault_id_ = h.fault_id.present ? h.fault_id.text : "";
    ever_asserted_ = true;
    return true;
  }
  if (!hold && held_) {
    held_ = false;
    return true;
  }
  return false;
}

StopDominance::StopDominance(std::set<std::string> hold_topics, std::set<std::string> source_topics,
                             double grace_s, double hold_timeout_s)
    : hold_topics_(std::move(hold_topics)),
      source_topics_(std::move(source_topics)),
      grace_s_(grace_s),
      grace_ns_(seconds_to_ns(grace_s)),
      timeout_ns_(seconds_to_ns(hold_timeout_s)),
      delivered_(timeout_ns_),
      recorded_(timeout_ns_) {
  reset();
}

void StopDominance::reset() {
  delivered_ = HoldTracker(timeout_ns_);
  recorded_ = HoldTracker(timeout_ns_);
  inv_ = InvariantState{};
  inv_.name = "stop_dominance";
  inv_.statement =
      "while a HELIX hold is asserted (in the delivered stream or in the evidence as recorded), "
      "every robot-facing command from " +
      fmt_g(grace_s_) + " s after the hold is received until its release is zero";
  in_violation_ = false;
  asked_.clear();
}

const HoldTracker* StopDominance::active() const {
  const HoldTracker* best = nullptr;
  for (const HoldTracker* t : {&delivered_, &recorded_}) {
    if (t->held() && (best == nullptr || t->assert_t() < best->assert_t())) {
      best = t;
    }
  }
  return best;
}

void StopDominance::on_recorded(const Event& e) {
  const MessageBody* m = e.message();
  if (m != nullptr && hold_topics_.count(m->topic) != 0U) {
    if (recorded_.update(e) && active() == nullptr) {
      in_violation_ = false;
    }
  }
}

void StopDominance::on_event(const Event& e, Findings& out) {
  const MessageBody* m = e.message();
  if (m == nullptr || m->data == nullptr) {
    return;
  }
  if (hold_topics_.count(m->topic) != 0U) {
    if (delivered_.update(e)) {
      if (delivered_.held()) {
        asked_.clear();
      } else if (active() == nullptr) {
        in_violation_ = false;
      }
    }
    return;
  }
  const HoldTracker* act = active();
  if (act == nullptr || source_topics_.count(m->topic) == 0U) {
    return;
  }
  const auto [vals, bad] = twist_values(twist_view(*m));
  const bool motion = vals[0] != 0.0 || vals[1] != 0.0 || vals[2] != 0.0;
  if (!bad && motion && asked_.count(m->topic) == 0U) {
    asked_.insert(m->topic);
    Finding f;
    f.t = e.t;
    f.monitor = std::string(name());
    f.kind = "motion_request_while_held";
    f.severity = Severity::info;
    f.subject = m->topic;
    f.message = m->topic + " commands motion while hold " +
                (act->fault_id().empty() ? "(no id)" : act->fault_id()) + " is asserted";
    f.evidence = {e.eid, act->assert_eid()};
    out.push_back(std::move(f));
  }
}

void StopDominance::on_decision(const Decision& d, Findings& out) {
  const HoldTracker* act = active();
  if (act == nullptr || !act->assert_t() || count_ns(d.t) < *act->assert_t() + grace_ns_) {
    return;
  }
  inv_.exercised = true;
  ++inv_.checks;
  if (!nonzero(d.robot_raw)) {
    in_violation_ = false;
    return;
  }
  inv_.record_violation(d.t);
  if (in_violation_) {
    return;
  }
  in_violation_ = true;
  ++inv_.episodes;
  const bool from_recorded = act == &recorded_ && !delivered_.held();
  const std::string which = from_recorded ? "recorded" : "delivered";
  Finding f;
  f.t = d.t;
  f.monitor = std::string(name());
  f.kind = "stop_violated";
  f.severity = Severity::critical;
  f.subject = "robot_output";
  f.message = "robot-facing command " + py_list(d.robot_raw) + " is nonzero while hold " +
              (act->fault_id().empty() ? "(no id)" : act->fault_id()) +
              " is asserted (winner: " + (d.source.empty() ? "none" : d.source) + ")" +
              (from_recorded ? " (the hold is in the evidence as recorded; the replayed stream "
                               "lost or altered it)"
                             : "");
  for (const std::string& x : {act->assert_eid(), d.cause}) {
    if (!x.empty() && x != "clock") {
      f.evidence.push_back(x);
    }
  }
  f.invariant = "stop_dominance";
  f.data = {{"robot_command", d.robot_raw ? raw_json(d.robot_raw) : Json::array()},
            {"arbiter_reason", d.reason},
            {"winner", d.source},
            {"hold_stream", which}};
  out.push_back(std::move(f));
}

void StopDominance::finish(ReplayTime, Findings&) {
  if ((delivered_.ever_asserted() || recorded_.ever_asserted()) && inv_.checks == 0) {
    inv_.incomplete_reason = "a hold was asserted but no robot-facing command was sampled after it";
  }
}

// ---------------------------------------------------------------------------
// command freshness, finiteness, input validity
// ---------------------------------------------------------------------------

CommandPath::CommandPath(std::map<std::string, double> sources, double grace_s)
    : sources_s_(std::move(sources)), grace_s_(grace_s), grace_ns_(seconds_to_ns(grace_s)) {
  for (const auto& [topic, s] : sources_s_) {
    sources_[topic] = seconds_to_ns(s);
  }
  reset();
}

void CommandPath::reset() {
  latest_.clear();
  replaced_.clear();
  bad_run_.clear();
  fresh_ = InvariantState{};
  fresh_.name = "fresh_output";
  fresh_.statement =
      "a nonzero robot-facing command equals the latest valid message of a command source "
      "received within that source's freshness window (+" +
      fmt_g(grace_s_) + " s), or one it replaced less than " + fmt_g(grace_s_) + " s ago";
  finite_ = InvariantState{};
  finite_.name = "finite_output";
  finite_.statement = "every robot-facing command component is a finite number";
  stale_ep_ = false;
  nonfinite_ep_ = false;
  outputs_seen_ = 0;
}

void CommandPath::on_event(const Event& e, Findings& out) {
  const MessageBody* m = e.message();
  if (m == nullptr || sources_.count(m->topic) == 0U || m->data == nullptr) {
    return;
  }
  const VelocityCommand v = twist_view(*m);
  const auto [worst, bad] = twist_problem(v);
  if (!worst.empty()) {
    set_latest(m->topic, Latest{e.t_ns(), e.eid, false, std::nullopt, false}, e.t_ns());
    if (!bad_run_[m->topic]) {
      bad_run_[m->topic] = true;
      std::string axes;
      for (std::size_t i = 0; i < bad.size(); ++i) {
        axes += (i != 0U ? ", " : "") + bad[i];
      }
      Finding f;
      f.t = e.t;
      f.monitor = std::string(name());
      f.kind = worst + "_input";
      f.severity = Severity::warning;
      f.subject = m->topic;
      f.message = m->topic + " carries " + (worst == "nonfinite" ? "NaN/Inf" : "non-numeric") +
                  " value(s) in " + axes;
      f.evidence = {e.eid};
      f.data = {{"fields", bad}};
      out.push_back(std::move(f));
    }
    return;
  }
  bad_run_[m->topic] = false;
  set_latest(m->topic, Latest{e.t_ns(), e.eid, true, twist_values(v).first, false}, e.t_ns());
}

void CommandPath::set_latest(const std::string& topic, Latest next, std::int64_t t) {
  auto& replaced = replaced_[topic];
  std::erase_if(replaced, [&](const auto& r) { return r.second < t - grace_ns_; });
  if (const auto it = latest_.find(topic); it != latest_.end()) {
    replaced.emplace_back(it->second, t);
    it->second = std::move(next);
  } else {
    latest_.emplace(topic, std::move(next));
  }
}

bool CommandPath::justified(const std::array<double, 3>& cmd, std::int64_t t) const {
  for (const auto& [topic, window] : sources_) {
    auto backs = [&](const Latest& m) {
      return m.valid && m.cmd && t - m.t_ns <= window + grace_ns_ && close3(*m.cmd, cmd);
    };
    if (const auto it = latest_.find(topic); it != latest_.end() && backs(it->second)) {
      return true;
    }
    // A message replaced less than grace ago: the arbiter has not yet had a
    // tick to publish its successor.
    if (const auto it = replaced_.find(topic); it != replaced_.end()) {
      for (const auto& [m, at] : it->second) {
        if (at >= t - grace_ns_ && backs(m)) {
          return true;
        }
      }
    }
  }
  return false;
}

void CommandPath::on_decision(const Decision& d, Findings& out) {
  const std::int64_t t = count_ns(d.t);
  // sources that went silent while their last command was motion
  for (const auto& [topic, window] : sources_) {
    const auto it = latest_.find(topic);
    if (it == latest_.end()) {
      continue;
    }
    Latest& last = it->second;
    if (last.valid && last.cmd &&
        ((*last.cmd)[0] != 0.0 || (*last.cmd)[1] != 0.0 || (*last.cmd)[2] != 0.0) &&
        !last.stale_reported && t - last.t_ns > window + grace_ns_) {
      last.stale_reported = true;
      const double silent = ns_to_seconds(t - last.t_ns);
      Finding f;
      f.t = d.t;
      f.monitor = std::string(name());
      f.kind = "command_source_stale";
      f.severity = Severity::warning;
      f.subject = topic;
      f.message = topic + " last commanded " + py_list(*last.cmd) + " and has been silent " +
                  fmt_fixed(silent, 3) + " s (window " + fmt_g(ns_to_seconds(window)) + " s)";
      f.evidence = {last.eid};
      f.data = {{"last_command", json3(*last.cmd)}, {"silent_s", silent}};
      out.push_back(std::move(f));
    }
  }
  if (!d.robot_raw) {
    return;
  }
  ++outputs_seen_;
  finite_.exercised = true;
  ++finite_.checks;
  std::array<Numeric, 3> nums{};
  bool any_problem = false;
  for (std::size_t i = 0; i < 3; ++i) {
    nums[i] = as_number(&(*d.robot_raw)[i]);
    any_problem = any_problem || nums[i].problem();
  }
  if (any_problem) {
    finite_.record_violation(d.t);
    if (!nonfinite_ep_) {
      nonfinite_ep_ = true;
      ++finite_.episodes;
      Finding f;
      f.t = d.t;
      f.monitor = std::string(name());
      f.kind = "nonfinite_output";
      f.severity = Severity::critical;
      f.subject = "robot_output";
      f.message = "robot-facing command " + py_list(d.robot_raw) + " is not finite";
      if (d.cause != "clock") {
        f.evidence = {d.cause};
      }
      f.invariant = "finite_output";
      f.data = {{"robot_command", raw_json(d.robot_raw)}};
      out.push_back(std::move(f));
    }
    return;
  }
  nonfinite_ep_ = false;
  const std::array<double, 3> cmd{nums[0].value, nums[1].value, nums[2].value};
  if (cmd[0] == 0.0 && cmd[1] == 0.0 && cmd[2] == 0.0) {
    stale_ep_ = false;
    return;
  }
  fresh_.exercised = true;
  ++fresh_.checks;
  if (justified(cmd, t)) {
    stale_ep_ = false;
    return;
  }
  fresh_.record_violation(d.t);
  if (stale_ep_) {
    return;
  }
  stale_ep_ = true;
  ++fresh_.episodes;
  Json ages = Json::object();
  std::vector<std::string> ev;
  std::string matches;
  for (const auto& [topic, last] : latest_) {
    if (last.cmd && close3(*last.cmd, cmd)) {
      const double age = ns_to_seconds(t - last.t_ns);
      ages[topic] = age;
      ev.push_back(last.eid);
      matches += (matches.empty() ? "" : ", ") + topic + " aged " + fmt_fixed(age, 3) + " s";
    }
  }
  if (d.cause != "clock") {
    ev.push_back(d.cause);
  }
  Finding f;
  f.t = d.t;
  f.monitor = std::string(name());
  f.kind = "stale_command_forwarded";
  f.severity = Severity::critical;
  f.subject = "robot_output";
  f.message = "robot-facing command " + py_list(cmd) +
              " is not backed by a fresh valid source message" +
              (matches.empty() ? "" : " (matches " + matches + ")");
  f.evidence = std::move(ev);
  f.invariant = "fresh_output";
  f.data = {{"robot_command", json3(cmd)}, {"matching_source_age_s", ages}};
  out.push_back(std::move(f));
}

void CommandPath::finish(ReplayTime, Findings&) {
  if (outputs_seen_ == 0) {
    const std::string reason = "no robot-facing command was observed";
    fresh_.incomplete_reason = reason;
    finite_.incomplete_reason = reason;
  }
}

// ---------------------------------------------------------------------------
// arbiter self-consistency
// ---------------------------------------------------------------------------

ConsistentState::ConsistentState(std::set<std::string> status_topics)
    : status_topics_(std::move(status_topics)) {
  reset();
}

void ConsistentState::reset() {
  inv_ = InvariantState{};
  inv_.name = "consistent_state";
  inv_.statement = "an arbiter status never reports the hold active together with a nonzero output";
  ep_ = false;
}

void ConsistentState::on_event(const Event& e, Findings& out) {
  const MessageBody* m = e.message();
  if (m == nullptr || status_topics_.count(m->topic) == 0U || m->data == nullptr) {
    return;
  }
  const ArbiterStatus s = status_view(*m);
  inv_.exercised = true;
  ++inv_.checks;
  const std::optional<RawCommand> raw = s.out_raw;
  if (s.hold_active == Flag::is_true && nonzero(raw)) {
    inv_.record_violation(e.t);
    if (!ep_) {
      ep_ = true;
      ++inv_.episodes;
      Finding f;
      f.t = e.t;
      f.monitor = std::string(name());
      f.kind = "contradictory_state";
      f.severity = Severity::critical;
      f.subject = m->topic;
      f.message = m->topic + " reports hold_active with output " + py_list(raw);
      f.evidence = {e.eid};
      f.invariant = "consistent_state";
      const Json* reason = m->data->contains("reason") ? &(*m->data)["reason"] : nullptr;
      f.data = {{"out", raw_json(raw)}, {"reason", reason != nullptr ? *reason : Json()}};
      out.push_back(std::move(f));
    }
    return;
  }
  ep_ = false;
}

// ---------------------------------------------------------------------------
// publisher timestamps against receipt
// ---------------------------------------------------------------------------

ClockMonitor::ClockMonitor(std::map<std::string, std::string> hosts, double step_threshold_s,
                           double offset_info_s)
    : hosts_(std::move(hosts)),
      thr_(seconds_to_ns(step_threshold_s)),
      offset_info_(seconds_to_ns(offset_info_s)) {}

void ClockMonitor::reset() {
  samples_.clear();
  baseline_.clear();
  baseline_t_.clear();
  episode_.clear();
  untimed_.clear();
}

void ClockMonitor::on_event(const Event& e, Findings& out) {
  const MessageBody* m = e.message();
  if (m == nullptr || m->topic.empty()) {
    return;
  }
  if (!m->src || !m->rx_wall) {
    untimed_.insert(m->topic);
    return;
  }
  const std::int64_t lat = count_ns(*m->rx_wall) - count_ns(*m->src);
  const std::string& topic = m->topic;
  const auto base = baseline_.find(topic);
  if (base == baseline_.end()) {
    auto& s = samples_[topic];
    s.push_back(lat);
    if (s.size() >= kBaselineSamples) {
      baseline_[topic] = median_low(s);
      baseline_t_[topic] = e.t_ns();
    }
    return;
  }
  const std::int64_t dev = lat - base->second;
  const std::string state = dev < -thr_ ? "ahead" : dev > thr_ ? "behind" : "";
  std::string& prev = episode_[topic];
  const std::string before = prev;
  prev = state;
  if (state.empty() || state == before) {
    return;
  }
  const std::string why =
      state == "ahead"
          ? "the publisher clock stepped forward, or the receiver clock stepped back"
          : "late or repeated delivery, the publisher clock stepped back, or the receiver clock "
            "stepped forward";
  const auto host_it = hosts_.find(topic);
  const std::string host = host_it == hosts_.end() ? "unknown" : host_it->second;
  Finding f;
  f.t = e.t;
  f.monitor = std::string(name());
  f.kind = "stamp_" + state;
  f.severity = Severity::warning;
  f.subject = topic;
  f.message = topic + ": receipt minus source time moved " +
              fmt_signed_fixed(ns_to_seconds(dev), 3) + " s from its baseline (" +
              fmt_signed_fixed(ns_to_seconds(base->second), 3) + " s); " + why;
  f.evidence = {e.eid};
  f.data = {{"deviation_s", ns_to_seconds(dev)},
            {"baseline_s", ns_to_seconds(base->second)},
            {"host", host}};
  out.push_back(std::move(f));
}

void ClockMonitor::finish(ReplayTime, Findings& out) {
  std::map<std::string, std::vector<std::string>> by_host;
  for (const auto& [topic, b] : baseline_) {
    const auto h = hosts_.find(topic);
    by_host[h == hosts_.end() ? "unknown" : h->second].push_back(topic);
  }
  for (const auto& [host, topics] : by_host) {
    std::vector<std::int64_t> offs;
    for (const auto& t : topics) {
      offs.push_back(-baseline_.at(t));
    }
    const std::int64_t off = median_low(offs);
    if (std::llabs(off) > offset_info_) {
      std::int64_t t0 = INT64_MAX;
      for (const auto& t : topics) {
        t0 = std::min(t0, baseline_t_.at(t));
      }
      Finding f;
      f.t = replay_ns(t0);
      f.monitor = std::string(name());
      f.kind = "clock_offset";
      f.severity = Severity::info;
      f.subject = host;
      f.message = "host " + host + ": publisher clock is " +
                  fmt_signed_fixed(ns_to_seconds(off), 3) +
                  " s from the receiver clock from the start of the evidence (constant offset, "
                  "not a step)";
      f.data = {{"offset_s", ns_to_seconds(off)}, {"topics", topics}};
      out.push_back(std::move(f));
    }
  }
}

Json ClockMonitor::summary() const {
  Json base = Json::object();
  for (const auto& [t, v] : baseline_) {
    base[t] = ns_to_seconds(v);
  }
  return {{"baseline_receipt_minus_source_s", base},
          {"topics_without_source_timestamps",
           std::vector<std::string>(untimed_.begin(), untimed_.end())}};
}

// ---------------------------------------------------------------------------
// odometry consistency
// ---------------------------------------------------------------------------

OdometryConsistency::OdometryConsistency(std::set<std::string> topics, double moving_mps,
                                         int frozen_samples, double jump_tolerance_m)
    : topics_(std::move(topics)),
      moving_(moving_mps),
      frozen_n_(frozen_samples),
      tol_(jump_tolerance_m) {}

void OdometryConsistency::reset() {
  prev_.clear();
  frozen_.clear();
  bad_.clear();
  jump_.clear();
}

void OdometryConsistency::on_event(const Event& e, Findings& out) {
  const MessageBody* m = e.message();
  if (m == nullptr || topics_.count(m->topic) == 0U || m->data == nullptr) {
    return;
  }
  const std::string& topic = m->topic;
  const Odometry o = std::holds_alternative<Odometry>(m->typed)
                         ? std::get<Odometry>(m->typed)
                         : std::get<Odometry>(decode_payload(Role::odometry, *m->data));
  std::vector<std::string> problems;
  const std::array<std::pair<const char*, const Numeric*>, 4> fields{{
      {"pose.pose.position.x", &o.x},
      {"pose.pose.position.y", &o.y},
      {"twist.twist.linear.x", &o.vx},
      {"twist.twist.linear.y", &o.vy},
  }};
  for (const auto& [path, n] : fields) {
    if (n->problem()) {
      problems.emplace_back(path);
    }
  }
  if (!problems.empty()) {
    prev_.erase(topic);
    if (!bad_[topic]) {
      bad_[topic] = true;
      std::string list;
      for (std::size_t i = 0; i < problems.size(); ++i) {
        list += (i != 0U ? ", " : "") + problems[i];
      }
      Finding f;
      f.t = e.t;
      f.monitor = std::string(name());
      f.kind = "nonfinite_telemetry";
      f.severity = Severity::warning;
      f.subject = topic;
      f.message = topic + ": " + list + " not a finite number";
      f.evidence = {e.eid};
      f.data = {{"fields", problems}};
      out.push_back(std::move(f));
    }
    return;
  }
  bad_[topic] = false;
  const double t = m->pub_stamp_s ? *m->pub_stamp_s : static_cast<double>(e.t_ns()) / 1e9;
  const Sample cur{t, o.x.value, o.y.value,
                   std::pow(o.vx.value * o.vx.value + o.vy.value * o.vy.value, 0.5)};
  const auto pit = prev_.find(topic);
  const std::optional<Sample> prev =
      pit == prev_.end() ? std::nullopt : std::optional<Sample>(pit->second);
  prev_[topic] = cur;
  if (!prev || cur.t <= prev->t) {
    return;
  }
  const double dt = cur.t - prev->t;
  const double dx = cur.x - prev->x;
  const double dy = cur.y - prev->y;
  const double dp = std::pow(dx * dx + dy * dy, 0.5);
  const double v = (prev->speed + cur.speed) / 2;
  if (dp > 2 * v * dt + tol_) {
    if (!jump_[topic]) {
      Finding f;
      f.t = e.t;
      f.monitor = std::string(name());
      f.kind = "odometry_jump";
      f.severity = Severity::warning;
      f.subject = topic;
      f.message = topic + ": position moved " + fmt_fixed(dp, 3) + " m in " + fmt_fixed(dt, 3) +
                  " s while its twist says " + fmt_fixed(v, 3) + " m/s";
      f.evidence = {e.eid};
      f.data = {{"moved_m", dp}, {"dt_s", dt}, {"speed_mps", v}};
      out.push_back(std::move(f));
    }
    jump_[topic] = true;
  } else {
    jump_[topic] = false;
  }
  if (dp < 1e-9 && v > moving_) {
    if (++frozen_[topic] == frozen_n_) {
      Finding f;
      f.t = e.t;
      f.monitor = std::string(name());
      f.kind = "odometry_frozen";
      f.severity = Severity::warning;
      f.subject = topic;
      f.message = topic + ": position unchanged for " + std::to_string(frozen_n_) +
                  " samples while its twist says " + fmt_fixed(v, 3) +
                  " m/s (stuck sensor or frozen publisher)";
      f.evidence = {e.eid};
      f.data = {{"samples", frozen_n_}, {"speed_mps", v}};
      out.push_back(std::move(f));
    }
  } else {
    frozen_[topic] = 0;
  }
}

}  // namespace blackboxrs::replay
