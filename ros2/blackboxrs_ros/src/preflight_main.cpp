// blackboxrs preflight: GO / NO-GO for a hardware session, without motion.
//
// ros2 run blackboxrs_ros preflight --config configs/go2_hardware.yaml [--json] [--listen S]
//
// Checks what can be checked without moving the robot: configuration, build,
// evidence directory, disk, clocks, RMW and the CycloneDDS interface binding,
// the ROS graph (publishers, types, data actually flowing), a live self-test of
// the real recorder (it records for the listen period, then the bundle must
// validate, nothing may be dropped, and its CPU and memory must fit the
// profile's budget), and that no BlackBoxRS node publishes on a motion or
// control topic. Preflight itself creates no publisher.
//
// Exit 0: GO (possibly with warnings). Exit 1: NO-GO. The reasons are printed.

#include <sys/resource.h>
#include <unistd.h>

#include <rmw/rmw.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <regex>
#include <sstream>

#include "blackboxrs/build_info.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "blackboxrs_ros/recorder_node.hpp"
#include "blackboxrs_ros/runtime.hpp"

namespace fs = std::filesystem;
using blackboxrs::Json;

namespace {

struct Checks {
  Json items = Json::array();
  void add(const std::string& id, const std::string& status, const std::string& summary,
           Json detail = Json::object()) {
    items.push_back({{"id", id}, {"status", status}, {"summary", summary}, {"detail", detail}});
  }
  [[nodiscard]] std::string verdict() const {
    for (const auto& i : items) {
      if (i["status"] == "FAIL") {
        return "NO-GO";
      }
    }
    return "GO";
  }
  [[nodiscard]] std::size_t count(const char* s) const {
    return static_cast<std::size_t>(
        std::count_if(items.begin(), items.end(), [&](const Json& i) { return i["status"] == s; }));
  }
};

double thread_cpu_s(int tid) {
  if (tid <= 0) {
    return 0.0;
  }
  std::ifstream in("/proc/self/task/" + std::to_string(tid) + "/stat");
  std::string line;
  std::getline(in, line);
  const auto rp = line.rfind(')');
  if (rp == std::string::npos) {
    return 0.0;
  }
  std::istringstream ss(line.substr(rp + 2));
  std::string f;
  std::uint64_t ut = 0;
  std::uint64_t st = 0;
  for (int i = 3; i <= 15 && ss >> f; ++i) {
    if (i == 14) ut = std::stoull(f);
    if (i == 15) st = std::stoull(f);
  }
  return static_cast<double>(ut + st) / static_cast<double>(::sysconf(_SC_CLK_TCK));
}

// NetworkInterface names a CycloneDDS configuration binds to.
std::vector<std::string> cyclone_interfaces(std::string uri) {
  std::string xml;
  if (uri.rfind("file://", 0) == 0) {
    std::ifstream in(uri.substr(7));
    std::stringstream ss;
    ss << in.rdbuf();
    xml = ss.str();
  } else if (!uri.empty() && uri.front() == '<') {
    xml = uri;
  } else if (!uri.empty()) {
    std::ifstream in(uri);
    std::stringstream ss;
    ss << in.rdbuf();
    xml = ss.str();
  }
  std::vector<std::string> out;
  const std::regex a(R"re(NetworkInterface[^>]*name\s*=\s*"([^"]+)")re");
  const std::regex b(R"re(<NetworkInterfaceAddress>\s*([^<\s]+)\s*</NetworkInterfaceAddress>)re");
  for (const auto* re : {&a, &b}) {
    for (auto it = std::sregex_iterator(xml.begin(), xml.end(), *re); it != std::sregex_iterator();
         ++it) {
      out.push_back((*it)[1].str());
    }
  }
  return out;
}

}  // namespace

int main(int argc, char** argv) {
  const auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  bool as_json = false;
  std::optional<double> listen;
  for (std::size_t i = 1; i < args.size(); ++i) {
    if (args[i] == "--json") as_json = true;
    if (args[i] == "--listen" && i + 1 < args.size()) listen = std::stod(args[i + 1]);
  }
  Checks c;
  const std::string path = blackboxrs_ros::config_path_from_args(args);
  blackboxrs::RuntimeConfig cfg;
  bool cfg_ok = false;
  // P1 configuration
  if (path.empty()) {
    c.add("P1_config", "FAIL", "no --config given");
  } else {
    try {
      cfg = blackboxrs::load_runtime_config(path);
      cfg_ok = true;
      c.add(
          "P1_config", "PASS",
          "configuration valid (" + cfg.profile.name + ", sha256 " + cfg.sha256.substr(0, 12) + ")",
          {{"path", path}, {"sha256", cfg.sha256}, {"profile", cfg.profile_path}});
    } catch (const blackboxrs::ConfigError& exc) {
      c.add("P1_config", "FAIL", exc.what());
    }
  }
  // P2 build
  {
    const bool release = std::string(blackboxrs::build_info::kBuildType) == "Release";
    c.add("P2_build", release && !blackboxrs::build_info::kGitDirty ? "PASS" : "WARN",
          std::string("blackboxrs ") + blackboxrs::build_info::kVersion + " " +
              blackboxrs::build_info::kGitSha +
              (blackboxrs::build_info::kGitDirty ? " (dirty)" : "") + ", " +
              blackboxrs::build_info::kBuildType,
          blackboxrs_ros::build_json());
  }
  // P3 clocks (the payload computer has no RTC and has booted at 1970)
  {
    const auto wall = blackboxrs::count_ns(blackboxrs::clock_domain::Wall::now());
    const double year = 1970.0 + static_cast<double>(wall) / 1e9 / (365.2425 * 86400.0);
    if (year < 2025.0) {
      c.add("P3_clock", "FAIL",
            "wall clock reads year " + std::to_string(static_cast<int>(year)) +
                ": set the clock before recording (no RTC on the payload)");
    } else {
      c.add("P3_clock", "PASS",
            "wall clock plausible (year " + std::to_string(static_cast<int>(year)) + ")");
    }
  }
  // P4 RMW / domain / DDS interface
  {
    const std::string rmw = rmw_get_implementation_identifier();
    if (cfg_ok && cfg.expect_rmw && *cfg.expect_rmw != rmw) {
      c.add("P4_rmw", "FAIL", "RMW is " + rmw + ", configuration expects " + *cfg.expect_rmw);
    } else {
      c.add("P4_rmw", "PASS", "RMW " + rmw);
    }
    const char* dom = std::getenv("ROS_DOMAIN_ID");
    const int domain = dom != nullptr ? std::atoi(dom) : 0;
    if (cfg_ok && cfg.expect_domain_id && *cfg.expect_domain_id != domain) {
      c.add("P4_domain", "FAIL",
            "ROS_DOMAIN_ID " + std::to_string(domain) + ", expected " +
                std::to_string(*cfg.expect_domain_id));
    } else {
      c.add("P4_domain", "PASS", "ROS_DOMAIN_ID " + std::to_string(domain));
    }
    const char* uri = std::getenv("CYCLONEDDS_URI");
    if (rmw.find("cyclonedds") != std::string::npos && uri != nullptr) {
      std::vector<std::string> missing;
      const auto ifaces = cyclone_interfaces(uri);
      for (const auto& n : ifaces) {
        if (n.find('.') == std::string::npos && !fs::exists("/sys/class/net/" + n)) {
          missing.push_back(n);
        }
      }
      if (!missing.empty()) {
        c.add("P4_dds_interface", "FAIL",
              "CYCLONEDDS_URI binds to interface(s) that do not exist on this host: " +
                  Json(missing).dump() +
                  " (topics would be advertised with no data; see the GO2 field notes)");
      } else {
        c.add("P4_dds_interface", "PASS",
              ifaces.empty() ? "CYCLONEDDS_URI names no interface"
                             : "DDS interfaces exist: " + Json(ifaces).dump());
      }
    }
  }
  if (!cfg_ok) {
    Json out = {{"verdict", c.verdict()}, {"checks", c.items}};
    std::cout << (as_json ? out.dump(2) : "NO-GO: configuration invalid") << "\n";
    rclcpp::shutdown();
    return 1;
  }
  const double listen_s = listen.value_or(cfg.preflight_listen_s);
  // P5 evidence directory and disk
  {
    std::error_code ec;
    fs::create_directories(cfg.evidence_dir, ec);
    const fs::path probe =
        fs::path(cfg.evidence_dir) / (".preflight_probe_" + std::to_string(::getpid()));
    std::ofstream(probe) << "probe";
    const bool writable = fs::exists(probe);
    fs::remove(probe, ec);
    c.add("P5_evidence_dir", writable ? "PASS" : "FAIL",
          cfg.evidence_dir + (writable ? " writable" : " not writable"));
    const auto space = fs::space(cfg.evidence_dir, ec);
    const auto free_mb = ec ? 0 : static_cast<std::int64_t>(space.available / (1024 * 1024));
    const char* st = free_mb < cfg.hard_disk_floor_mb         ? "FAIL"
                     : free_mb < cfg.profile.min_free_disk_mb ? "WARN"
                                                              : "PASS";
    c.add("P5_disk", st,
          std::to_string(free_mb) + " MB free (floor " + std::to_string(cfg.hard_disk_floor_mb) +
              ", profile minimum " + std::to_string(cfg.profile.min_free_disk_mb) + ")");
  }
  // P6 live self-test with the real recorder node (diagnostics off: no publishers).
  blackboxrs::RuntimeConfig test_cfg = cfg;
  test_cfg.capture_mode = blackboxrs::CaptureMode::continuous;
  test_cfg.diagnostics_publish = false;
  test_cfg.evidence_dir = (fs::path(cfg.evidence_dir) / "_preflight").string();
  auto node = std::make_shared<blackboxrs_ros::RecorderNode>(
      test_cfg, blackboxrs_ros::passive_node_options({}), blackboxrs_ros::make_session_id());
  rclcpp::executors::SingleThreadedExecutor exec;
  exec.add_node(node);
  rusage ru0{};
  getrusage(RUSAGE_SELF, &ru0);
  const auto t0 = std::chrono::steady_clock::now();
  double p_cpu0 = -1.0;
  double w_cpu0 = -1.0;
  int pt = 0;
  int wt = 0;
  while (rclcpp::ok() &&
         std::chrono::steady_clock::now() - t0 < std::chrono::duration<double>(listen_s)) {
    exec.spin_some(std::chrono::milliseconds(20));
    if (p_cpu0 < 0) {
      std::tie(pt, wt) = node->recorder_thread_ids();
      if (pt != 0 && wt != 0) {
        p_cpu0 = thread_cpu_s(pt);
        w_cpu0 = thread_cpu_s(wt);
      }
    }
  }
  const double elapsed =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  const double pipe_cpu = p_cpu0 >= 0 ? thread_cpu_s(pt) - p_cpu0 : 0.0;
  const double writer_cpu = w_cpu0 >= 0 ? thread_cpu_s(wt) - w_cpu0 : 0.0;
  rusage ru1{};
  getrusage(RUSAGE_SELF, &ru1);
  const double proc_cpu = static_cast<double>(ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec +
                                              ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) +
                          static_cast<double>(ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec +
                                              ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) /
                              1e6;
  // P7 graph: nobody from BlackBoxRS publishes on a control topic; preflight has no publisher.
  {
    std::vector<std::string> offenders;
    Json control = Json::object();
    for (const auto& t : cfg.forbidden_topics) {
      for (const auto& info : node->get_publishers_info_by_topic(t)) {
        const std::string who = info.node_namespace() + "/" + info.node_name();
        control[t].push_back(who);
        if (info.node_namespace().rfind(cfg.node_namespace, 0) == 0 ||
            info.node_name().rfind("blackboxrs", 0) == 0) {
          offenders.push_back(who + " -> " + t);
        }
      }
    }
    const auto own = node->get_node_graph_interface()->get_publisher_names_and_types_by_node(
        node->get_name(), node->get_namespace());
    if (!offenders.empty()) {
      c.add("P7_no_control_publishers", "FAIL",
            "BlackBoxRS publishes on control topics: " + Json(offenders).dump());
    } else if (!own.empty()) {
      c.add("P7_no_control_publishers", "FAIL",
            "the preflight recorder has publishers: " + Json(own.size()).dump());
    } else {
      c.add("P7_no_control_publishers", "PASS",
            "no BlackBoxRS publisher on any motion or control topic; the recorder under test has "
            "none at all",
            {{"control_topic_publishers", control}});
    }
    // HELIX preflight C6: /cmd_vel must have the sport sink as its only subscriber.
    std::set<std::string> nodes;
    for (const auto& [n, ns] : node->get_node_graph_interface()->get_node_names_and_namespaces()) {
      nodes.insert((ns == "/" ? "" : ns) + "/" + n);
    }
    const bool helix =
        nodes.count("/helix_arbiter") != 0U || nodes.count("/helix_go2_sport_sink") != 0U;
    if (helix && cfg.profile.topic("/cmd_vel") != nullptr) {
      c.add("P7_helix_c6", "FAIL",
            "HELIX is running and the profile subscribes /cmd_vel (HELIX preflight C6 would refuse "
            "its stages); use go2_helix");
    } else {
      c.add("P7_helix_c6", "PASS",
            helix ? "HELIX running; profile leaves /cmd_vel alone" : "HELIX not running");
    }
  }
  exec.remove_node(node);
  node->stop("preflight_done");
  const auto m = node->metrics();
  const auto bundles = node->bundles();
  // P8 topics: present, typed right, data flowing at the expected rate.
  {
    for (std::size_t i = 0; i < cfg.profile.topics.size(); ++i) {
      const auto& spec = cfg.profile.topics[i];
      const auto received = m.topics[i].received;
      const double hz = static_cast<double>(received) / elapsed;
      const auto pubs =
          blackboxrs_ros::GraphProbe::publishers(*node, spec.name, cfg.node_namespace);
      std::string st = "PASS";
      std::string why;
      if (pubs.empty()) {
        st = spec.required ? "FAIL" : "WARN";
        why = "no publisher";
      } else if (std::none_of(pubs.begin(), pubs.end(),
                              [&](const auto& p) { return p.topic_type() == spec.type; })) {
        st = "FAIL";
        why = "publisher type " + pubs.front().topic_type() + " != " + spec.type;
      } else if (received == 0) {
        st = spec.required ? "FAIL" : "WARN";
        why =
            "advertised but no data arrived (the CycloneDDS wrong-interface signature, or an idle "
            "event topic)";
      } else if (spec.expected_hz && hz < cfg.preflight_min_rate_fraction * *spec.expected_hz) {
        st = spec.required ? "FAIL" : "WARN";
        why = "rate " + std::to_string(hz) + " Hz below " +
              std::to_string(cfg.preflight_min_rate_fraction) + " x expected " +
              std::to_string(*spec.expected_hz);
      } else {
        why = std::to_string(received) + " messages, " + std::to_string(hz) + " Hz";
      }
      c.add("P8_topic " + spec.name, st, why,
            {{"received", received},
             {"rate_hz", hz},
             {"publishers", pubs.size()},
             {"required", spec.required}});
    }
  }
  // P9 recorder self-test result and resource budget.
  {
    std::string validation = "no bundle";
    if (!bundles.empty()) {
      validation = blackboxrs::validate_bundle(bundles.front()).status ==
                           blackboxrs::ValidationStatus::verified
                       ? "verified"
                       : "invalid";
    }
    c.add("P9_selftest_evidence", validation == "verified" ? "PASS" : "FAIL",
          "self-test bundle " + validation,
          {{"bundles", bundles}, {"records_written", m.writer.records_written}});
    const std::uint64_t dropped = m.dropped_ingest + m.dropped_at_shutdown;
    c.add("P9_selftest_drops", dropped == 0 ? "PASS" : "FAIL",
          std::to_string(dropped) + " message(s) dropped during the self-test");
    const double rec_cpu = 100.0 * (pipe_cpu + writer_cpu) / elapsed;
    const double all_cpu = 100.0 * proc_cpu / elapsed;
    const double budget = cfg.profile.preflight.max_recorder_cpu_percent;
    c.add("P9_cpu", all_cpu <= budget ? "PASS" : "FAIL",
          "recorder process " + std::to_string(all_cpu) +
              "% of one core (pipeline+writer threads " + std::to_string(rec_cpu) + "%), budget " +
              std::to_string(budget) + "%",
          {{"process_percent", all_cpu},
           {"pipeline_writer_percent", rec_cpu},
           {"budget_percent", budget}});
    const double rss = blackboxrs::recorder::current_rss_mb().value_or(0.0);
    c.add("P9_rss", rss <= cfg.profile.preflight.max_recorder_rss_mb ? "PASS" : "FAIL",
          std::to_string(rss) + " MB resident, budget " +
              std::to_string(cfg.profile.preflight.max_recorder_rss_mb) + " MB");
  }
  node.reset();
  const std::string verdict = c.verdict();
  Json out = {{"schema", "blackboxrs.preflight.v1"},
              {"verdict", verdict},
              {"warnings", c.count("WARN")},
              {"failures", c.count("FAIL")},
              {"listen_s", elapsed},
              {"config", {{"path", cfg.path}, {"sha256", cfg.sha256}}},
              {"build", blackboxrs_ros::build_json()},
              {"host", blackboxrs_ros::hostname()},
              {"checks", c.items}};
  if (as_json) {
    std::cout << out.dump(2) << "\n";
  } else {
    for (const auto& i : c.items) {
      std::printf("  %-5s %-34s %s\n", i["status"].get<std::string>().c_str(),
                  i["id"].get<std::string>().c_str(), i["summary"].get<std::string>().c_str());
    }
    std::printf("\nPREFLIGHT %s  (%zu failure(s), %zu warning(s)); preflight commanded nothing\n",
                verdict.c_str(), c.count("FAIL"), c.count("WARN"));
  }
  rclcpp::shutdown();
  return verdict == "GO" ? 0 : 1;
}
