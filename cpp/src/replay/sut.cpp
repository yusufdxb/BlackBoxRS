#include "blackboxrs/replay/sut.hpp"

#include <algorithm>
#include <set>
#include <stdexcept>

#include "blackboxrs/evidence/bundle.hpp"

namespace blackboxrs::replay {
namespace {

const char* name_of(StopMode m) {
  return m == StopMode::state ? "state" : "source";
}
const char* name_of(NonFinitePolicy p) {
  return p == NonFinitePolicy::reject ? "reject" : "forward";
}
const char* name_of(PublishMode p) {
  return p == PublishMode::timer ? "timer" : "on_input";
}
const char* name_of(FreshnessClock c) {
  return c == FreshnessClock::receipt ? "receipt" : "source_timestamp";
}

std::vector<SourceSpec> helix_sources() {
  // HELIX src/helix_arbiter/config/arbiter.yaml: teleop 200, nav 50, 0.5 s.
  return {{"teleop", "/teleop/cmd_vel", 200, 0.5}, {"nav", "/nav/cmd_vel", 50, 0.5}};
}

double number(const Json& v, const std::string& key) {
  if (v.is_boolean() || !v.is_number()) {
    throw std::invalid_argument("sut override " + key + " must be a number");
  }
  return v.get<double>();
}

std::int64_t integer(const Json& v, const std::string& key) {
  if (v.is_boolean() || !(v.is_number_integer() || v.is_number_unsigned() || v.is_number_float())) {
    throw std::invalid_argument("sut override " + key + " must be an integer");
  }
  return static_cast<std::int64_t>(v.get<double>());
}

std::int64_t ns(double s) {
  return seconds_to_ns(s);
}

}  // namespace

// ---------------------------------------------------------------------------
// configuration
// ---------------------------------------------------------------------------

Json ArbiterConfig::to_json() const {
  Json srcs = Json::array();
  for (const auto& s : sources) {
    srcs.push_back({{"name", s.name},
                    {"topic", s.topic},
                    {"priority", s.priority},
                    {"timeout_s", s.timeout_s}});
  }
  return {{"preset", preset},
          {"sources", srcs},
          {"hold_topic", hold_topic},
          {"hold_timeout_s", hold_timeout_s},
          {"period_s", period_s},
          {"max_abs_linear", max_abs_linear},
          {"max_abs_angular", max_abs_angular},
          {"stop_mode", name_of(stop_mode)},
          {"stop_priority", stop_priority},
          {"stop_timeout_s", stop_timeout_s},
          {"nonfinite", name_of(nonfinite)},
          {"publish", name_of(publish)},
          {"freshness_clock", name_of(freshness_clock)}};
}

ArbiterConfig build_arbiter_config(const std::string& preset, const Json& overrides) {
  ArbiterConfig cfg;
  cfg.sources = helix_sources();
  if (preset == "helix_arbiter") {
    cfg.preset = preset;
  } else if (preset == "twist_mux_legacy") {
    // HELIX config/twist_mux.yaml: teleop 200 > helix_recovery 100 > navigation 50.
    cfg.preset = preset;
    cfg.stop_mode = StopMode::source;
    cfg.stop_priority = 100;
    cfg.stop_timeout_s = 0.5;
    cfg.nonfinite = NonFinitePolicy::forward;
    cfg.publish = PublishMode::on_input;
  } else {
    throw std::invalid_argument("unknown preset '" + preset +
                                "'; known: ['helix_arbiter', 'twist_mux_legacy']");
  }
  static const std::set<std::string> kOverridable{
      "sources",         "hold_topic",    "hold_timeout_s", "period_s",       "max_abs_linear",
      "max_abs_angular", "stop_priority", "stop_timeout_s", "freshness_clock"};
  if (!overrides.is_null() && !overrides.is_object()) {
    throw std::invalid_argument("sut overrides must be an object");
  }
  std::vector<std::string> bad;
  for (const auto& [k, v] : overrides.items()) {
    if (kOverridable.count(k) == 0U) {
      bad.push_back(k);
    }
  }
  if (!bad.empty()) {
    std::string list;
    for (const auto& b : bad) {
      list += (list.empty() ? "'" : ", '") + b + "'";
    }
    throw std::invalid_argument("sut overrides: unknown or fixed keys [" + list + "]");
  }
  for (const auto& [k, v] : overrides.items()) {
    if (k == "sources") {
      if (!v.is_array()) {
        throw std::invalid_argument("sut override sources must be a list");
      }
      cfg.sources.clear();
      for (const auto& s : v) {
        cfg.sources.push_back(
            {s.at("name").is_string() ? s.at("name").get<std::string>() : s.at("name").dump(),
             s.at("topic").get<std::string>(), integer(s.at("priority"), "priority"),
             number(s.at("timeout_s"), "timeout_s")});
      }
    } else if (k == "hold_topic") {
      cfg.hold_topic = v.get<std::string>();
    } else if (k == "hold_timeout_s") {
      cfg.hold_timeout_s = number(v, k);
    } else if (k == "period_s") {
      cfg.period_s = number(v, k);
    } else if (k == "max_abs_linear") {
      cfg.max_abs_linear = number(v, k);
    } else if (k == "max_abs_angular") {
      cfg.max_abs_angular = number(v, k);
    } else if (k == "stop_priority") {
      cfg.stop_priority = integer(v, k);
    } else if (k == "stop_timeout_s") {
      cfg.stop_timeout_s = number(v, k);
    } else if (k == "freshness_clock") {
      const std::string c = v.is_string() ? v.get<std::string>() : std::string();
      if (c == "receipt") {
        cfg.freshness_clock = FreshnessClock::receipt;
      } else if (c == "source_timestamp") {
        cfg.freshness_clock = FreshnessClock::source_timestamp;
      } else {
        throw std::invalid_argument("freshness_clock must be receipt or source_timestamp");
      }
    }
  }
  std::set<std::string> names;
  for (const auto& s : cfg.sources) {
    names.insert(s.name);
  }
  if (cfg.sources.empty() || names.size() != cfg.sources.size()) {
    throw std::invalid_argument("sources must be non-empty with unique names");
  }
  if (std::any_of(cfg.sources.begin(), cfg.sources.end(),
                  [](const SourceSpec& s) { return !(s.timeout_s > 0); }) ||
      !(cfg.hold_timeout_s > 0)) {
    throw std::invalid_argument("every timeout must be > 0");
  }
  if (!(cfg.period_s > 0)) {
    throw std::invalid_argument("period_s must be > 0");
  }
  return cfg;
}

// ---------------------------------------------------------------------------
// typed views
// ---------------------------------------------------------------------------

VelocityCommand twist_view(const MessageBody& m) {
  if (const auto* v = std::get_if<VelocityCommand>(&m.typed)) {
    return *v;
  }
  return std::get<VelocityCommand>(decode_payload(Role::cmd_vel_source, *m.data));
}

HoldState hold_view(const MessageBody& m) {
  if (const auto* h = std::get_if<HoldState>(&m.typed)) {
    return *h;
  }
  return std::get<HoldState>(decode_payload(Role::helix_hold, *m.data));
}

ArbiterStatus status_view(const MessageBody& m) {
  if (const auto* s = std::get_if<ArbiterStatus>(&m.typed)) {
    return *s;
  }
  return std::get<ArbiterStatus>(decode_payload(Role::arbiter_status, *m.data));
}

namespace {

// Python sut._hold_fields: None unless `hold` is a bool; epoch and seq are
// used only when both are integers.
struct HoldFields {
  bool hold = false;
  std::string fault_id;
  std::int64_t epoch = 0;
  std::int64_t seq = 0;
};

std::optional<HoldFields> hold_fields(const HoldState& h) {
  if (!flag_is_bool(h.hold)) {
    return std::nullopt;
  }
  HoldFields f;
  f.hold = h.hold == Flag::is_true;
  f.fault_id = h.fault_id.present ? h.fault_id.text : std::string();
  if (h.epoch.value && h.seq.value) {
    f.epoch = *h.epoch.value;
    f.seq = *h.seq.value;
  }
  return f;
}

}  // namespace

// ---------------------------------------------------------------------------
// reference model
// ---------------------------------------------------------------------------

ReferenceArbiter::ReferenceArbiter(ArbiterConfig cfg, std::int64_t wall0_ns)
    : cfg_(std::move(cfg)), wall0_ns_(wall0_ns) {
  for (const auto& s : cfg_.sources) {
    slots_.push_back(Slot{s, {}, {}, {}, {}, -1, {}});
  }
  if (cfg_.stop_mode == StopMode::source) {
    slots_.push_back(
        Slot{SourceSpec{"helix_recovery", cfg_.hold_topic, cfg_.stop_priority, cfg_.stop_timeout_s},
             {},
             {},
             {},
             {},
             -1,
             {}});
  }
  for (std::size_t i = 0; i < slots_.size(); ++i) {
    by_topic_[slots_[i].spec.topic] = i;  // a later source on one topic replaces an earlier one
  }
}

void ReferenceArbiter::on_event(const Event& e, ReplayTime t_rt) {
  const MessageBody* m = e.message();
  if (m == nullptr) {
    return;
  }
  const std::int64_t t = count_ns(t_rt);
  if (m->topic == cfg_.hold_topic) {
    on_hold(e, t);
    if (cfg_.stop_mode != StopMode::source) {
      return;
    }
  }
  const auto it = by_topic_.find(m->topic);
  if (it == by_topic_.end()) {
    return;
  }
  if (m->data == nullptr) {
    throw EvidenceError(e.eid + " on " + m->topic +
                        ": payload not stored; the reference arbiter cannot replay a command it "
                        "cannot read");
  }
  Slot& slot = slots_[it->second];
  std::array<Json, 6> values;
  if (slot.spec.name == "helix_recovery") {
    const auto f = hold_fields(hold_view(*m));
    if (!f || !f->hold) {
      return;  // only an asserted hold is a zero-twist input on this path
    }
    values.fill(Json(0.0));
  } else {
    for (std::size_t i = 0; i < kTwistAxes.size(); ++i) {
      const Json* v = get_path(*m->data, kTwistAxes[i]);
      values[i] = v != nullptr ? *v : Json();
    }
  }
  on_source(slot, values, e, t);
}

void ReferenceArbiter::on_source(Slot& slot, const std::array<Json, 6>& values, const Event& e,
                                 std::int64_t t) {
  std::array<Numeric, 6> nums{};
  bool bad = false;
  for (std::size_t i = 0; i < values.size(); ++i) {
    nums[i] = as_number(&values[i]);
    bad = bad || nums[i].problem();
  }
  std::optional<Command> cmd;
  RawCommand raw;
  if (cfg_.nonfinite == NonFinitePolicy::reject) {
    const bool within = !bad && std::abs(nums[0].value) <= cfg_.max_abs_linear &&
                        std::abs(nums[1].value) <= cfg_.max_abs_linear &&
                        std::abs(nums[5].value) <= cfg_.max_abs_angular;
    if (bad || !within) {
      slot.cmd.reset();  // P4: never fall back to an older value
      slot.raw.reset();
      slot.rx_ns.reset();
      slot.fresh_ref_ns.reset();
      ++rejected_count_;
      rejected_ = e.eid;
      return;
    }
    cmd = Command{nums[0].value + 0.0, nums[1].value + 0.0, nums[5].value + 0.0};
    raw = raw_of(*cmd);
  } else {
    // twist_mux forwards the payload unchanged, NaN and garbage included.
    if (!bad) {
      cmd = Command{nums[0].value + 0.0, nums[1].value + 0.0, nums[5].value + 0.0};
    }
    raw = RawCommand{values[0], values[1], values[5]};
  }
  ++order_;
  slot.cmd = cmd;
  slot.raw = raw;
  slot.rx_ns = t;
  slot.order = order_;
  slot.eid = e.eid;
  const MessageBody* m = e.message();
  if (cfg_.freshness_clock == FreshnessClock::source_timestamp && m->src) {
    // the publisher's clock mapped onto the replay clock through the arbiter
    // host's wall clock: skew between the two hosts shifts it
    slot.fresh_ref_ns = count_ns(*m->src) - wall0_ns_;
  } else {
    slot.fresh_ref_ns = t;
  }
  if (cfg_.publish == PublishMode::on_input && winner(t) == &slot) {
    publish(slot.cmd, slot.raw, e.eid);
  }
}

void ReferenceArbiter::on_hold(const Event& e, std::int64_t t) {
  const MessageBody* m = e.message();
  if (m->data == nullptr) {
    throw EvidenceError(e.eid + " on " + m->topic + ": hold payload not stored");
  }
  const auto f = hold_fields(hold_view(*m));
  if (!f) {
    ++hold_malformed_;
    return;
  }
  if (cfg_.stop_mode == StopMode::source) {
    return;  // legacy path: the hold only feeds the zero-twist input
  }
  if (hold_ && hold_fresh(t) &&
      std::pair(f->epoch, f->seq) <= std::pair(hold_->epoch, hold_->seq)) {
    ++hold_reordered_;  // P8
    return;
  }
  const bool before = effective_hold(t);
  hold_ = Hold{f->hold, f->fault_id, f->epoch, f->seq, t, e.eid};
  if (effective_hold(t) != before) {
    clear();  // P7
    ++hold_transitions_;
  }
}

bool ReferenceArbiter::hold_fresh(std::int64_t t) const {
  return hold_ && t - hold_->rx_ns <= ns(cfg_.hold_timeout_s);
}

bool ReferenceArbiter::effective_hold(std::int64_t t) const {
  return !hold_ || !hold_fresh(t) || hold_->hold;
}

void ReferenceArbiter::clear() {
  for (auto& s : slots_) {
    s.cmd.reset();
    s.raw.reset();
    s.rx_ns.reset();
    s.fresh_ref_ns.reset();
  }
}

bool ReferenceArbiter::fresh(const Slot& s, std::int64_t t) const {
  if (!s.raw || !s.fresh_ref_ns) {
    return false;
  }
  return t - *s.fresh_ref_ns <= ns(s.spec.timeout_s);
}

ReferenceArbiter::Slot* ReferenceArbiter::winner(std::int64_t t) {
  Slot* best = nullptr;
  for (auto& s : slots_) {
    if (!fresh(s, t)) {
      continue;
    }
    if (best == nullptr ||
        std::pair(s.spec.priority, s.order) > std::pair(best->spec.priority, best->order)) {
      best = &s;
    }
  }
  return best;
}

void ReferenceArbiter::publish(std::optional<Command> cmd, std::optional<RawCommand> raw,
                               const std::string& eid) {
  sink_ = cmd;
  sink_raw_ = std::move(raw);
  sink_eid_ = eid;
  published_now_ = true;
  ++published_;
  if (!eid.empty()) {
    pending_pub_ = eid;
  }
}

std::optional<Decision> ReferenceArbiter::take_publication(ReplayTime t) {
  std::string eid;
  std::swap(eid, pending_pub_);
  if (eid.empty()) {
    return std::nullopt;
  }
  const Slot* win = winner(count_ns(t));
  return Decision{
      t,  kReasonSource, win == nullptr ? "" : win->spec.name, sink_, sink_raw_, true, std::nullopt,
      eid};
}

Decision ReferenceArbiter::tick(ReplayTime t) {
  std::string rejected;
  std::swap(rejected, rejected_);
  if (cfg_.stop_mode == StopMode::state) {
    return tick_state(count_ns(t), rejected);
  }
  return tick_source(count_ns(t));
}

Decision ReferenceArbiter::tick_state(std::int64_t t, const std::string& cause) {
  // The decision's cause is the input that determines it: the winning
  // source's message, the hold message, a just-rejected message, or the clock
  // (a timeout, or nothing received yet).
  if (effective_hold(t)) {
    clear();
  }
  std::optional<bool> hold;
  if (hold_) {
    hold = hold_->hold;
  }
  std::string reason;
  std::string src;
  Command cmd;
  std::string why;
  if (!hold_) {
    reason = kReasonMissing;
    why = "clock";
  } else if (!hold_fresh(t)) {
    reason = kReasonStale;
    why = "clock";
  } else if (hold_->hold) {
    reason = kReasonHold;
    why = hold_->eid;
  } else if (const Slot* win = winner(t); win == nullptr) {
    reason = kReasonNoInput;
    why = cause.empty() ? "clock" : cause;
  } else {
    reason = kReasonSource;
    src = win->spec.name;
    cmd = *win->cmd;
    why = win->eid;
  }
  publish(cmd, raw_of(cmd), "");
  published_now_ = false;
  return Decision{replay_ns(t), reason, src, cmd, raw_of(cmd), true, hold, why};
}

Decision ReferenceArbiter::tick_source(std::int64_t t) {
  const Slot* win = winner(t);
  const bool published = published_now_;
  published_now_ = false;
  std::string cause = "clock";
  if (published || win != nullptr) {
    cause = sink_eid_.empty() ? "clock" : sink_eid_;
  }
  return Decision{replay_ns(t),
                  win != nullptr ? kReasonSource : kReasonSilent,
                  win == nullptr ? "" : win->spec.name,
                  sink_,
                  sink_raw_,
                  published,
                  std::nullopt,
                  cause};
}

Json ReferenceArbiter::state() const {
  return {{"counters",
           {{"rejected", rejected_count_},
            {"hold_reordered", hold_reordered_},
            {"hold_transitions", hold_transitions_},
            {"hold_malformed", hold_malformed_},
            {"published", published_}}}};
}

// ---------------------------------------------------------------------------
// observed output
// ---------------------------------------------------------------------------

ObservedOutput::ObservedOutput(const std::map<std::string, std::vector<std::string>>& by_role) {
  if (const auto it = by_role.find("cmd_vel_out"); it != by_role.end()) {
    out_topics_ = it->second;
  }
  if (const auto it = by_role.find("arbiter_status"); it != by_role.end()) {
    status_topics_ = it->second;
  }
  use_status_ = out_topics_.empty();
  if (!use_status_) {
    source_label_ = "/cmd_vel (recorded)";
  } else if (!status_topics_.empty()) {
    source_label_ = "ArbiterStatus out_* (recorded)";
  }
}

void ObservedOutput::on_event(const Event& e, ReplayTime) {
  const MessageBody* m = e.message();
  if (m == nullptr || m->data == nullptr) {
    return;
  }
  auto contains = [](const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
  };
  if (contains(status_topics_, m->topic)) {
    const ArbiterStatus s = status_view(*m);
    reason_ = s.reason.present ? s.reason.text : "";
    src_ = s.selected_source.present ? s.selected_source.text : "";
    hold_.reset();
    if (flag_is_bool(s.hold_active)) {
      hold_ = s.hold_active == Flag::is_true;
    }
    if (use_status_) {
      set(s.out_raw, e);
    }
  } else if (contains(out_topics_, m->topic)) {
    set(twist_view(*m).forwarded, e);
  }
}

void ObservedOutput::set(const RawCommand& raw, const Event& e) {
  raw_ = raw;
  const Numeric a = as_number(&raw[0]);
  const Numeric b = as_number(&raw[1]);
  const Numeric c = as_number(&raw[2]);
  if (a.problem() || b.problem() || c.problem()) {
    cmd_.reset();
  } else {
    cmd_ = Command{a.value + 0.0, b.value + 0.0, c.value + 0.0};
  }
  published_ = true;
  cause_ = e.eid;
  pending_ = e.eid;
}

std::optional<Decision> ObservedOutput::take_publication(ReplayTime t) {
  std::string eid;
  std::swap(eid, pending_);
  if (eid.empty()) {
    return std::nullopt;
  }
  return Decision{t, reason_.empty() ? "RECORDED" : reason_, src_, cmd_, raw_, true, hold_, eid};
}

Decision ObservedOutput::tick(ReplayTime t) {
  const bool published = published_;
  published_ = false;
  std::string cause;
  std::swap(cause, cause_);
  return Decision{t,     reason_.empty() ? "RECORDED" : reason_, src_, cmd_, raw_, published,
                  hold_, cause.empty() ? "clock" : cause};
}

Json ObservedOutput::state() const {
  return {{"output_source", source_label_ ? Json(*source_label_) : Json()}};
}

// ---------------------------------------------------------------------------

RawCommand raw_of(const Command& c) {
  return {Json(c.vx), Json(c.vy), Json(c.wz)};
}

Json raw_json(const std::optional<RawCommand>& raw) {
  if (!raw) {
    return nullptr;
  }
  return Json::array({(*raw)[0], (*raw)[1], (*raw)[2]});
}

}  // namespace blackboxrs::replay
