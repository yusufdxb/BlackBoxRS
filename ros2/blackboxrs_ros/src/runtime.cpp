#include "blackboxrs_ros/runtime.hpp"

#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <ctime>

#include "blackboxrs/build_info.hpp"

namespace blackboxrs_ros {
using blackboxrs::Json;

rclcpp::NodeOptions passive_node_options(const std::vector<std::string>& ros_args) {
  rclcpp::NodeOptions o;
  o.start_parameter_services(false);
  o.start_parameter_event_publisher(false);
  o.enable_rosout(false);
  if (!ros_args.empty()) {
    o.arguments(ros_args);
  }
  return o;
}

std::string config_path_from_args(const std::vector<std::string>& args) {
  for (std::size_t i = 0; i + 1 < args.size(); ++i) {
    if (args[i] == "--config") {
      return args[i + 1];
    }
  }
  for (const auto& a : args) {
    if (a.rfind("--config=", 0) == 0) {
      return a.substr(9);
    }
  }
  const char* env = std::getenv("BLACKBOXRS_CONFIG");
  return env != nullptr ? env : "";
}

std::string qos_string(const rclcpp::QoS& q) {
  const auto& p = q.get_rmw_qos_profile();
  const char* rel = p.reliability == RMW_QOS_POLICY_RELIABILITY_RELIABLE      ? "reliable"
                    : p.reliability == RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT ? "best_effort"
                                                                              : "other";
  const char* dur = p.durability == RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL ? "transient_local"
                    : p.durability == RMW_QOS_POLICY_DURABILITY_VOLATILE      ? "volatile"
                                                                              : "other";
  return std::string(rel) + "/" + dur;
}

GraphProbe::GraphProbe(const blackboxrs::Profile& profile, std::string own_namespace)
    : profile_(profile), own_ns_(std::move(own_namespace)) {
  for (const auto& t : profile_.topics) {
    status_[t.name] = {{"status", "absent"}, {"reason", "not seen on the graph yet"}};
  }
}

void GraphProbe::mark_type_unavailable(const std::string& topic, const std::string& why) {
  status_[topic] = {{"status", "type_unavailable"}, {"reason", why}};
}

std::vector<rclcpp::TopicEndpointInfo> GraphProbe::publishers(rclcpp::Node& node,
                                                              const std::string& topic,
                                                              const std::string& own_namespace) {
  auto infos = node.get_publishers_info_by_topic(topic);
  auto strip = [](std::string ns) {
    while (ns.size() > 1 && ns.back() == '/') {
      ns.pop_back();
    }
    return ns;
  };
  std::erase_if(infos, [&](const rclcpp::TopicEndpointInfo& i) {
    return strip(i.node_namespace()) == strip(own_namespace);
  });
  return infos;
}

blackboxrs::recorder::GraphSnapshot GraphProbe::probe(rclcpp::Node& node) {
  blackboxrs::recorder::GraphSnapshot g;
  g.t_mono = blackboxrs::clock_domain::Mono::now();
  g.t_wall = blackboxrs::clock_domain::Wall::now();
  auto join = [](const std::string& ns, const std::string& name) {
    std::string n = ns;
    while (!n.empty() && n.back() == '/') {
      n.pop_back();
    }
    return n + "/" + name;
  };
  for (const auto& [name, ns] : node.get_node_graph_interface()->get_node_names_and_namespaces()) {
    g.nodes.push_back(join(ns, name));
  }
  std::sort(g.nodes.begin(), g.nodes.end());
  const auto graph_types = node.get_topic_names_and_types();
  for (const auto& spec : profile_.topics) {
    Json& st = status_[spec.name];
    const auto infos = publishers(node, spec.name, own_ns_);
    std::vector<std::string> pubs;
    std::set<std::string> types;
    std::set<std::string> qos;
    for (const auto& i : infos) {
      pubs.push_back(join(i.node_namespace(), i.node_name()));
      types.insert(i.topic_type());
      qos.insert(qos_string(i.qos_profile()));
    }
    std::sort(pubs.begin(), pubs.end());
    st["publishers"] = pubs;
    st["graph_types"] =
        types.empty() ? Json() : Json(std::vector<std::string>(types.begin(), types.end()));
    if (st.value("status", std::string()) == "type_unavailable") {
      continue;
    }
    if (infos.empty()) {
      if (st.value("ever_published", false)) {
        st["left_graph"] = true;
      } else {
        st["status"] = graph_types.count(spec.name) != 0U ? "no_publishers" : "absent";
        st["reason"] = "no publisher on the graph";
      }
      continue;
    }
    g.publishers[spec.name] = pubs;
    if (types.count(spec.type) == 0U) {
      st["status"] = "type_mismatch";
      st["reason"] = "publishers use " +
                     Json(std::vector<std::string>(types.begin(), types.end())).dump() +
                     ", profile expects " + spec.type;
      continue;
    }
    st["status"] = "subscribed";
    st["ever_published"] = true;
    st["left_graph"] = false;
    st["publisher_qos"] = std::vector<std::string>(qos.begin(), qos.end());
    st["reason"] = types.size() > 1 ? Json("mixed publisher types") : Json();
  }
  for (const auto& [topic, st] : status_) {
    g.topic_status[topic] = st;
  }
  for (const auto& [name, t] : graph_types) {
    g.topics[name] = t;
  }
  return g;
}

std::string hostname() {
  char buf[256] = {};
  if (::gethostname(buf, sizeof buf - 1) != 0) {
    return "unknown";
  }
  return buf;
}

std::string make_session_id() {
  const std::time_t now = std::time(nullptr);
  std::tm tm{};
  gmtime_r(&now, &tm);
  char buf[32];
  std::strftime(buf, sizeof buf, "%Y%m%dT%H%M%SZ", &tm);
  return std::string(buf) + "_" + hostname();
}

Json build_json() {
  return {{"implementation", "blackboxrs-cpp"},
          {"version", blackboxrs::build_info::kVersion},
          {"git_sha", blackboxrs::build_info::kGitSha},
          {"git_dirty", blackboxrs::build_info::kGitDirty},
          {"build_type", blackboxrs::build_info::kBuildType},
          {"compiler", blackboxrs::build_info::kCompiler}};
}

blackboxrs::recorder::ManifestContext manifest_context(const blackboxrs::RuntimeConfig& cfg,
                                                       const std::string& session_id,
                                                       const std::string& mode, bool use_sim_time) {
  blackboxrs::recorder::ManifestContext m;
  const char* rmw = std::getenv("RMW_IMPLEMENTATION");
  const char* domain = std::getenv("ROS_DOMAIN_ID");
  m.session = {
      {"session_id", session_id},
      {"host", hostname()},
      {"recorder", "blackboxrs-cpp"},
      {"runtime_mode", mode},
      {"capture_mode",
       cfg.capture_mode == blackboxrs::CaptureMode::continuous ? "continuous" : "triggered"},
      {"experiment", cfg.experiment ? Json(*cfg.experiment) : Json()},
      {"synthetic", false},
      {"use_sim_time", use_sim_time},
      {"rmw_implementation", rmw != nullptr ? Json(rmw) : Json()},
      {"ros_domain_id", domain != nullptr ? Json(domain) : Json()},
      {"started_wall_ns", blackboxrs::count_ns(blackboxrs::clock_domain::Wall::now())},
      {"config", {{"path", cfg.path}, {"sha256", cfg.sha256}}},
      {"build", build_json()}};
  m.profile = blackboxrs::profile_manifest_block(cfg.profile, cfg.profile_path);
  m.config_sha256 = cfg.sha256;
  m.writer_build = build_json();
  return m;
}

}  // namespace blackboxrs_ros
