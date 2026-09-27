#include "blackboxrs/replay/faults.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <set>

#include "blackboxrs/evidence/bundle.hpp"

namespace blackboxrs::replay {
namespace {

// ---------------------------------------------------------------------------
// kind table
// ---------------------------------------------------------------------------

ParamSpec opt(std::string name, ParamType t, Json def, std::string help) {
  return {std::move(name), t, false, std::move(def), std::move(help)};
}
ParamSpec req(std::string name, ParamType t, std::string help) {
  return {std::move(name), t, true, Json(), std::move(help)};
}

std::vector<ParamSpec> select_params() {
  return {opt("topic", ParamType::str, nullptr, "one topic"),
          opt("topics", ParamType::list, nullptr, "several topics"),
          opt("host", ParamType::str, nullptr,
              "every topic published from this host (payload, robot)")};
}
std::vector<ParamSpec> window_params() {
  return {
      opt("from_s", ParamType::f64, 0.0, "window start, seconds from evidence start"),
      opt("to_s", ParamType::f64, nullptr, "window end (exclusive); omitted = end of evidence")};
}
std::vector<ParamSpec> field_params() {
  return {opt("field", ParamType::str, nullptr, "dotted payload field"),
          opt("fields", ParamType::list, nullptr, "several dotted payload fields")};
}

KindSpec make_kind(FaultKind k, std::string name, std::string category, std::string summary,
                   std::vector<ParamSpec> params, bool selects = true, bool window = true) {
  std::vector<ParamSpec> all;
  if (selects) {
    for (auto& p : select_params()) {
      all.push_back(std::move(p));
    }
  }
  if (window) {
    for (auto& p : window_params()) {
      all.push_back(std::move(p));
    }
  }
  // Later declarations replace earlier ones with the same name (Python dict merge).
  for (auto& p : params) {
    auto it =
        std::find_if(all.begin(), all.end(), [&](const ParamSpec& q) { return q.name == p.name; });
    if (it != all.end()) {
      *it = std::move(p);
    } else {
      all.push_back(std::move(p));
    }
  }
  return {k, std::move(name), std::move(category), std::move(summary), std::move(all)};
}

std::vector<KindSpec> build_kinds() {
  using PT = ParamType;
  std::vector<KindSpec> k;
  k.push_back(make_kind(FaultKind::drop, "drop", "transport",
                        "drop messages (every_n=1: all) in the window",
                        {opt("every_n", PT::i64, 1, "drop every n-th selected message")}));
  k.push_back(make_kind(FaultKind::gap, "gap", "transport",
                        "temporary telemetry gap: nothing arrives in [from_s, to_s)", {}));
  k.push_back(make_kind(FaultKind::delay, "delay", "transport",
                        "deliver messages delay_s late (publisher stamps unchanged)",
                        {req("delay_s", PT::f64, "added receipt delay")}));
  k.push_back(make_kind(FaultKind::duplicate, "duplicate", "transport",
                        "deliver a second copy lag_s after the original",
                        {opt("every_n", PT::i64, 1, "duplicate every n-th message"),
                         opt("lag_s", PT::f64, 0.0002, "delay of the copy")}));
  k.push_back(make_kind(FaultKind::reorder, "reorder", "transport",
                        "swap the arrival of consecutive message pairs", {}));
  k.push_back(make_kind(FaultKind::stale_redelivery, "stale_redelivery", "transport",
                        "re-deliver at at_s the last message that is age_s old (stale command)",
                        {req("at_s", PT::f64, "delivery time"), req("age_s", PT::f64, "its age")},
                        true, false));
  k.push_back(make_kind(FaultKind::clock_skew, "clock_skew", "transport",
                        "offset publisher timestamps (DDS source + embedded) by offset_s from "
                        "from_s on",
                        {req("offset_s", PT::f64, "seconds added to publisher clocks")}));
  k.push_back(make_kind(FaultKind::timestamp_jump, "timestamp_jump", "transport",
                        "publisher timestamps step by jump_s at at_s",
                        {req("at_s", PT::f64, "jump time"), req("jump_s", PT::f64, "step size")},
                        true, false));
  k.push_back(
      make_kind(FaultKind::nan, "nan", "data", "set payload field(s) to NaN", field_params()));
  {
    auto p = field_params();
    p.push_back(opt("sign", PT::i64, 1, "1 or -1"));
    k.push_back(make_kind(FaultKind::inf, "inf", "data", "set payload field(s) to +Inf or -Inf",
                          std::move(p)));
  }
  {
    auto p = field_params();
    p.push_back(opt("value", PT::json, "not-a-number", "replacement value"));
    p.push_back(opt("remove", PT::boolean, false, "delete the field instead"));
    k.push_back(make_kind(FaultKind::malformed, "malformed", "data",
                          "replace field(s) with a non-numeric value, or remove them",
                          std::move(p)));
  }
  {
    auto p = field_params();
    p.push_back(req("value", PT::json, "value to write"));
    k.push_back(make_kind(FaultKind::set_value, "set_value", "data",
                          "overwrite field(s) with a given value", std::move(p)));
  }
  k.push_back(make_kind(FaultKind::freeze, "freeze", "data",
                        "hold field(s) at their last value before from_s (stuck sensor)",
                        field_params()));
  k.push_back(make_kind(
      FaultKind::step, "step", "data", "add delta to a numeric field from at_s on (discontinuity)",
      {req("field", PT::str, "dotted payload field"), req("delta", PT::f64, "step"),
       req("at_s", PT::f64, "step time"), opt("to_s", PT::f64, nullptr, "step end")},
      true, false));
  k.push_back(make_kind(
      FaultKind::node_exit, "node_exit", "transport",
      "a node leaves the ROS graph at at_s (crash or host down)",
      {req("node", PT::str, "fully qualified node name"), req("at_s", PT::f64, "exit time")}, false,
      false));
  k.push_back(make_kind(
      FaultKind::inject_stream, "inject_stream", "control",
      "publish a message stream that is not in the evidence (e.g. a teleop stream)",
      {req("topic", PT::str, "topic to publish on"),
       req("data", PT::json, "payload of every message"),
       opt("rate_hz", PT::f64, 20.0, "publish rate"), req("from_s", PT::f64, "first message"),
       req("to_s", PT::f64, "stream end"),
       opt("final_data", PT::json, nullptr, "one last payload published at to_s"),
       opt("role", PT::str, nullptr, "role (default: from the evidence)"),
       opt("type", PT::str, "geometry_msgs/msg/Twist", "message type"),
       opt("host", PT::str, nullptr, "publishing host (default: from the evidence, else payload)")},
      false, false));
  std::sort(k.begin(), k.end(),
            [](const KindSpec& a, const KindSpec& b) { return a.name < b.name; });
  return k;
}

// ---------------------------------------------------------------------------
// parameter coercion (Python faults._coerce)
// ---------------------------------------------------------------------------

std::string repr(const Json& v) {
  return v.is_string() ? "'" + v.get<std::string>() + "'" : v.dump();
}

[[noreturn]] void bad_param(const ParamSpec& spec, const Json& v) {
  static constexpr std::array<const char*, 6> kNames{"float", "int", "bool", "str", "list", "json"};
  throw FaultError("parameter " + spec.name + ": expected " +
                   kNames[static_cast<std::size_t>(spec.type)] + ", got " + repr(v));
}

std::optional<double> parse_float_text(const std::string& s) {
  try {
    std::size_t pos = 0;
    const double d = std::stod(s, &pos);
    if (pos != s.size()) {
      return std::nullopt;
    }
    return d;
  } catch (const std::exception&) {
    return std::nullopt;
  }
}

std::string lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return s;
}

Json coerce(const ParamSpec& spec, const Json& v) {
  if (v.is_null()) {
    return nullptr;
  }
  switch (spec.type) {
    case ParamType::f64: {
      double d = 0.0;
      if (v.is_number() && !v.is_boolean()) {
        d = v.get<double>();
      } else if (v.is_string()) {
        const auto p = parse_float_text(v.get<std::string>());
        if (!p) {
          bad_param(spec, v);
        }
        d = *p;
      } else {
        bad_param(spec, v);
      }
      if (!std::isfinite(d)) {
        bad_param(spec, v);
      }
      return d;
    }
    case ParamType::i64: {
      if (v.is_boolean()) {
        bad_param(spec, v);
      }
      double d = 0.0;
      if (v.is_number_integer() || v.is_number_unsigned()) {
        return v.get<std::int64_t>();
      }
      if (v.is_number_float()) {
        d = v.get<double>();
      } else if (v.is_string()) {
        const auto p = parse_float_text(v.get<std::string>());
        if (!p) {
          bad_param(spec, v);
        }
        d = *p;
      } else {
        bad_param(spec, v);
      }
      if (!std::isfinite(d) || d != std::trunc(d)) {
        bad_param(spec, v);
      }
      return static_cast<std::int64_t>(d);
    }
    case ParamType::boolean: {
      if (v.is_boolean()) {
        return v.get<bool>();
      }
      std::string s;
      if (v.is_string()) {
        s = lower(v.get<std::string>());
      } else if (v.is_number_integer() || v.is_number_unsigned()) {
        s = std::to_string(v.get<std::int64_t>());
      } else {
        bad_param(spec, v);
      }
      if (s == "true" || s == "1" || s == "yes") {
        return true;
      }
      if (s == "false" || s == "0" || s == "no") {
        return false;
      }
      bad_param(spec, v);
    }
    case ParamType::list: {
      Json out = Json::array();
      if (v.is_string()) {
        const std::string s = v.get<std::string>();
        std::size_t start = 0;
        while (start <= s.size()) {
          const auto bar = s.find('|', start);
          const std::string part =
              s.substr(start, bar == std::string::npos ? std::string::npos : bar - start);
          if (!part.empty()) {
            out.push_back(part);
          }
          if (bar == std::string::npos) {
            break;
          }
          start = bar + 1;
        }
        return out;
      }
      if (v.is_array() &&
          std::all_of(v.begin(), v.end(), [](const Json& x) { return x.is_string(); })) {
        return v;
      }
      bad_param(spec, v);
    }
    case ParamType::str:
      if (!v.is_string()) {
        bad_param(spec, v);
      }
      return v;
    case ParamType::json: return v;
  }
  return v;
}

// ---------------------------------------------------------------------------
// application context
// ---------------------------------------------------------------------------

struct Ctx {
  const Fault& fault;
  std::size_t index;
  TopicTable& topics;
  std::vector<Event> touched;
  std::map<std::int64_t, std::int64_t> subs;  // seq -> highest copy index

  Ctx(const Fault& f, std::size_t i, TopicTable& t, const std::vector<Event>& events)
      : fault(f), index(i), topics(t) {
    for (const Event& e : events) {
      if (e.order.origin == 0) {
        auto& s = subs[e.order.a];
        s = std::max(s, e.order.b);
      }
    }
  }
  std::int64_t next_sub(std::int64_t seq) { return ++subs[seq]; }
  Event mark(Event e) {
    e.touch(fault.id);
    touched.push_back(e);
    return e;
  }
  [[noreturn]] void fail(const std::string& msg) const { throw FaultError(msg); }
};

std::int64_t ns_of(double s) {
  return seconds_to_ns(s);
}

std::set<std::string> topics_of(const Fault& f, const TopicTable& topics) {
  const auto topic = f.opt_str("topic");
  const auto has_topics = !f.json("topics").is_null();
  const auto host = f.opt_str("host");
  const int chosen = (topic ? 1 : 0) + (has_topics ? 1 : 0) + (host ? 1 : 0);
  if (chosen != 1) {
    throw FaultError("give exactly one of topic, topics, host");
  }
  std::set<std::string> names;
  if (topic) {
    names.insert(*topic);
  } else if (has_topics) {
    for (const auto& t : f.list("topics")) {
      names.insert(t);
    }
  } else {
    for (const auto& [name, info] : topics) {
      if (info.host == *host) {
        names.insert(name);
      }
    }
    if (names.empty()) {
      throw FaultError("no topic in the evidence is published from host '" + *host + "'");
    }
  }
  std::vector<std::string> unknown;
  for (const auto& n : names) {
    if (topics.find(n) == topics.end()) {
      unknown.push_back(n);
    }
  }
  if (!unknown.empty()) {
    std::string s = "topics not in the evidence: [";
    for (std::size_t i = 0; i < unknown.size(); ++i) {
      s += (i != 0U ? ", '" : "'") + unknown[i] + "'";
    }
    throw FaultError(s + "]");
  }
  return names;
}

struct Window {
  std::int64_t lo = 0;
  std::optional<std::int64_t> hi;
  [[nodiscard]] bool contains(std::int64_t t) const { return t >= lo && (!hi || t < *hi); }
};

Window window_of(const Fault& f, std::optional<double> from_override = std::nullopt) {
  Window w;
  const auto from = from_override ? from_override : f.opt_f64("from_s");
  w.lo = ns_of(from.value_or(0.0));
  if (const auto to = f.opt_f64("to_s")) {
    w.hi = ns_of(*to);
  }
  return w;
}

std::vector<std::size_t> selected(const std::vector<Event>& events, const Fault& f, Ctx& ctx,
                                  const Window& w) {
  const auto names = topics_of(f, ctx.topics);
  std::vector<std::size_t> idx;
  for (std::size_t i = 0; i < events.size(); ++i) {
    const auto* m = events[i].message();
    if (m != nullptr && names.count(m->topic) != 0U && w.contains(events[i].t_ns())) {
      idx.push_back(i);
    }
  }
  return idx;
}

const Json& need_payload(const Event& e, const Ctx& ctx) {
  const auto* m = e.message();
  if (m == nullptr || m->data == nullptr) {
    ctx.fail(ctx.fault.id + ": " + e.eid + " on " + e.topic() +
             " has no stored payload (decimated by store_max_hz); cannot inject a data fault "
             "into it");
  }
  return *m->data;
}

Event shifted(Event e, std::int64_t d) {
  e.t = e.t + Nanos{d};
  auto* m = e.message();
  if (m != nullptr && m->rx_wall) {
    m->rx_wall = *m->rx_wall + Nanos{d};
  }
  return e;
}

// -- timing / transport ------------------------------------------------------

std::vector<Event> drop_every(std::vector<Event> events, const Fault& f, Ctx& ctx, std::int64_t n,
                              const Window& w) {
  if (n < 1) {
    ctx.fail("every_n must be >= 1");
  }
  const auto idx = selected(events, f, ctx, w);
  std::vector<bool> gone(events.size(), false);
  for (std::size_t k = 0; k < idx.size(); ++k) {
    if ((static_cast<std::int64_t>(k) + 1) % n == 0) {
      gone[idx[k]] = true;
    }
  }
  std::vector<Event> out;
  out.reserve(events.size());
  for (std::size_t i = 0; i < events.size(); ++i) {
    if (gone[i]) {
      ctx.mark(events[i]);
    } else {
      out.push_back(std::move(events[i]));
    }
  }
  return out;
}

std::vector<Event> fault_drop(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  return drop_every(std::move(events), f, ctx, f.i64("every_n"), window_of(f));
}

std::vector<Event> fault_gap(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  if (!f.opt_f64("to_s")) {
    ctx.fail("gap needs to_s (use drop for a loss that never recovers)");
  }
  return drop_every(std::move(events), f, ctx, 1, window_of(f));
}

std::vector<Event> fault_delay(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const std::int64_t d = ns_of(f.f64("delay_s"));
  if (d <= 0) {
    ctx.fail("delay_s must be > 0");
  }
  for (std::size_t i : selected(events, f, ctx, window_of(f))) {
    events[i] = ctx.mark(shifted(events[i], d));
  }
  return events;
}

std::vector<Event> fault_duplicate(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const std::int64_t lag = ns_of(f.f64("lag_s"));
  if (lag <= 0) {
    ctx.fail("lag_s must be > 0 (a copy at the same instant has no receipt order)");
  }
  const std::int64_t n = f.i64("every_n");
  const auto idx = selected(events, f, ctx, window_of(f));
  std::vector<Event> copies;
  for (std::size_t k = 0; k < idx.size(); ++k) {
    if (n == 0) {
      ctx.fail("every_n must not be 0");
    }
    if ((static_cast<std::int64_t>(k) + 1) % n != 0) {
      continue;
    }
    const Event& e = events[idx[k]];
    if (e.order.origin != 0) {
      ctx.fail("duplicate applies to evidence events only");
    }
    const std::int64_t sub = ctx.next_sub(e.order.a);
    Event dup = shifted(e, lag);
    dup.order = OrderKey{0, e.order.a, sub};
    dup.eid = e.eid + ".dup" + std::to_string(sub);
    copies.push_back(ctx.mark(std::move(dup)));
  }
  for (auto& c : copies) {
    events.push_back(std::move(c));
  }
  return events;
}

std::vector<Event> fault_reorder(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const auto idx = selected(events, f, ctx, window_of(f));
  const std::vector<Event> orig = events;
  for (std::size_t p = 0; p + 1 < idx.size(); p += 2) {
    const Event& ea = orig[idx[p]];
    const Event& eb = orig[idx[p + 1]];
    Event na = ea;
    Event nb = eb;
    na.t = eb.t;
    na.msg().rx_wall = eb.msg().rx_wall;
    nb.t = ea.t;
    nb.msg().rx_wall = ea.msg().rx_wall;
    events[idx[p]] = ctx.mark(std::move(na));
    events[idx[p + 1]] = ctx.mark(std::move(nb));
  }
  return events;
}

std::vector<Event> fault_stale_redelivery(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const std::int64_t at = ns_of(f.f64("at_s"));
  const std::int64_t age = ns_of(f.f64("age_s"));
  if (age <= 0) {
    ctx.fail("age_s must be > 0");
  }
  const auto names = topics_of(f, ctx.topics);
  const Event* src = nullptr;
  for (const Event& e : events) {
    const auto* m = e.message();
    if (m != nullptr && names.count(m->topic) != 0U && e.t_ns() <= at - age &&
        e.order.origin == 0) {
      src = &e;
    }
  }
  if (src == nullptr) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.3f", ns_to_seconds(at - age));
    std::string list = "[";
    bool first = true;
    for (const auto& n : names) {
      list += (first ? "'" : ", '") + n + "'";
      first = false;
    }
    ctx.fail("no message on " + list + "] at or before " + buf + " s");
  }
  const std::int64_t sub = ctx.next_sub(src->order.a);
  Event copy = shifted(*src, at - src->t_ns());
  copy.order = OrderKey{0, src->order.a, sub};
  copy.eid = src->eid + ".stale" + std::to_string(sub);
  Event marked = ctx.mark(std::move(copy));
  events.push_back(std::move(marked));
  return events;
}

// Shift the publisher clocks of a message; false when it carries none.
bool shift_stamps(Event& e, double off_s) {
  auto* m = e.message();
  bool changed = false;
  if (m->src) {
    m->src = *m->src + Nanos{ns_of(off_s)};
    changed = true;
  }
  if (m->pub_stamp_s) {
    *m->pub_stamp_s += off_s;
    changed = true;
  }
  return changed;
}

std::vector<Event> skew(std::vector<Event> events, const Fault& f, Ctx& ctx, double off_s,
                        const Window& w) {
  std::size_t hit = 0;
  for (std::size_t i : selected(events, f, ctx, w)) {
    Event e = events[i];
    if (shift_stamps(e, off_s)) {
      events[i] = ctx.mark(std::move(e));
      ++hit;
    }
  }
  if (hit == 0) {
    ctx.fail("clock_skew: no selected message carries a publisher timestamp");
  }
  return events;
}

std::vector<Event> fault_clock_skew(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  return skew(std::move(events), f, ctx, f.f64("offset_s"), window_of(f));
}

std::vector<Event> fault_timestamp_jump(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  Window w;
  w.lo = ns_of(f.f64("at_s"));
  return skew(std::move(events), f, ctx, f.f64("jump_s"), w);
}

// -- data --------------------------------------------------------------------

std::vector<std::string> field_list(const Fault& f) {
  std::vector<std::string> fields;
  if (const auto one = f.opt_str("field"); one && !one->empty()) {
    fields.push_back(*one);
  } else if (!f.json("fields").is_null()) {
    fields = f.list("fields");
  }
  if (fields.empty()) {
    throw FaultError("give field or fields");
  }
  return fields;
}

struct Removed {};
using FieldValue = std::variant<Json, Removed>;

std::vector<Event> set_fields(
    std::vector<Event> events, const Fault& f, Ctx& ctx, const Window& w,
    const std::function<FieldValue(const Event&, const std::string&)>& value_for) {
  const auto fields = field_list(f);
  for (std::size_t i : selected(events, f, ctx, w)) {
    const Event& e = events[i];
    Json data = need_payload(e, ctx);
    for (const auto& field : fields) {
      if (get_path(data, field) == nullptr) {
        ctx.fail(ctx.fault.id + ": field '" + field + "' not in " + e.topic() + " payload");
      }
      FieldValue v = value_for(e, field);
      if (std::holds_alternative<Removed>(v)) {
        data = del_path(std::move(data), field);
      } else {
        data = set_path(std::move(data), field, std::get<Json>(std::move(v)));
      }
    }
    Event changed = e;
    changed.msg().set_data(std::move(data));
    events[i] = ctx.mark(std::move(changed));
  }
  return events;
}

std::vector<Event> fault_nan(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  return set_fields(std::move(events), f, ctx, window_of(f), [](const Event&, const std::string&) {
    return FieldValue{Json(std::numeric_limits<double>::quiet_NaN())};
  });
}

std::vector<Event> fault_inf(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const std::int64_t sign = f.i64("sign");
  if (sign != 1 && sign != -1) {
    ctx.fail("sign must be 1 or -1");
  }
  const double v =
      sign > 0 ? std::numeric_limits<double>::infinity() : -std::numeric_limits<double>::infinity();
  return set_fields(std::move(events), f, ctx, window_of(f),
                    [v](const Event&, const std::string&) { return FieldValue{Json(v)}; });
}

std::vector<Event> fault_malformed(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  if (f.flag("remove")) {
    return set_fields(std::move(events), f, ctx, window_of(f),
                      [](const Event&, const std::string&) { return FieldValue{Removed{}}; });
  }
  const Json value = f.json("value");
  return set_fields(std::move(events), f, ctx, window_of(f),
                    [value](const Event&, const std::string&) { return FieldValue{value}; });
}

std::vector<Event> fault_set_value(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const Json value = f.json("value");
  return set_fields(std::move(events), f, ctx, window_of(f),
                    [value](const Event&, const std::string&) { return FieldValue{value}; });
}

std::vector<Event> fault_freeze(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const auto names = topics_of(f, ctx.topics);
  const Window w = window_of(f);
  const auto fields = field_list(f);
  std::map<std::string, Json> held;
  for (const Event& e : events) {
    const auto* m = e.message();
    if (m != nullptr && names.count(m->topic) != 0U && e.t_ns() < w.lo && m->data != nullptr) {
      for (const auto& field : fields) {
        if (const Json* v = get_path(*m->data, field)) {
          held[field] = *v;
        }
      }
    }
  }
  std::vector<std::string> missing;
  for (const auto& field : fields) {
    if (held.count(field) == 0U) {
      missing.push_back(field);
    }
  }
  if (!missing.empty()) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.3f", ns_to_seconds(w.lo));
    std::string list = "[";
    for (std::size_t i = 0; i < missing.size(); ++i) {
      list += (i != 0U ? ", '" : "'") + missing[i] + "'";
    }
    ctx.fail("freeze: no value of " + list + "] before " + buf + " s to hold");
  }
  return set_fields(std::move(events), f, ctx, w, [&held](const Event&, const std::string& field) {
    return FieldValue{held.at(field)};
  });
}

std::vector<Event> fault_step(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const double delta = f.f64("delta");
  return set_fields(
      std::move(events), f, ctx, window_of(f, f.f64("at_s")),
      [delta, &ctx](const Event& e, const std::string& field) {
        const Numeric n = as_number(get_path(*e.msg().data, field));
        if (n.problem()) {
          ctx.fail("step: '" + field + "' on " + e.topic() + " is not a finite number");
        }
        return FieldValue{Json(n.value + delta)};
      });
}

// -- safety / control ----------------------------------------------------------

// Python statistics.median over ints, truncated by int() toward zero.
std::int64_t median_trunc(std::vector<std::int64_t> d) {
  if (d.empty()) {
    return 0;
  }
  std::sort(d.begin(), d.end());
  const std::size_t n = d.size();
  if (n % 2 == 1) {
    return d[n / 2];
  }
  const long double mid =
      (static_cast<long double>(d[n / 2 - 1]) + static_cast<long double>(d[n / 2])) / 2.0L;
  return static_cast<std::int64_t>(mid);
}

std::int64_t host_offset_ns(const std::vector<Event>& events, const std::string& host,
                            const TopicTable& topics) {
  std::vector<std::int64_t> d;
  for (const Event& e : events) {
    const auto* m = e.message();
    if (m == nullptr || !m->src || count_ns(*m->src) == 0 || !m->rx_wall ||
        count_ns(*m->rx_wall) == 0) {
      continue;
    }
    const auto it = topics.find(m->topic);
    if (it == topics.end() || it->second.host != host) {
      continue;
    }
    d.push_back(count_ns(*m->src) - count_ns(*m->rx_wall));
  }
  return median_trunc(std::move(d));
}

std::vector<Event> fault_inject_stream(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const double rate = f.f64("rate_hz");
  if (!(rate > 0)) {
    ctx.fail("rate_hz must be > 0");
  }
  if (!f.opt_f64("to_s")) {
    ctx.fail("inject_stream needs to_s");
  }
  for (const char* key : {"data", "final_data"}) {
    const Json& v = f.json(key);
    if (!v.is_null() && !v.is_object()) {
      ctx.fail(std::string("inject_stream: ") + key + " must be a message object, got " + repr(v));
    }
  }
  const std::string topic = *f.opt_str("topic");
  const auto info_it = ctx.topics.find(topic);
  const TopicInfo* info = info_it == ctx.topics.end() ? nullptr : &info_it->second;
  std::optional<std::string> role_name = f.opt_str("role");
  if (!role_name || role_name->empty()) {
    role_name = info != nullptr ? std::optional<std::string>(info->role_name) : std::nullopt;
  }
  if (!role_name) {
    ctx.fail("inject_stream: " + topic + " is not in the evidence; give role");
  }
  std::string host = f.opt_str("host").value_or("");
  if (host.empty()) {
    host = info != nullptr ? info->host : "payload";
  }
  const Event* anchor = nullptr;
  for (const Event& e : events) {
    const auto* m = e.message();
    if (m != nullptr && m->rx_wall && count_ns(*m->rx_wall) != 0) {
      anchor = &e;
      break;
    }
  }
  if (anchor == nullptr) {
    ctx.fail("inject_stream: evidence has no receipt wall clock to anchor on");
  }
  const std::int64_t wall0 = count_ns(*anchor->msg().rx_wall) - anchor->t_ns();
  const std::int64_t mono0 = count_ns(anchor->rec_mono) - anchor->t_ns();
  const std::int64_t off = host_offset_ns(events, host, ctx.topics);
  const std::int64_t lo = ns_of(f.f64("from_s"));
  const std::int64_t hi = ns_of(*f.opt_f64("to_s"));
  const double period = 1e9 / rate;
  std::vector<std::int64_t> times;
  for (std::int64_t k = 0;; ++k) {
    const std::int64_t t =
        lo + static_cast<std::int64_t>(std::nearbyint(static_cast<double>(k) * period));
    if (t >= hi) {
      break;
    }
    times.push_back(t);
  }
  std::vector<const Json*> payloads(times.size(), &f.json("data"));
  if (!f.json("final_data").is_null()) {
    times.push_back(hi);
    payloads.push_back(&f.json("final_data"));
  }
  const Role role = parse_role(*role_name).value_or(Role::other);
  const std::string type = f.opt_str("type").value_or("geometry_msgs/msg/Twist");
  std::vector<Event> made;
  for (std::size_t k = 0; k < times.size(); ++k) {
    const std::int64_t t = times[k];
    const std::int64_t rx = wall0 + t;
    Json rec = {{"kind", "msg"},
                {"topic", topic},
                {"role", *role_name},
                {"type", type},
                {"t_mono_ns", mono0 + t},
                {"t_wall_ns", rx},
                {"t_ros_ns", rx},
                {"dds_src_ns", rx + off},
                {"dds_rx_ns", rx},
                {"pub_stamp_s", nullptr},
                {"pub_stamp_domain", nullptr},
                {"data", *payloads[k]}};
    Event e;
    e.t = replay_ns(t);
    e.order = OrderKey{1, static_cast<std::int64_t>(ctx.index), static_cast<std::int64_t>(k)};
    e.eid = ctx.fault.id + "." + std::to_string(k);
    e.rec_mono = mono_ns(mono0 + t);
    e.rec_wall = wall_ns(rx);
    MessageBody m;
    m.topic = topic;
    m.role = role;
    m.role_name = *role_name;
    m.type = type;
    m.src = source_ns(rx + off);
    m.rx_wall = wall_ns(rx);
    if (!payloads[k]->is_null()) {
      m.set_data(*payloads[k]);
    }
    e.body = std::move(m);
    e.record = std::make_shared<const Json>(std::move(rec));
    made.push_back(ctx.mark(std::move(e)));
  }
  if (made.empty()) {
    ctx.fail("inject_stream: window produced no messages");
  }
  if (info == nullptr) {
    TopicInfo ti;
    ti.name = topic;
    ti.role_name = *role_name;
    ti.role = role;
    ti.host = host;
    ti.liveness = Liveness::event;
    ctx.topics.emplace(topic, std::move(ti));
  }
  for (auto& e : made) {
    events.push_back(std::move(e));
  }
  return events;
}

GraphBody without_node(GraphBody g, const std::string& node) {
  auto strip = [&](std::vector<std::string>& v) { std::erase(v, node); };
  strip(g.nodes);
  strip(g.nodes_new);
  for (auto& [topic, pubs] : g.publishers) {
    strip(pubs);
  }
  return g;
}

std::vector<Event> fault_node_exit(std::vector<Event> events, const Fault& f, Ctx& ctx) {
  const std::int64_t at = ns_of(f.f64("at_s"));
  const std::string node = *f.opt_str("node");
  std::set<std::string> nodes;
  std::map<std::string, std::vector<std::string>> pubs;
  const Event* last_before = nullptr;
  for (const Event& e : events) {
    const GraphBody* g = e.graph();
    if (g == nullptr || e.t_ns() > at) {
      continue;
    }
    last_before = &e;
    if (g->full) {
      nodes = std::set<std::string>(g->nodes.begin(), g->nodes.end());
    } else {
      for (const auto& n : g->nodes_gone) {
        nodes.erase(n);
      }
      nodes.insert(g->nodes_new.begin(), g->nodes_new.end());
    }
    if (g->has_publishers && !g->publishers.empty()) {
      pubs = g->publishers;
    }
  }
  if (nodes.count(node) == 0U) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.3f", ns_to_seconds(at));
    ctx.fail("node_exit: " + node + " is not on the recorded graph before " + buf + " s");
  }
  const Event anchor = last_before != nullptr ? *last_before : events.front();
  for (Event& e : events) {
    if (e.graph() != nullptr && e.t_ns() > at) {
      Event changed = e;
      changed.body = without_node(*e.graph(), node);
      e = ctx.mark(std::move(changed));
    }
  }
  const std::int64_t shift = at - anchor.t_ns();
  GraphBody g;
  g.full = false;
  g.nodes_gone = {node};
  g.has_publishers = true;
  for (const auto& [topic, list] : pubs) {
    std::vector<std::string> kept;
    for (const auto& n : list) {
      if (n != node) {
        kept.push_back(n);
      }
    }
    g.publishers.emplace(topic, std::move(kept));
  }
  Json pubs_json = Json::object();
  for (const auto& [topic, list] : g.publishers) {
    pubs_json[topic] = list;
  }
  Event e;
  e.t = replay_ns(at);
  e.order = OrderKey{1, static_cast<std::int64_t>(ctx.index), 0};
  e.eid = ctx.fault.id + ".0";
  e.rec_mono = anchor.rec_mono + Nanos{shift};
  e.rec_wall = anchor.rec_wall + Nanos{shift};
  e.record = std::make_shared<const Json>(Json{{"kind", "graph"},
                                               {"full", false},
                                               {"nodes_gone", Json::array({node})},
                                               {"nodes_new", Json::array()},
                                               {"publishers", pubs_json},
                                               {"t_mono_ns", count_ns(e.rec_mono)},
                                               {"t_wall_ns", count_ns(e.rec_wall)}});
  e.body = std::move(g);
  events.push_back(ctx.mark(std::move(e)));
  return events;
}

using Injector = std::vector<Event> (*)(std::vector<Event>, const Fault&, Ctx&);

Injector injector_for(FaultKind k) {
  switch (k) {
    case FaultKind::drop: return &fault_drop;
    case FaultKind::gap: return &fault_gap;
    case FaultKind::delay: return &fault_delay;
    case FaultKind::duplicate: return &fault_duplicate;
    case FaultKind::reorder: return &fault_reorder;
    case FaultKind::stale_redelivery: return &fault_stale_redelivery;
    case FaultKind::clock_skew: return &fault_clock_skew;
    case FaultKind::timestamp_jump: return &fault_timestamp_jump;
    case FaultKind::nan: return &fault_nan;
    case FaultKind::inf: return &fault_inf;
    case FaultKind::malformed: return &fault_malformed;
    case FaultKind::set_value: return &fault_set_value;
    case FaultKind::freeze: return &fault_freeze;
    case FaultKind::step: return &fault_step;
    case FaultKind::node_exit: return &fault_node_exit;
    case FaultKind::inject_stream: return &fault_inject_stream;
  }
  return nullptr;
}

void sort_events(std::vector<Event>& v) {
  std::sort(v.begin(), v.end(), [](const Event& a, const Event& b) { return a < b; });
}

}  // namespace

// ---------------------------------------------------------------------------
// public API
// ---------------------------------------------------------------------------

const std::vector<KindSpec>& fault_kinds() {
  static const std::vector<KindSpec> kinds = build_kinds();
  return kinds;
}

const KindSpec* find_kind(std::string_view name) noexcept {
  for (const auto& k : fault_kinds()) {
    if (k.name == name) {
      return &k;
    }
  }
  return nullptr;
}

Json Fault::describe() const {
  Json p = Json::object();
  for (const auto& [k, v] : params) {
    p[k] = v;
  }
  return {{"id", id}, {"kind", kind_name}, {"params", p}};
}

const Json& Fault::json(const std::string& k) const {
  static const Json kNull;
  const auto it = params.find(k);
  return it == params.end() ? kNull : it->second;
}
double Fault::f64(const std::string& k) const {
  const Json& v = json(k);
  if (!v.is_number()) {
    throw FaultError("parameter " + k + " is required");
  }
  return v.get<double>();
}
std::optional<double> Fault::opt_f64(const std::string& k) const {
  const Json& v = json(k);
  return v.is_number() ? std::optional<double>(v.get<double>()) : std::nullopt;
}
std::int64_t Fault::i64(const std::string& k) const {
  return json(k).get<std::int64_t>();
}
bool Fault::flag(const std::string& k) const {
  return json(k).get<bool>();
}
std::optional<std::string> Fault::opt_str(const std::string& k) const {
  const Json& v = json(k);
  return v.is_string() ? std::optional<std::string>(v.get<std::string>()) : std::nullopt;
}
std::vector<std::string> Fault::list(const std::string& k) const {
  std::vector<std::string> out;
  for (const auto& v : json(k)) {
    out.push_back(v.get<std::string>());
  }
  return out;
}

Fault parse_fault(const Json& raw, std::size_t index) {
  const std::string where = "fault #" + std::to_string(index + 1);
  if (!raw.is_object()) {
    throw FaultError(where + ": must be an object");
  }
  const std::string kind = raw.contains("kind") && raw["kind"].is_string()
                               ? raw["kind"].get<std::string>()
                               : std::string();
  const KindSpec* spec = find_kind(kind);
  if (spec == nullptr) {
    std::string known;
    for (const auto& k : fault_kinds()) {
      known += (known.empty() ? "" : ", ") + k.name;
    }
    throw FaultError(where + ": unknown kind " +
                     (raw.contains("kind") ? repr(raw["kind"]) : "None") + "; known: " + known);
  }
  Fault f;
  f.kind = spec->kind;
  f.kind_name = spec->name;
  if (raw.contains("id") && !raw["id"].is_null() &&
      !(raw["id"].is_string() && raw["id"].get<std::string>().empty())) {
    if (!raw["id"].is_string()) {
      throw FaultError(where + ": id must be a string");
    }
    f.id = raw["id"].get<std::string>();
  } else {
    f.id = "F" + std::to_string(index + 1);
  }
  std::vector<std::string> extra;
  for (const auto& item : raw.items()) {
    const std::string& k = item.key();
    if (k == "kind" || k == "id") {
      continue;
    }
    if (std::none_of(spec->params.begin(), spec->params.end(),
                     [&](const ParamSpec& p) { return p.name == k; })) {
      extra.push_back(k);
    }
  }
  if (!extra.empty()) {
    std::string s;
    for (const auto& e : extra) {
      s += (s.empty() ? "'" : ", '") + e + "'";
    }
    throw FaultError("fault " + f.id + " (" + kind + "): unknown parameter(s) [" + s + "]");
  }
  for (const auto& p : spec->params) {
    if (raw.contains(p.name)) {
      try {
        f.params[p.name] = coerce(p, raw[p.name]);
      } catch (const FaultError& exc) {
        throw FaultError("fault " + f.id + " (" + kind + "): " + exc.what());
      }
    } else if (p.required) {
      throw FaultError("fault " + f.id + " (" + kind + "): missing parameter '" + p.name + "'");
    } else {
      f.params[p.name] = p.default_value;
    }
  }
  for (const auto& [k, v] : f.params) {
    f.param_text[k] = py_str_any(v);
  }
  return f;
}

Fault parse_fault_ordered(const OrderedJson& raw, std::size_t index) {
  Fault f = parse_fault(Json::parse(raw.dump()), index);
  if (raw.is_object()) {
    for (auto it = raw.begin(); it != raw.end(); ++it) {
      if (f.param_text.count(it.key()) != 0U && (it->is_object() || it->is_array())) {
        f.param_text[it.key()] = py_str_any(it.value());
      }
    }
  }
  return f;
}

Fault parse_cli_fault(std::string_view text_in, std::size_t index) {
  std::string text(text_in);
  const auto b = text.find_first_not_of(" \t");
  const auto e = text.find_last_not_of(" \t");
  text = b == std::string::npos ? std::string() : text.substr(b, e - b + 1);
  if (!text.empty() && text.front() == '{') {
    const OrderedJson j = OrderedJson::parse(text, nullptr, false);
    if (j.is_discarded()) {
      throw FaultError("--inject '" + text + "': invalid JSON");
    }
    return parse_fault_ordered(j, index);
  }
  const auto colon = text.find(':');
  OrderedJson raw = OrderedJson::object();
  std::string kind = text.substr(0, colon);
  kind.erase(0, kind.find_first_not_of(" \t"));
  kind.erase(kind.find_last_not_of(" \t") + 1);
  raw["kind"] = kind;
  if (colon != std::string::npos) {
    const std::string rest = text.substr(colon + 1);
    std::size_t start = 0;
    while (start <= rest.size()) {
      const auto comma = rest.find(',', start);
      std::string part =
          rest.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
      start = comma == std::string::npos ? rest.size() + 1 : comma + 1;
      if (part.find_first_not_of(" \t") == std::string::npos) {
        continue;
      }
      const auto eq = part.find('=');
      if (eq == std::string::npos) {
        throw FaultError("--inject '" + text + "': expected key=value, got '" + part + "'");
      }
      std::string k = part.substr(0, eq);
      std::string v = part.substr(eq + 1);
      k.erase(0, k.find_first_not_of(" \t"));
      k.erase(k.find_last_not_of(" \t") + 1);
      const OrderedJson parsed = OrderedJson::parse(v, nullptr, false);
      if (!parsed.is_discarded()) {
        raw[k] = parsed;
      } else {
        v.erase(0, v.find_first_not_of(" \t"));
        v.erase(v.find_last_not_of(" \t") + 1);
        raw[k] = v;
      }
    }
  }
  return parse_fault_ordered(raw, index);
}

Json InjectionLog::to_json() const {
  Json p = Json::object();
  for (const auto& [k, v] : params) {
    p[k] = v;
  }
  return {{"id", id},
          {"kind", kind},
          {"category", category},
          {"params", p},
          {"events_affected", events_affected},
          {"topics_affected", topics_affected},
          {"first_t_ns", count_ns(first_t)},
          {"last_t_ns", count_ns(last_t)},
          {"first_events", first_events}};
}

void check_total_order(const std::vector<Event>& events) {
  std::set<std::pair<std::int64_t, OrderKey>> keys;
  std::set<std::string> ids;
  for (const Event& e : events) {
    if (!keys.emplace(e.t_ns(), e.order).second) {
      throw EvidenceError(
          "two replay events share one (time, order) key; "
          "the replay order would be ambiguous");
    }
    if (!ids.insert(e.eid).second) {
      throw EvidenceError("two replay events share one event id");
    }
  }
}

std::vector<Event> apply_faults(std::vector<Event> events, const std::vector<Fault>& faults,
                                TopicTable& topics, std::vector<InjectionLog>& log) {
  std::set<std::string> ids;
  for (const auto& f : faults) {
    if (!ids.insert(f.id).second) {
      std::string all;
      for (const auto& g : faults) {
        all += (all.empty() ? "'" : ", '") + g.id + "'";
      }
      throw FaultError("duplicate fault ids: [" + all + "]");
    }
  }
  const auto& kinds = fault_kinds();
  for (std::size_t i = 0; i < faults.size(); ++i) {
    const Fault& f = faults[i];
    Ctx ctx(f, i, topics, events);
    try {
      events = injector_for(f.kind)(std::move(events), f, ctx);
    } catch (const FaultError& exc) {
      throw FaultError("fault " + f.id + " (" + f.kind_name + "): " + exc.what());
    }
    if (ctx.touched.empty()) {
      throw FaultError("fault " + f.id + " (" + f.kind_name +
                       ") matched no event; check the topic and the time window");
    }
    sort_events(events);
    sort_events(ctx.touched);
    InjectionLog entry;
    entry.id = f.id;
    entry.kind = f.kind_name;
    entry.params = f.params;
    entry.param_text = f.param_text;
    const auto spec = std::find_if(kinds.begin(), kinds.end(),
                                   [&](const KindSpec& k) { return k.kind == f.kind; });
    entry.category = spec->category;
    entry.events_affected = static_cast<std::int64_t>(ctx.touched.size());
    std::set<std::string> touched_topics;
    for (const Event& e : ctx.touched) {
      if (!e.topic().empty()) {
        touched_topics.insert(e.topic());
      }
    }
    entry.topics_affected.assign(touched_topics.begin(), touched_topics.end());
    entry.first_t = ctx.touched.front().t;
    entry.last_t = ctx.touched.back().t;
    for (std::size_t k = 0; k < ctx.touched.size() && k < 10; ++k) {
      entry.first_events.push_back(ctx.touched[k].eid);
    }
    log.push_back(std::move(entry));
  }
  check_total_order(events);
  return events;
}

}  // namespace blackboxrs::replay
