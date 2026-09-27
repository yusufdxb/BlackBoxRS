#include "blackboxrs/replay/engine.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <memory>
#include <set>
#include <sstream>

#include "blackboxrs/integrity.hpp"
#include "blackboxrs/replay/liveness.hpp"
#include "blackboxrs/replay/monitors.hpp"
#include "blackboxrs/replay/sut.hpp"

namespace blackboxrs::replay {
namespace fs = std::filesystem;

Nanos ReplayClock::advance_to(ReplayTime t) {
  if (t < now_) {
    throw ClockError("replay clock cannot go back from " + std::to_string(count_ns(now_)) + " to " +
                     std::to_string(count_ns(t)));
  }
  const Nanos step = t - now_;
  now_ = t;
  return step;
}

Json ReplayConfig::describe() const {
  Json faults_json = Json::array();
  for (const auto& f : faults) {
    faults_json.push_back(f.describe());
  }
  Json cs = Json::object();
  for (const auto& [k, v] : command_sources) {
    cs[k] = v;
  }
  return {{"sut_mode", sut_mode},
          {"preset", preset},
          {"overrides", overrides.is_null() ? Json::object() : overrides},
          {"faults", faults_json},
          {"topics", topics.is_null() ? Json::object() : topics},
          {"from_s", from_s ? Json(*from_s) : Json()},
          {"to_s", to_s ? Json(*to_s) : Json()},
          {"command_sources", cs},
          {"stop_grace_s", stop_grace_s},
          {"fresh_grace_s", fresh_grace_s},
          {"clock_step_threshold_s", clock_step_threshold_s},
          {"clock_offset_info_s", clock_offset_info_s},
          {"observed_period_s", observed_period_s}};
}

void validate_config(const ReplayConfig& cfg) {
  auto finite = [](const char* name, double v) {
    if (!std::isfinite(v)) {
      throw std::invalid_argument(std::string(name) + " must be a number");
    }
    return v;
  };
  for (const auto& [name, v] : {std::pair("from_s", cfg.from_s), std::pair("to_s", cfg.to_s)}) {
    if (v && finite(name, *v) < 0) {
      throw std::invalid_argument(std::string(name) + " must be >= 0");
    }
  }
  // A grace window is tolerance for arbiter tick and transport delay. Past
  // half a second it would hide the very violations it is meant to judge.
  for (const auto& [name, v] : {std::pair("stop_grace_s", cfg.stop_grace_s),
                                std::pair("fresh_grace_s", cfg.fresh_grace_s)}) {
    if (!(finite(name, v) >= 0.0 && v <= 0.5)) {
      throw std::invalid_argument(std::string(name) + " must be within [0, 0.5] s");
    }
  }
  for (const auto& [name, v] : {std::pair("clock_step_threshold_s", cfg.clock_step_threshold_s),
                                std::pair("clock_offset_info_s", cfg.clock_offset_info_s),
                                std::pair("observed_period_s", cfg.observed_period_s)}) {
    if (!(finite(name, v) > 0.0 && v <= 10.0)) {
      throw std::invalid_argument(std::string(name) + " must be within (0, 10] s");
    }
  }
}

TopicTable topic_table(const Evidence& ev, const Json& overrides_in) {
  const Json overrides = overrides_in.is_null() ? Json::object() : overrides_in;
  std::map<std::string, std::string> roles;
  for (const auto& t : ev.profile.topics) {
    roles.emplace(t.name, std::string(role_name(t.role)));
  }
  for (const Json& r : ev.records) {
    if (r["kind"] == "msg") {
      roles.emplace(r["topic"].get<std::string>(), r["role"].get<std::string>());
    }
  }
  std::vector<std::string> unknown;
  for (const auto& [k, v] : overrides.items()) {
    if (roles.count(k) == 0U) {
      unknown.push_back(k);
    }
  }
  if (!unknown.empty()) {
    std::string list;
    for (const auto& u : unknown) {
      list += (list.empty() ? "'" : ", '") + u + "'";
    }
    throw EvidenceError("topic overrides name topics not in the evidence: [" + list + "]");
  }
  TopicTable out;
  for (const auto& [name, rname] : roles) {
    const TopicSpec* spec = ev.profile.topic(name);
    std::optional<double> stale = spec != nullptr ? spec->stale_after_sec : std::nullopt;
    const Json o = overrides.contains(name) ? overrides[name] : Json::object();
    for (const auto& [k, v] : o.items()) {
      if (k != "host" && k != "liveness" && k != "stale_after_s") {
        throw EvidenceError("topic override for " + name + ": unknown keys ['" + k + "']");
      }
    }
    if (o.contains("stale_after_s")) {
      const Json& s = o["stale_after_s"];
      stale =
          s.is_number() && !s.is_boolean() ? std::optional<double>(s.get<double>()) : std::nullopt;
      if (!s.is_null() && !s.is_number()) {
        throw EvidenceError("topic " + name + ": periodic liveness needs stale_after_s > 0");
      }
    }
    std::string liveness = stale ? "periodic" : "event";
    if (o.contains("liveness")) {
      liveness = o["liveness"].is_string() ? o["liveness"].get<std::string>() : "";
    }
    if (liveness != "periodic" && liveness != "event") {
      throw EvidenceError("topic override for " + name + ": liveness must be periodic or event");
    }
    if (liveness == "periodic" && !(stale && *stale > 0)) {
      throw EvidenceError("topic " + name + ": periodic liveness needs stale_after_s > 0");
    }
    const Role role = parse_role(rname).value_or(Role::other);
    TopicInfo info;
    info.name = name;
    info.role_name = rname;
    info.role = role;
    info.host = o.contains("host") && o["host"].is_string()
                    ? o["host"].get<std::string>()
                    : (is_robot_role(role) ? "robot" : "payload");
    info.liveness = liveness == "periodic" ? Liveness::periodic : Liveness::event;
    if (info.liveness == Liveness::periodic) {
      info.stale_after_s = stale;
    }
    out.emplace(name, std::move(info));
  }
  return out;
}

namespace {

Json finding_json(const std::string& id, const Finding& f,
                  const std::vector<std::string>& related) {
  return {{"id", id},
          {"t_ns", count_ns(f.t)},
          {"t_s", py_round(static_cast<double>(count_ns(f.t)) / 1e9, 9)},
          {"monitor", f.monitor},
          {"kind", f.kind},
          {"severity", severity_name(f.severity)},
          {"subject", f.subject},
          {"message", f.message},
          {"evidence", f.evidence},
          {"invariant", f.invariant ? Json(*f.invariant) : Json()},
          {"related_faults", related},
          {"data", jsonable(f.data)}};
}

Json invariant_json(const InvariantState& inv) {
  return {
      {"status", status_name(inv.status())},
      {"statement", inv.statement},
      {"checks", inv.checks},
      {"violating_ticks", inv.violations},
      {"episodes", inv.episodes},
      {"first_violation_t_ns", inv.first_violation ? Json(count_ns(*inv.first_violation)) : Json()},
      {"incomplete_reason", inv.incomplete_reason ? Json(*inv.incomplete_reason) : Json()}};
}

Json compute_verdict(const Json& result) {
  const Json& inv = result["invariants"];
  std::vector<std::string> failed;
  std::vector<std::string> incomplete;
  std::vector<std::string> not_exercised;
  for (const auto& [name, v] : inv.items()) {
    const std::string s = v["status"].get<std::string>();
    if (s == "FAIL") failed.push_back(name);
    if (s == "INCOMPLETE") incomplete.push_back(name);
    if (s == "NOT_EXERCISED") not_exercised.push_back(name);
  }
  const auto kinds = detection_kinds(result);
  std::vector<std::string> reasons;
  auto join = [](const std::vector<std::string>& v, const char* sep) {
    std::string s;
    for (std::size_t i = 0; i < v.size(); ++i) {
      s += (i != 0U ? sep : "") + v[i];
    }
    return s;
  };
  std::string verdict;
  const bool partial = result["evidence"]["partial"].get<bool>();
  if (!failed.empty()) {
    verdict = "FAIL";
    reasons.push_back("invariant(s) violated: " + join(failed, ", "));
  } else if (!incomplete.empty() || partial) {
    verdict = "INCOMPLETE";
    for (const auto& n : incomplete) {
      reasons.push_back(n + ": " + inv[n]["incomplete_reason"].get<std::string>());
    }
    if (partial) {
      std::vector<std::string> probs;
      for (const auto& p : result["evidence"]["problems"]) {
        probs.push_back(p.get<std::string>());
      }
      reasons.push_back("evidence is partial: " + join(probs, "; "));
    }
  } else if (!kinds.empty()) {
    verdict = "DETECTED";
    reasons.push_back("invariants held; detections: " + join(kinds, ", "));
  } else {
    verdict = "PASS";
    reasons.emplace_back("invariants held; no warning or critical finding");
  }
  for (const auto& note : result["replay"]["notes"]) {
    reasons.push_back(note.get<std::string>());
  }
  return {{"result", verdict},
          {"reasons", reasons},
          {"invariants_failed", failed},
          {"invariants_incomplete", incomplete},
          {"invariants_not_exercised", not_exercised},
          {"detections", kinds}};
}

Json summarize(const Json& result) {
  const Json& tl = result["timeline"];
  const Json& finds = result["findings"];
  const Json* first_fault = nullptr;
  for (const auto& e : tl) {
    if (e["layer"] == "fault") {
      first_fault = &e;
      break;
    }
  }
  const Json* first_det = nullptr;
  const Json* first_viol = nullptr;
  for (const auto& f : finds) {
    if (first_det == nullptr && f["severity"] != "info" && f["invariant"].is_null()) {
      first_det = &f;
    }
    if (first_viol == nullptr && !f["invariant"].is_null()) {
      first_viol = &f;
    }
  }
  Json faults = Json::array();
  for (const auto& f : result["injections"]) {
    faults.push_back(f["id"].get<std::string>() + " " + f["kind"].get<std::string>());
  }
  Json first_decision = nullptr;
  const Json* last_output = nullptr;
  for (const auto& e : tl) {
    if (e["layer"] == "decision" && first_fault != nullptr && first_decision.is_null() &&
        e["t_ns"].get<std::int64_t>() >= (*first_fault)["t_ns"].get<std::int64_t>()) {
      first_decision = {{"t_s", e["t_s"]}, {"text", e["text"]}};
    }
    if (e["layer"] == "output") {
      last_output = &e;
    }
  }
  Json invs = Json::object();
  for (const auto& [n, v] : result["invariants"].items()) {
    invs[n] = v["status"];
  }
  return {{"faults", faults},
          {"first_fault", first_fault == nullptr ? Json()
                                                 : Json{{"t_s", (*first_fault)["t_s"]},
                                                        {"text", (*first_fault)["text"]}}},
          {"first_detection", first_det == nullptr ? Json()
                                                   : Json{{"t_s", (*first_det)["t_s"]},
                                                          {"kind", (*first_det)["kind"]},
                                                          {"subject", (*first_det)["subject"]}}},
          {"first_decision_after_fault", first_decision},
          {"final_robot_command", last_output == nullptr ? Json() : (*last_output)["text"]},
          {"first_invariant_violation", first_viol == nullptr
                                            ? Json()
                                            : Json{{"t_s", (*first_viol)["t_s"]},
                                                   {"invariant", (*first_viol)["invariant"]},
                                                   {"message", (*first_viol)["message"]}}},
          {"invariants", invs}};
}

std::string fmt3(double v) {
  return fmt_fixed(v, 3);
}

}  // namespace

std::vector<std::string> detection_kinds(const Json& result) {
  std::set<std::string> kinds;
  for (const auto& f : result["findings"]) {
    if ((f["severity"] == "warning" || f["severity"] == "critical") && f["invariant"].is_null()) {
      kinds.insert(f["kind"].get<std::string>());
    }
  }
  return {kinds.begin(), kinds.end()};
}

ExitCode exit_code_for(const Json& result) {
  const std::string v = result["verdict"]["result"].get<std::string>();
  if (v == "PASS") return ExitCode::pass;
  if (v == "FAIL") return ExitCode::fail;
  if (v == "INCOMPLETE") return ExitCode::incomplete;
  return ExitCode::detected;
}

Json replay(const Evidence& ev, const ReplayConfig& cfg, const ReplayOptions& options) {
  validate_config(cfg);
  std::optional<ArbiterConfig> arb_cfg;
  if (cfg.sut_mode == "reference") {
    arb_cfg = build_arbiter_config(cfg.preset, cfg.overrides);
  } else if (cfg.sut_mode != "observed") {
    throw std::invalid_argument("sut mode must be reference or observed, not '" + cfg.sut_mode +
                                "'");
  } else if (!cfg.overrides.is_null() && !cfg.overrides.empty()) {
    throw std::invalid_argument("sut overrides apply to reference mode only");
  }
  TopicTable topics = topic_table(ev, cfg.topics);

  // --- normalize -------------------------------------------------------------
  std::vector<Event> events;
  std::map<std::string, std::int64_t> skipped;
  for (const Json& r : ev.records) {
    const std::string& kind = r["kind"].get_ref<const std::string&>();
    if (!is_input_kind(kind)) {
      ++skipped[kind];
      continue;
    }
    events.push_back(event_from_record(r, ev.t0));
  }
  std::sort(events.begin(), events.end());
  const std::vector<Event> recorded = events;  // the evidence as recorded, before any fault
  std::vector<InjectionLog> faultlog;
  events = apply_faults(std::move(events), cfg.faults, topics, faultlog);
  std::map<std::string, std::vector<std::string>> by_role;
  for (const auto& [name, info] : topics) {
    by_role[info.role_name].push_back(name);
  }

  // --- window -----------------------------------------------------------------
  const std::int64_t lo = seconds_to_ns(cfg.from_s.value_or(0.0));
  const std::optional<std::int64_t> hi =
      cfg.to_s ? std::optional<std::int64_t>(seconds_to_ns(*cfg.to_s)) : std::nullopt;
  if (hi && *hi <= lo) {
    throw EvidenceError("--to must be after --from");
  }
  auto in_window = [&](std::int64_t t) { return t >= lo && (!hi || t < *hi); };
  std::erase_if(events, [&](const Event& e) { return !in_window(e.t_ns()); });

  // --- system under test ------------------------------------------------------
  const Event* anchor = nullptr;
  for (const Event& e : events) {
    const MessageBody* m = e.message();
    if (m != nullptr && m->rx_wall && count_ns(*m->rx_wall) != 0) {
      anchor = &e;
      break;
    }
  }
  if (anchor == nullptr) {
    throw EvidenceError("no message in the replay window");
  }
  const std::int64_t wall0 = count_ns(*anchor->msg().rx_wall) - anchor->t_ns();
  std::map<std::string, std::int64_t> suppressed;
  std::map<std::string, double> sources;
  std::int64_t period = 0;
  std::optional<SystemUnderTest> sut;
  if (arb_cfg) {
    std::set<std::string> downstream;
    for (const auto& [n, t] : topics) {
      if (is_downstream_role(t.role)) {
        downstream.insert(n);
      }
    }
    for (const auto& f : faultlog) {
      if (!f.topics_affected.empty() &&
          std::all_of(f.topics_affected.begin(), f.topics_affected.end(),
                      [&](const std::string& t) { return downstream.count(t) != 0U; })) {
        std::string list;
        for (const auto& t : f.topics_affected) {
          list += (list.empty() ? "'" : ", '") + t + "'";
        }
        throw FaultError("fault " + f.id + " only touches recorded arbiter outputs [" + list +
                         "], which reference mode replaces with the model's output, so it would "
                         "have no effect; use --sut observed");
      }
    }
    std::vector<Event> kept;
    kept.reserve(events.size());
    for (Event& e : events) {
      const MessageBody* m = e.message();
      if (m != nullptr && is_downstream_role(m->role)) {
        ++suppressed[m->topic];
      } else {
        kept.push_back(std::move(e));
      }
    }
    events = std::move(kept);
    sut.emplace(std::in_place_type<ReferenceArbiter>, *arb_cfg, wall0);
    period = seconds_to_ns(arb_cfg->period_s);
    for (const auto& s : arb_cfg->sources) {
      sources[s.topic] = s.timeout_s;
    }
  } else {
    sut.emplace(std::in_place_type<ObservedOutput>, by_role);
    period = seconds_to_ns(cfg.observed_period_s);
    if (!cfg.command_sources.empty()) {
      sources = cfg.command_sources;
    } else if (const auto it = by_role.find("cmd_vel_source"); it != by_role.end()) {
      for (const auto& t : it->second) {
        sources[t] = 0.5;
      }
    }
  }
  if (period <= 0) {
    throw std::invalid_argument("tick period must be > 0");
  }
  std::vector<std::string> unknown_sources;
  for (const auto& [t, w] : sources) {
    if (topics.count(t) == 0U) {
      unknown_sources.push_back(t);
    }
  }
  std::set<std::string> hold_topics;
  if (const auto it = by_role.find("helix_hold"); it != by_role.end()) {
    hold_topics.insert(it->second.begin(), it->second.end());
  }
  std::set<std::string> source_topics;
  for (const auto& [t, w] : sources) {
    source_topics.insert(t);
  }

  // --- detectors ------------------------------------------------------------------
  StopDominance stop(hold_topics, source_topics, cfg.stop_grace_s,
                     arb_cfg ? arb_cfg->hold_timeout_s : 0.5);
  // The oracle also follows the hold as recorded, so a fault on the STOP
  // signal cannot erase a STOP that the evidence contains.
  std::vector<const Event*> shadow;
  std::optional<std::int64_t> recorded_hold_at;
  for (const Event& e : recorded) {
    const MessageBody* m = e.message();
    if (m == nullptr || hold_topics.count(m->topic) == 0U) {
      continue;
    }
    if (in_window(e.t_ns())) {
      shadow.push_back(&e);
    }
    if (!recorded_hold_at && m->data != nullptr) {
      if (const Json* h = get_path(*m->data, "hold");
          h != nullptr && h->is_boolean() && h->get<bool>()) {
        recorded_hold_at = e.t_ns();
      }
    }
  }
  CommandPath cmd(sources, cfg.fresh_grace_s);
  std::set<std::string> status_topics;
  if (!arb_cfg) {
    if (const auto it = by_role.find("arbiter_status"); it != by_role.end()) {
      status_topics.insert(it->second.begin(), it->second.end());
    }
  }
  ConsistentState consistent(status_topics);
  std::map<std::string, std::string> hosts;
  for (const auto& [n, t] : topics) {
    hosts[n] = t.host;
  }
  ClockMonitor clock_mon(hosts, cfg.clock_step_threshold_s, cfg.clock_offset_info_s);
  std::set<std::string> odom_topics;
  if (const auto it = by_role.find("odometry"); it != by_role.end()) {
    odom_topics.insert(it->second.begin(), it->second.end());
  }
  OdometryConsistency odom(odom_topics);
  const std::array<Monitor*, 5> monitors{&stop, &cmd, &consistent, &clock_mon, &odom};
  LivenessMonitor live(ev.profile, topics, ev.t0, wall0);
  Timeline timeline(faultlog, hold_topics, source_topics, options.observer);

  std::vector<std::pair<std::string, Finding>> findings;
  Findings batch;
  auto emit = [&]() {
    for (auto& f : batch) {
      std::string tmp = "tmp" + std::to_string(findings.size());
      timeline.finding(f, tmp);
      findings.emplace_back(std::move(tmp), std::move(f));
    }
    batch.clear();
  };

  // --- dispatch ---------------------------------------------------------------------
  ReplayClock clock(replay_ns(lo));
  const std::int64_t t_last = events.empty() ? lo : events.back().t_ns();
  std::int64_t next_tick = lo;
  std::int64_t ticks = 0;
  std::int64_t publications = 0;
  std::vector<Event> delivered;
  delivered.reserve(events.size());
  std::size_t i = 0;
  std::size_t j = 0;
  auto wait = [&](ReplayTime t) {
    const Nanos step = clock.advance_to(t);
    if (options.pacer) {
      options.pacer(step);
    }
  };
  auto shadow_until = [&](std::int64_t t) {
    while (j < shadow.size() && shadow[j]->t_ns() <= t) {
      stop.on_recorded(*shadow[j]);
      ++j;
    }
  };
  auto sut_on_event = [&](const Event& e) {
    std::visit([&](auto& s) { s.on_event(e, e.t); }, *sut);
  };
  auto sut_take = [&](ReplayTime t) {
    return std::visit([&](auto& s) { return s.take_publication(t); }, *sut);
  };
  auto sut_tick = [&](ReplayTime t) {
    return std::visit([&](auto& s) { return s.tick(t); }, *sut);
  };

  while (i < events.size() || next_tick <= t_last) {
    if (i < events.size() && events[i].t_ns() <= next_tick) {
      Event& e = events[i++];
      wait(e.t);
      shadow_until(e.t_ns());
      timeline.advance(e.t_ns());
      timeline.input(e);
      sut_on_event(e);
      for (Monitor* m : monitors) {
        m->on_event(e, batch);
        emit();
      }
      live.on_event(e, batch);
      emit();
      delivered.push_back(e);
      // A callback publisher (legacy mux, or a recorded output message) can
      // put a command on the robot between ticks: judge it now.
      if (auto pub = sut_take(e.t)) {
        ++publications;
        timeline.decision(*pub);
        for (Monitor* m : monitors) {
          m->on_decision(*pub, batch);
          emit();
        }
      }
    } else {
      const std::int64_t t = next_tick;
      next_tick += period;
      ++ticks;
      wait(replay_ns(t));
      shadow_until(t);
      timeline.advance(t);
      const Decision d = sut_tick(replay_ns(t));
      timeline.decision(d);
      for (Monitor* m : monitors) {
        m->on_decision(d, batch);
        emit();
      }
      live.tick(replay_ns(t), batch);
      emit();
    }
  }
  const ReplayTime t_end = clock.now();
  for (Monitor* m : monitors) {
    m->finish(t_end, batch);
    emit();
  }
  live.finish(t_end, batch);
  emit();
  TransportResult transport = analyze_delivered(ev.profile, delivered, t_end);
  batch = std::move(transport.findings);
  emit();

  // --- stable ids ------------------------------------------------------------------
  std::vector<std::size_t> order(findings.size());
  for (std::size_t k = 0; k < order.size(); ++k) {
    order[k] = k;
  }
  std::stable_sort(order.begin(), order.end(), [&](std::size_t a, std::size_t b) {
    return findings[a].second.sort_key() < findings[b].second.sort_key();
  });
  std::map<std::string, std::string> fid;
  for (std::size_t n = 0; n < order.size(); ++n) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "D%03zu", n + 1);
    fid[findings[order[n]].first] = buf;
  }
  std::map<std::string, std::string> topic_of;
  for (const Event& e : delivered) {
    if (!e.topic().empty()) {
      topic_of[e.eid] = e.topic();
    }
  }
  std::map<std::string, std::vector<std::string>> related;
  Json finding_rows = Json::array();
  for (std::size_t k : order) {
    const auto& [tmp, f] = findings[k];
    // Association, not causation: faults that touched a topic this finding
    // names (subject or evidence) and began at or before it. A dropped
    // message leaves no event to link, so this is how a drop is tied to the
    // finding it produced.
    std::set<std::string> names{f.subject};
    for (const auto& x : f.evidence) {
      if (const auto it = topic_of.find(x); it != topic_of.end()) {
        names.insert(it->second);
      }
    }
    std::vector<std::string> rel;
    for (const auto& log : faultlog) {
      const bool touches = std::any_of(log.topics_affected.begin(), log.topics_affected.end(),
                                       [&](const std::string& t) { return names.count(t) != 0U; });
      if (touches && log.first_t <= f.t) {
        rel.push_back(log.id);
      }
    }
    related[fid[tmp]] = rel;
    finding_rows.push_back(finding_json(fid[tmp], f, rel));
  }
  Json tl = timeline.result(fid, related);

  Json invariants = Json::object();
  for (Monitor* m : monitors) {
    for (const InvariantState* inv : m->invariants()) {
      invariants[inv->name] = invariant_json(*inv);
    }
  }
  auto incomplete = [&](std::initializer_list<const char*> names, const std::string& reason) {
    for (const char* name : names) {
      if (invariants[name]["status"] != "FAIL") {
        invariants[name]["status"] = "INCOMPLETE";
        invariants[name]["incomplete_reason"] = reason;
      }
    }
  };
  // A replay that never saw what it needs must not pass by default.
  std::set<std::string> seen_topics;
  for (const Event& e : delivered) {
    if (e.is_message()) {
      seen_topics.insert(e.topic());
    }
  }
  auto any_seen = [&](const std::set<std::string>& s) {
    return std::any_of(s.begin(), s.end(),
                       [&](const std::string& t) { return seen_topics.count(t) != 0U; });
  };
  const auto* observed = std::get_if<ObservedOutput>(&*sut);
  if (!arb_cfg && !observed->available()) {
    incomplete({"finite_output", "fresh_output", "stop_dominance"},
               "evidence records no arbitration output (/cmd_vel or ArbiterStatus)");
  }
  if (!any_seen(source_topics)) {
    std::string list;
    for (const auto& s : source_topics) {
      list += (list.empty() ? "'" : ", '") + s + "'";
    }
    const std::string reason = "no message on any command source [" + list +
                               "] in the replay; the arbitration path was never exercised";
    if (arb_cfg) {
      incomplete({"finite_output", "fresh_output", "stop_dominance"}, reason);
    } else {
      incomplete({"fresh_output"}, reason);
    }
  }
  if (recorded_hold_at && invariants["stop_dominance"]["status"] == "NOT_EXERCISED") {
    incomplete({"stop_dominance"}, "the evidence asserts a hold at " +
                                       fmt3(ns_to_seconds(*recorded_hold_at)) +
                                       " s, but no robot-facing command was judged against it in "
                                       "this replay window");
  }
  if (!hold_topics.empty() && !any_seen(hold_topics) && recorded_hold_at) {
    incomplete({"finite_output", "fresh_output", "stop_dominance"},
               "the evidence has hold messages but none reached the system under test");
  }
  if (arb_cfg && arb_cfg->stop_mode == StopMode::state &&
      seen_topics.count(arb_cfg->hold_topic) == 0U) {
    incomplete({"finite_output", "fresh_output", "stop_dominance"},
               "no " + arb_cfg->hold_topic +
                   " message in the replay: the model holds zero (HELIX_STATE_MISSING) for the "
                   "whole run, which proves nothing");
  }

  Json config = cfg.describe();
  if (arb_cfg) {
    Json s = arb_cfg->to_json();
    s["mode"] = "reference";
    config["sut"] = s;
  } else {
    Json cs = Json::object();
    for (const auto& [k, v] : sources) {
      cs[k] = v;
    }
    config["sut"] = {
        {"mode", "observed"},
        {"output_source", observed->source_label() ? Json(*observed->source_label()) : Json()},
        {"command_sources", cs}};
  }
  Json topics_json = Json::object();
  for (const auto& [n, t] : topics) {
    topics_json[n] = {{"name", t.name},
                      {"role", t.role_name},
                      {"host", t.host},
                      {"liveness", t.liveness == Liveness::periodic ? "periodic" : "event"},
                      {"stale_after_s", t.stale_after_s ? Json(*t.stale_after_s) : Json()}};
  }
  Json injections = Json::array();
  for (const auto& f : faultlog) {
    injections.push_back(f.to_json());
  }
  Json skipped_json = Json::object();
  for (const auto& [k, v] : skipped) {
    skipped_json[k] = v;
  }
  Json suppressed_json = Json::object();
  for (const auto& [k, v] : suppressed) {
    suppressed_json[k] = v;
  }
  Json notes = Json::array();
  if (lo > 0) {
    notes.push_back("replay starts at " + fmt_g(ns_to_seconds(lo)) +
                    " s: state before it (holds, commands) is not replayed");
  }
  Json result;
  result["schema"] = kResultSchema;
  result["run_id"] = sha256_hex(canonical_json(Json{{"evidence", ev.digest}, {"config", config}}));
  result["evidence"] = {{"source", ev.source},        {"digest", ev.digest},
                        {"synthetic", ev.synthetic},  {"partial", ev.partial},
                        {"problems", ev.problems},    {"records", ev.records.size()},
                        {"profile", ev.profile.name}, {"profile_sha256", ev.profile.sha256}};
  result["config"] = config;
  result["topics"] = topics_json;
  result["injections"] = injections;
  result["replay"] = {{"window_start_ns", lo},
                      {"window_end_ns", count_ns(t_end)},
                      {"events_delivered", delivered.size()},
                      {"ticks", ticks},
                      {"period_ns", period},
                      {"publications_between_ticks", publications},
                      {"suppressed_recorded_outputs", suppressed_json},
                      {"skipped_record_kinds", skipped_json},
                      {"command_sources_not_in_evidence", unknown_sources},
                      {"notes", notes}};
  result["findings"] = finding_rows;
  result["invariants"] = invariants;
  result["liveness"] = live.summary(t_end);
  result["clock"] = clock_mon.summary();
  result["transport"] = transport.summary;
  result["sut_state"] = std::visit([](const auto& s) { return s.state(); }, *sut);
  result["stop_oracle"] = {{"hold_messages_ignored_as_older", stop.ignored_older()}};
  result["timeline"] = tl;
  result["verdict"] = compute_verdict(result);
  result["summary"] = summarize(result);
  return jsonable(std::move(result));
}

// ---------------------------------------------------------------------------
// case files
// ---------------------------------------------------------------------------

Case load_case(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw CaseError(path + ": cannot read case");
  }
  std::stringstream ss;
  ss << in.rdbuf();
  Json raw = Json::parse(ss.str(), nullptr, false);
  if (raw.is_discarded()) {
    throw CaseError(path + ": cannot read case: invalid JSON");
  }
  static constexpr const char* kSchema = "blackboxrs.lab.case.v1";
  if (!raw.is_object() || raw.value("schema", std::string()) != kSchema) {
    throw CaseError(path + ": not a " + std::string(kSchema) + " file");
  }
  static const std::set<std::string> kKeys{
      "schema",         "name",     "description", "regression",     "initial_conditions",
      "injected_fault", "evidence", "sut",         "faults",         "topics",
      "window",         "monitors", "expect",      "command_sources"};
  for (const auto& [k, v] : raw.items()) {
    if (kKeys.count(k) == 0U) {
      throw CaseError(path + ": unknown keys ['" + k + "']");
    }
  }
  for (const char* k : {"name", "evidence", "sut"}) {
    if (!raw.contains(k)) {
      throw CaseError(path + ": missing '" + std::string(k) + "'");
    }
  }
  const Json& sut = raw["sut"];
  if (!sut.is_object() || !(sut.value("mode", std::string()) == "reference" ||
                            sut.value("mode", std::string()) == "observed")) {
    throw CaseError(path + ": sut.mode must be reference or observed");
  }
  Case c;
  c.path = path;
  c.name = raw["name"].get<std::string>();
  c.raw = raw;
  c.evidence = (fs::path(path).parent_path() / raw["evidence"].get<std::string>())
                   .lexically_normal()
                   .string();
  ReplayConfig& cfg = c.config;
  cfg.sut_mode = sut["mode"].get<std::string>();
  cfg.preset = sut.value("preset", std::string("helix_arbiter"));
  cfg.overrides =
      sut.contains("overrides") && sut["overrides"].is_object() ? sut["overrides"] : Json::object();
  if (raw.contains("faults") && raw["faults"].is_array()) {
    // Parsed again with key order kept, so fault descriptions read as written.
    const OrderedJson ordered = OrderedJson::parse(ss.str());
    for (std::size_t i = 0; i < ordered["faults"].size(); ++i) {
      cfg.faults.push_back(parse_fault_ordered(ordered["faults"][i], i));
    }
  }
  static const std::set<std::string> kMonitorKeys{"stop_grace_s", "fresh_grace_s",
                                                  "clock_step_threshold_s", "clock_offset_info_s",
                                                  "observed_period_s"};
  const Json mon =
      raw.contains("monitors") && raw["monitors"].is_object() ? raw["monitors"] : Json::object();
  for (const auto& [k, v] : mon.items()) {
    if (kMonitorKeys.count(k) == 0U) {
      throw CaseError(path + ": unknown monitor settings ['" + k + "']");
    }
    if (!v.is_number()) {
      throw CaseError(path + ": monitor setting " + k + " must be a number");
    }
    const double d = v.get<double>();
    if (k == "stop_grace_s") cfg.stop_grace_s = d;
    if (k == "fresh_grace_s") cfg.fresh_grace_s = d;
    if (k == "clock_step_threshold_s") cfg.clock_step_threshold_s = d;
    if (k == "clock_offset_info_s") cfg.clock_offset_info_s = d;
    if (k == "observed_period_s") cfg.observed_period_s = d;
  }
  cfg.topics = raw.contains("topics") && raw["topics"].is_object() ? raw["topics"] : Json::object();
  if (raw.contains("window") && raw["window"].is_object()) {
    const Json& w = raw["window"];
    if (w.contains("from_s") && w["from_s"].is_number()) cfg.from_s = w["from_s"].get<double>();
    if (w.contains("to_s") && w["to_s"].is_number()) cfg.to_s = w["to_s"].get<double>();
  }
  if (raw.contains("command_sources") && raw["command_sources"].is_object()) {
    for (const auto& [k, v] : raw["command_sources"].items()) {
      cfg.command_sources[k] = v.get<double>();
    }
  }
  c.expect = raw.contains("expect") && raw["expect"].is_object() ? raw["expect"] : Json::object();
  return c;
}

std::vector<std::string> list_case_files(const std::string& dir) {
  std::vector<std::string> out;
  for (const auto& entry : fs::directory_iterator(dir)) {
    if (entry.is_regular_file() && entry.path().extension() == ".json") {
      out.push_back(entry.path().string());
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

std::vector<std::string> check_expectations(const Json& result, const Json& expect) {
  for (const auto& [k, v] : expect.items()) {
    if (k != "verdict" && k != "invariants" && k != "detections" && k != "info") {
      return {"unknown expect keys ['" + k + "']"};
    }
  }
  std::vector<std::string> out;
  const std::string v = result["verdict"]["result"].get<std::string>();
  if (expect.contains("verdict") && expect["verdict"] != v) {
    out.push_back("verdict: expected " + expect["verdict"].get<std::string>() + ", got " + v);
  }
  if (expect.contains("invariants")) {
    for (const auto& [name, want] : expect["invariants"].items()) {
      const Json got =
          result["invariants"].contains(name) ? result["invariants"][name]["status"] : Json();
      if (got != want) {
        out.push_back("invariant " + name + ": expected " + want.get<std::string>() + ", got " +
                      (got.is_string() ? got.get<std::string>() : "None"));
      }
    }
  }
  if (expect.contains("detections")) {
    std::set<std::string> want;
    for (const auto& d : expect["detections"]) {
      want.insert(d.get<std::string>());
    }
    const auto got_v = detection_kinds(result);
    const std::set<std::string> got(got_v.begin(), got_v.end());
    if (want != got) {
      std::vector<std::string> missing;
      std::vector<std::string> extra;
      std::set_difference(want.begin(), want.end(), got.begin(), got.end(),
                          std::back_inserter(missing));
      std::set_difference(got.begin(), got.end(), want.begin(), want.end(),
                          std::back_inserter(extra));
      out.push_back("detections: missing " + Json(missing).dump() + ", unexpected " +
                    Json(extra).dump());
    }
  }
  if (expect.contains("info")) {
    for (const auto& kind : expect["info"]) {
      const bool found = std::any_of(result["findings"].begin(), result["findings"].end(),
                                     [&](const Json& f) { return f["kind"] == kind; });
      if (!found) {
        out.push_back("info finding " + kind.get<std::string>() + " not reported");
      }
    }
  }
  return out;
}

std::string semantic_digest(const Json& result) {
  return sha256_hex(canonical_json(result));
}

}  // namespace blackboxrs::replay
