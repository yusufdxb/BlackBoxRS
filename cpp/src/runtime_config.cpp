#include "blackboxrs/runtime_config.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>
#include <string_view>

#include "blackboxrs/integrity.hpp"

namespace blackboxrs {
namespace fs = std::filesystem;

namespace {

// Every control-path topic in HELIX's own list of real motion topics
// (helix preflight.py) and the GO2 vendor control topics. /helix/hold can
// release a hold, so it is a control topic too.
constexpr std::array<std::string_view, 10> kForbidden{"/cmd_vel",
                                                      "/nav/cmd_vel",
                                                      "/teleop/cmd_vel",
                                                      "/helix/cmd_vel",
                                                      "/helix/hold",
                                                      "/api/sport/request",
                                                      "/lowcmd",
                                                      "/wirelesscontroller",
                                                      "/api/motion_switcher/request",
                                                      "/api/robot_state/request"};
constexpr std::array<std::string_view, 2> kForbiddenPrefixes{"/api/", "/helix_dry/"};

class Reader {
 public:
  // YAML::Node is a reference-counted handle: copying it is the intended use.
  Reader(const YAML::Node& root, std::string where) : root_(root), where_(std::move(where)) {}

  YAML::Node section(const char* name) {
    seen_.insert(name);
    YAML::Node n = root_[name];
    if (!n || n.IsNull()) {
      return YAML::Node(YAML::NodeType::Map);
    }
    if (!n.IsMap()) {
      fail(std::string(name) + " must be a mapping");
    }
    return n;
  }
  void mark(const char* name) { seen_.insert(name); }
  void check_unknown(const YAML::Node& node, const std::set<std::string>& known,
                     const std::string& prefix) const {
    for (const auto& kv : node) {
      const std::string k = kv.first.as<std::string>();
      if (known.count(k) == 0U) {
        fail("unknown key " + prefix + k);
      }
    }
  }
  [[noreturn]] void fail(const std::string& msg) const { throw ConfigError(where_ + ": " + msg); }
  [[nodiscard]] const YAML::Node& root() const { return root_; }
  [[nodiscard]] const std::set<std::string>& seen() const { return seen_; }

 private:
  YAML::Node root_;
  std::string where_;
  std::set<std::string> seen_;
};

double num(const Reader& r, const YAML::Node& n, const std::string& key, double def, double lo,
           double hi, bool lo_open = false) {
  if (!n || n.IsNull()) {
    return def;
  }
  double v = 0.0;
  try {
    v = n.as<double>();
  } catch (const YAML::Exception&) {
    r.fail(key + " must be a number");
  }
  if (!std::isfinite(v) || (lo_open ? !(v > lo) : !(v >= lo)) || !(v <= hi)) {
    r.fail(key + " must be within " + (lo_open ? "(" : "[") + std::to_string(lo) + ", " +
           std::to_string(hi) + "], got " + std::to_string(v));
  }
  return v;
}

std::int64_t integer(const Reader& r, const YAML::Node& n, const std::string& key, std::int64_t def,
                     std::int64_t lo, std::int64_t hi) {
  if (!n || n.IsNull()) {
    return def;
  }
  std::int64_t v = 0;
  try {
    v = n.as<std::int64_t>();
  } catch (const YAML::Exception&) {
    r.fail(key + " must be an integer");
  }
  if (v < lo || v > hi) {
    r.fail(key + " must be within [" + std::to_string(lo) + ", " + std::to_string(hi) + "], got " +
           std::to_string(v));
  }
  return v;
}

bool boolean(const Reader& r, const YAML::Node& n, const std::string& key, bool def) {
  if (!n || n.IsNull()) {
    return def;
  }
  try {
    return n.as<bool>();
  } catch (const YAML::Exception&) {
    r.fail(key + " must be true or false");
  }
}

std::optional<std::string> opt_str(const Reader& r, const YAML::Node& n, const std::string& key) {
  if (!n || n.IsNull()) {
    return std::nullopt;
  }
  if (!n.IsScalar()) {
    r.fail(key + " must be a string");
  }
  return n.as<std::string>();
}

std::vector<std::string> str_list(const Reader& r, const YAML::Node& n, const std::string& key) {
  std::vector<std::string> out;
  if (!n || n.IsNull()) {
    return out;
  }
  if (!n.IsSequence()) {
    r.fail(key + " must be a list");
  }
  for (const auto& x : n) {
    out.push_back(x.as<std::string>());
  }
  return out;
}

}  // namespace

// Built on first use, so an allocation failure is an ordinary, catchable
// exception rather than one thrown during static initialization.
const std::vector<std::string>& builtin_forbidden_topics() {
  static const std::vector<std::string> v(kForbidden.begin(), kForbidden.end());
  return v;
}
const std::vector<std::string>& builtin_forbidden_prefixes() {
  static const std::vector<std::string> v(kForbiddenPrefixes.begin(), kForbiddenPrefixes.end());
  return v;
}

std::string expand_user(const std::string& path) {
  if (path.rfind("~/", 0) == 0 || path == "~") {
    const char* home = std::getenv("HOME");
    return std::string(home != nullptr ? home : "") + path.substr(1);
  }
  return path;
}

bool publish_allowed(const RuntimeConfig& cfg, const std::string& topic, std::string* why) {
  auto deny = [&](const std::string& reason) {
    if (why != nullptr) {
      *why = reason;
    }
    return false;
  };
  for (const auto& t : cfg.forbidden_topics) {
    if (topic == t) {
      return deny(topic + " is a motion or control topic");
    }
  }
  for (const auto& p : cfg.forbidden_prefixes) {
    if (topic.rfind(p, 0) == 0) {
      return deny(topic + " is under the forbidden prefix " + p);
    }
  }
  // Allowlist: diagnostics and BlackBoxRS's own namespace, nothing else.
  if (topic == "/diagnostics" || topic.rfind("/blackboxrs/", 0) == 0) {
    return true;
  }
  return deny(topic + " is outside the publish allowlist (/diagnostics, /blackboxrs/...)");
}

RuntimeConfig load_runtime_config(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw ConfigError(path + ": cannot read configuration");
  }
  std::stringstream ss;
  ss << in.rdbuf();
  RuntimeConfig c;
  c.path = path;
  c.text = ss.str();
  c.sha256 = sha256_hex(c.text);
  YAML::Node root;
  try {
    root = YAML::Load(c.text);
  } catch (const YAML::Exception& exc) {
    throw ConfigError(path + ": invalid YAML: " + exc.what());
  }
  if (!root.IsMap()) {
    throw ConfigError(path + ": configuration must be a mapping");
  }
  Reader r(root, path);
  try {
    r.mark("schema");
    if (!root["schema"] || root["schema"].as<std::string>() != kRuntimeSchema) {
      r.fail(std::string("schema must be ") + kRuntimeSchema);
    }
    r.mark("description");
    r.mark("profile");
    const auto profile = opt_str(r, root["profile"], "profile");
    if (!profile) {
      r.fail("profile is required (path to a capture profile YAML)");
    }
    fs::path pp = expand_user(*profile);
    if (pp.is_relative()) {
      pp = fs::path(path).parent_path() / pp;
    }
    c.profile_path = pp.lexically_normal().string();
    try {
      c.profile = load_profile_file(c.profile_path);
    } catch (const ProfileError& exc) {
      r.fail(std::string("profile: ") + exc.what());
    }

    YAML::Node cap = r.section("capture");
    r.check_unknown(cap, {"mode", "evidence_dir", "session_id", "experiment", "hard_disk_floor_mb"},
                    "capture.");
    const auto mode = opt_str(r, cap["mode"], "capture.mode").value_or("triggered");
    if (mode == "triggered") {
      c.capture_mode = CaptureMode::triggered;
    } else if (mode == "continuous") {
      c.capture_mode = CaptureMode::continuous;
    } else {
      r.fail("capture.mode must be triggered or continuous");
    }
    c.evidence_dir = expand_user(
        opt_str(r, cap["evidence_dir"], "capture.evidence_dir").value_or(c.profile.evidence_dir));
    c.session_id = opt_str(r, cap["session_id"], "capture.session_id");
    if (c.session_id && (c.session_id->empty() || c.session_id->find('/') != std::string::npos)) {
      r.fail("capture.session_id must be a non-empty name without '/'");
    }
    c.experiment = opt_str(r, cap["experiment"], "capture.experiment");
    c.hard_disk_floor_mb =
        integer(r, cap["hard_disk_floor_mb"], "capture.hard_disk_floor_mb", 256, 1, 1'000'000);

    YAML::Node q = r.section("queues");
    r.check_unknown(q,
                    {"ingest_capacity", "control_reserve", "writer_capacity", "drain_deadline_s"},
                    "queues.");
    c.ingest_capacity = static_cast<std::size_t>(
        integer(r, q["ingest_capacity"], "queues.ingest_capacity", 65'536, 64, 4'000'000));
    c.control_reserve = static_cast<std::size_t>(
        integer(r, q["control_reserve"], "queues.control_reserve", 256, 1, 100'000));
    if (c.control_reserve >= c.ingest_capacity) {
      r.fail("queues.control_reserve must be smaller than queues.ingest_capacity");
    }
    c.writer_capacity = static_cast<std::size_t>(
        integer(r, q["writer_capacity"], "queues.writer_capacity", 65'536, 64, 4'000'000));
    c.drain_deadline = std::chrono::milliseconds(static_cast<std::int64_t>(
        1000.0 * num(r, q["drain_deadline_s"], "queues.drain_deadline_s", 3.0, 0.0, 60.0)));

    YAML::Node w = r.section("writer");
    r.check_unknown(w, {"fsync_every_s", "chunk_records", "chunk_bytes"}, "writer.");
    c.fsync_every = std::chrono::milliseconds(static_cast<std::int64_t>(
        1000.0 * num(r, w["fsync_every_s"], "writer.fsync_every_s", 1.0, 0.05, 60.0)));
    c.chunk_records = static_cast<std::size_t>(
        integer(r, w["chunk_records"], "writer.chunk_records", 1024, 1, 1'000'000));
    c.chunk_bytes = static_cast<std::size_t>(
        integer(r, w["chunk_bytes"], "writer.chunk_bytes", 1 << 20, 4096, 1LL << 30));

    YAML::Node sub = r.section("subscription");
    r.check_unknown(sub, {"depth"}, "subscription.");
    c.subscription_depth =
        static_cast<std::size_t>(integer(r, sub["depth"], "subscription.depth", 200, 1, 10'000));

    YAML::Node mon = r.section("monitor");
    r.check_unknown(mon,
                    {"enabled", "stop_grace_s", "fresh_grace_s", "clock_step_threshold_s",
                     "clock_offset_info_s", "period_s", "command_sources", "findings_file"},
                    "monitor.");
    c.monitor_enabled = boolean(r, mon["enabled"], "monitor.enabled", true);
    c.stop_grace_s = num(r, mon["stop_grace_s"], "monitor.stop_grace_s", 0.05, 0.0, 0.5);
    c.fresh_grace_s = num(r, mon["fresh_grace_s"], "monitor.fresh_grace_s", 0.05, 0.0, 0.5);
    c.clock_step_threshold_s = num(r, mon["clock_step_threshold_s"],
                                   "monitor.clock_step_threshold_s", 0.2, 0.0, 10.0, true);
    c.clock_offset_info_s =
        num(r, mon["clock_offset_info_s"], "monitor.clock_offset_info_s", 0.5, 0.0, 10.0, true);
    c.monitor_period_s = num(r, mon["period_s"], "monitor.period_s", 0.02, 0.0, 10.0, true);
    if (mon["command_sources"] && !mon["command_sources"].IsNull()) {
      if (!mon["command_sources"].IsMap()) {
        r.fail("monitor.command_sources must map topic -> freshness window (s)");
      }
      for (const auto& kv : mon["command_sources"]) {
        const std::string topic = kv.first.as<std::string>();
        if (c.profile.topic(topic) == nullptr) {
          r.fail("monitor.command_sources: " + topic + " is not a profile topic");
        }
        c.command_sources[topic] =
            num(r, kv.second, "monitor.command_sources." + topic, 0.5, 0.0, 10.0, true);
      }
    } else {
      for (const auto& t : c.profile.topics) {
        if (t.role == Role::cmd_vel_source) {
          c.command_sources[t.name] = 0.5;
        }
      }
    }
    if (const auto ff = opt_str(r, mon["findings_file"], "monitor.findings_file")) {
      c.findings_file = expand_user(*ff);
    }

    YAML::Node d = r.section("diagnostics");
    r.check_unknown(d, {"publish", "period_s"}, "diagnostics.");
    c.diagnostics_publish = boolean(r, d["publish"], "diagnostics.publish", true);
    c.diagnostics_period_s = num(r, d["period_s"], "diagnostics.period_s", 1.0, 0.05, 60.0);

    YAML::Node s = r.section("safety");
    r.check_unknown(s, {"forbidden_publish_topics", "forbidden_publish_prefixes"}, "safety.");
    c.forbidden_topics = builtin_forbidden_topics();
    for (auto& t : str_list(r, s["forbidden_publish_topics"], "safety.forbidden_publish_topics")) {
      if (t.empty() || t.front() != '/') {
        r.fail("safety.forbidden_publish_topics: '" + t + "' must be fully qualified");
      }
      c.forbidden_topics.push_back(std::move(t));
    }
    c.forbidden_prefixes = builtin_forbidden_prefixes();
    for (auto& p :
         str_list(r, s["forbidden_publish_prefixes"], "safety.forbidden_publish_prefixes")) {
      c.forbidden_prefixes.push_back(std::move(p));
    }

    YAML::Node ros = r.section("ros");
    r.check_unknown(ros, {"namespace", "expect_rmw", "expect_domain_id"}, "ros.");
    c.node_namespace = opt_str(r, ros["namespace"], "ros.namespace").value_or("/blackbox");
    if (c.node_namespace.empty() || c.node_namespace.front() != '/') {
      r.fail("ros.namespace must start with '/'");
    }
    c.expect_rmw = opt_str(r, ros["expect_rmw"], "ros.expect_rmw");
    if (!c.expect_rmw) {
      c.expect_rmw = c.profile.preflight.expect_rmw;
    }
    if (ros["expect_domain_id"] && !ros["expect_domain_id"].IsNull()) {
      c.expect_domain_id =
          static_cast<int>(integer(r, ros["expect_domain_id"], "ros.expect_domain_id", 0, 0, 232));
    }

    YAML::Node pf = r.section("preflight");
    r.check_unknown(pf, {"listen_s", "min_rate_fraction"}, "preflight.");
    c.preflight_listen_s =
        num(r, pf["listen_s"], "preflight.listen_s", c.profile.preflight.listen_sec, 0.5, 120.0);
    c.preflight_min_rate_fraction =
        num(r, pf["min_rate_fraction"], "preflight.min_rate_fraction", 0.5, 0.0, 1.0, true);

    for (const auto& kv : root) {
      const std::string k = kv.first.as<std::string>();
      if (r.seen().count(k) == 0U) {
        r.fail("unknown key " + k);
      }
    }
  } catch (const YAML::Exception& exc) {
    throw ConfigError(path + ": " + exc.what());
  }
  return c;
}

Json RuntimeConfig::to_json() const {
  Json sources = Json::object();
  for (const auto& [t, w] : command_sources) {
    sources[t] = w;
  }
  auto opt = [](const auto& v) { return v ? Json(*v) : Json(); };
  return {
      {"schema", kRuntimeSchema},
      {"path", path},
      {"sha256", sha256},
      {"profile", {{"path", profile_path}, {"name", profile.name}, {"sha256", profile.sha256}}},
      {"capture",
       {{"mode", capture_mode == CaptureMode::continuous ? "continuous" : "triggered"},
        {"evidence_dir", evidence_dir},
        {"session_id", opt(session_id)},
        {"experiment", opt(experiment)},
        {"hard_disk_floor_mb", hard_disk_floor_mb}}},
      {"queues",
       {{"ingest_capacity", ingest_capacity},
        {"control_reserve", control_reserve},
        {"writer_capacity", writer_capacity},
        {"drain_deadline_s", static_cast<double>(drain_deadline.count()) / 1000.0}}},
      {"writer",
       {{"fsync_every_s", static_cast<double>(fsync_every.count()) / 1000.0},
        {"chunk_records", chunk_records},
        {"chunk_bytes", chunk_bytes}}},
      {"subscription",
       {{"depth", subscription_depth}, {"reliability", "best_effort"}, {"durability", "volatile"}}},
      {"monitor",
       {{"enabled", monitor_enabled},
        {"stop_grace_s", stop_grace_s},
        {"fresh_grace_s", fresh_grace_s},
        {"clock_step_threshold_s", clock_step_threshold_s},
        {"clock_offset_info_s", clock_offset_info_s},
        {"period_s", monitor_period_s},
        {"command_sources", sources},
        {"findings_file", opt(findings_file)}}},
      {"diagnostics", {{"publish", diagnostics_publish}, {"period_s", diagnostics_period_s}}},
      {"safety",
       {{"forbidden_publish_topics", forbidden_topics},
        {"forbidden_publish_prefixes", forbidden_prefixes}}},
      {"ros",
       {{"namespace", node_namespace},
        {"expect_rmw", opt(expect_rmw)},
        {"expect_domain_id", opt(expect_domain_id)}}},
      {"preflight",
       {{"listen_s", preflight_listen_s}, {"min_rate_fraction", preflight_min_rate_fraction}}}};
}

}  // namespace blackboxrs
