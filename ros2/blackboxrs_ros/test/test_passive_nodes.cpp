// Hardware-facing safety of the ROS nodes, checked on a live ROS graph.
//
// * the recorder records what is published and finalizes verified evidence;
// * the recorder, with diagnostics on, publishes only on /diagnostics and
//   /blackboxrs/status, and with diagnostics off publishes nothing at all;
// * no BlackBoxRS node ever appears as a publisher of a motion or control
//   topic;
// * the publish guard refuses a remap of an allowed topic onto /cmd_vel
//   before any publisher exists;
// * the recorder executable finalizes its evidence on SIGINT;
// * the monitor executable reports a stale command forwarded to the output;
// * the preflight executable gives a verdict and has no publishers.
//
// Runs on its own ROS_DOMAIN_ID so it cannot see or disturb anything else.

#include <fcntl.h>
#include <gtest/gtest.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <filesystem>
#include <fstream>
#include <geometry_msgs/msg/twist.hpp>
#include <map>
#include <nav_msgs/msg/odometry.hpp>
#include <random>
#include <set>
#include <sstream>
#include <std_msgs/msg/string.hpp>
#include <thread>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "blackboxrs/replay/engine.hpp"
#include "blackboxrs_ros/guarded_publisher.hpp"
#include "blackboxrs_ros/recorder_node.hpp"

namespace blackboxrs_ros {
namespace {
namespace fs = std::filesystem;
using namespace std::chrono_literals;

class Env : public ::testing::Environment {
 public:
  void SetUp() override {
    std::random_device rd;
    domain_ = 100 + static_cast<int>(rd() % 100);
    ::setenv("ROS_DOMAIN_ID", std::to_string(domain_).c_str(), 1);
    rclcpp::init(0, nullptr);
  }
  void TearDown() override { rclcpp::shutdown(); }
  int domain_ = 0;
};

fs::path temp_dir() {
  std::random_device rd;
  const fs::path p = fs::temp_directory_path() / ("bbrs_ros_" + std::to_string(rd()));
  fs::create_directories(p);
  return p;
}

// A runtime configuration for the test profile, evidence under `dir`.
std::string write_config(const fs::path& dir, const std::string& extra = "") {
  const fs::path cfg = dir / "runtime.yaml";
  std::ofstream(cfg) << "schema: blackboxrs.runtime.v1\n"
                     << "profile: " << BLACKBOXRS_ROS_TEST_DIR << "/test_profile.yaml\n"
                     << "capture: {mode: continuous, evidence_dir: " << (dir / "evidence").string()
                     << ", hard_disk_floor_mb: 1}\n"
                     << "ros: {namespace: /blackbox_test}\n"
                     << extra;
  return cfg.string();
}

std::vector<fs::path> bundles_in(const fs::path& evidence) {
  std::vector<fs::path> out;
  if (!fs::exists(evidence)) {
    return out;
  }
  for (const auto& session : fs::directory_iterator(evidence)) {
    if (!session.is_directory()) continue;
    for (const auto& b : fs::directory_iterator(session.path())) {
      if (b.is_directory()) out.push_back(b.path());
    }
  }
  return out;
}

geometry_msgs::msg::Twist twist(double x) {
  geometry_msgs::msg::Twist t;
  t.linear.x = x;
  return t;
}

// Publishes the test topics from a separate node until stopped.
class Traffic {
 public:
  explicit Traffic(bool keep_output_after_nav_stops = false) {
    node_ = std::make_shared<rclcpp::Node>("bbrs_test_publisher");
    nav_ = node_->create_publisher<geometry_msgs::msg::Twist>("/bbrs_test/nav", 10);
    out_ = node_->create_publisher<geometry_msgs::msg::Twist>("/bbrs_test/cmd_out", 10);
    odom_ = node_->create_publisher<nav_msgs::msg::Odometry>("/bbrs_test/odom", 10);
    text_ = node_->create_publisher<std_msgs::msg::String>("/bbrs_test/text", 10);
    keep_output_ = keep_output_after_nav_stops;
  }
  // Publish for `d`; navigation stops after `nav_for` (the output keeps
  // repeating the last command when keep_output is set: a stale command).
  void run(std::chrono::milliseconds d, std::chrono::milliseconds nav_for = std::chrono::hours(1)) {
    const auto t0 = std::chrono::steady_clock::now();
    int k = 0;
    while (std::chrono::steady_clock::now() - t0 < d) {
      const bool nav_on = std::chrono::steady_clock::now() - t0 < nav_for;
      if (nav_on) {
        nav_->publish(twist(0.2));
        ++sent_nav;
      }
      if (nav_on || keep_output_) {
        out_->publish(twist(0.2));
      }
      nav_msgs::msg::Odometry o;
      o.header.frame_id = "odom";
      o.pose.pose.position.x = 0.004 * k;
      o.twist.twist.linear.x = 0.2;
      odom_->publish(o);
      ++sent_odom;
      if (k % 10 == 0) {
        std_msgs::msg::String s;
        s.data = "tick " + std::to_string(k);
        text_->publish(s);
      }
      ++k;
      std::this_thread::sleep_for(20ms);
    }
  }
  int sent_nav = 0;
  int sent_odom = 0;

 private:
  std::shared_ptr<rclcpp::Node> node_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr nav_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr out_;
  rclcpp::Publisher<nav_msgs::msg::Odometry>::SharedPtr odom_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr text_;
  bool keep_output_ = false;
};

void wait_for_subscribers(rclcpp::Node& probe, const std::string& topic, std::size_t n) {
  for (int i = 0; i < 100 && probe.count_subscribers(topic) < n; ++i) {
    std::this_thread::sleep_for(50ms);
  }
}

// Every topic a node publishes on, by querying the graph for that node.
std::set<std::string> published_by(rclcpp::Node& probe, const std::string& name,
                                   const std::string& ns) {
  std::set<std::string> out;
  for (int i = 0; i < 40; ++i) {
    try {
      for (const auto& [topic, types] :
           probe.get_node_graph_interface()->get_publisher_names_and_types_by_node(name, ns)) {
        out.insert(topic);
      }
      return out;
    } catch (const std::exception&) {
      std::this_thread::sleep_for(50ms);  // node not discovered yet
    }
  }
  return out;
}

TEST(RecorderNode, RecordsAndFinalizesVerifiedEvidence) {
  const fs::path dir = temp_dir();
  const auto cfg =
      blackboxrs::load_runtime_config(write_config(dir, "diagnostics: {publish: false}\n"));
  auto node = std::make_shared<RecorderNode>(cfg, passive_node_options({}));
  EXPECT_EQ(node->subscribed_topics(), 4U) << "the uninstalled type is skipped, not fatal";
  rclcpp::executors::SingleThreadedExecutor exec;
  exec.add_node(node);
  std::jthread spinner([&](std::stop_token st) {
    while (!st.stop_requested()) exec.spin_some(10ms);
  });
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe");
  wait_for_subscribers(*probe, "/bbrs_test/odom", 1);
  Traffic traffic;
  traffic.run(1500ms);
  std::this_thread::sleep_for(300ms);
  spinner.request_stop();
  spinner.join();
  exec.remove_node(node);
  node->stop("test");
  const auto m = node->metrics();
  EXPECT_EQ(m.dropped_ingest, 0U);
  EXPECT_GE(m.topics[2].received, static_cast<std::uint64_t>(traffic.sent_odom * 9 / 10))
      << "best effort on loopback: nearly everything arrives";
  const auto bundles = bundles_in(dir / "evidence");
  ASSERT_EQ(bundles.size(), 1U);
  EXPECT_EQ(blackboxrs::validate_bundle(bundles[0]).status, blackboxrs::ValidationStatus::verified);
  const auto b = blackboxrs::load_bundle(bundles[0]);
  EXPECT_EQ(b.manifest["status"], "complete");
  EXPECT_EQ(b.manifest["topic_status"]["/bbrs_test/missing_type"]["status"], "type_unavailable");
  EXPECT_EQ(b.manifest["topic_status"]["/bbrs_test/odom"]["status"], "subscribed");
  bool has_src = false;
  for (const auto& r : b.records) {
    if (r["kind"] == "msg" && r["topic"] == "/bbrs_test/odom") {
      EXPECT_FALSE(r["data"]["header"].contains("frame_id")) << "only profile fields are stored";
      has_src = has_src || (r["dds_src_ns"].is_number() && r["dds_rx_ns"].is_number());
    }
  }
  EXPECT_TRUE(has_src) << "DDS source and reception timestamps are captured";
  // The recorded evidence replays through the C++ engine.
  const auto ev = blackboxrs::load_evidence(bundles[0], false);
  blackboxrs::replay::ReplayConfig rc;
  rc.sut_mode = "observed";
  const auto result = blackboxrs::replay::replay(ev, rc);
  EXPECT_EQ(result["invariants"]["finite_output"]["status"], "PASS")
      << result["invariants"].dump(1);
  fs::remove_all(dir);
}

TEST(RecorderNode, PublishesOnlyDiagnosticsAndNothingWhenDisabled) {
  const fs::path dir = temp_dir();
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_pub");
  {
    const auto cfg = blackboxrs::load_runtime_config(write_config(dir));
    auto node = std::make_shared<RecorderNode>(cfg, passive_node_options({}));
    const auto topics = published_by(*probe, "blackboxrs_recorder", "/blackbox_test");
    EXPECT_EQ(topics, (std::set<std::string>{"/diagnostics", "/blackboxrs/status"}));
    for (const auto& t : blackboxrs::builtin_forbidden_topics()) {
      for (const auto& info : probe->get_publishers_info_by_topic(t)) {
        ADD_FAILURE() << info.node_name() << " publishes on control topic " << t;
      }
    }
  }
  {
    const auto cfg =
        blackboxrs::load_runtime_config(write_config(dir, "diagnostics: {publish: false}\n"));
    auto node = std::make_shared<RecorderNode>(cfg, passive_node_options({}));
    std::this_thread::sleep_for(300ms);
    EXPECT_TRUE(published_by(*probe, "blackboxrs_recorder", "/blackbox_test").empty())
        << "no /rosout, no /parameter_events, no diagnostics: no publisher at all";
  }
  fs::remove_all(dir);
}

TEST(PublishGuard, RemapOntoAControlTopicIsRefusedBeforeAdvertising) {
  const fs::path dir = temp_dir();
  const auto cfg = blackboxrs::load_runtime_config(write_config(dir));
  auto opts = passive_node_options({"--ros-args", "-r", "/blackboxrs/replay/timeline:=/cmd_vel"});
  auto node = std::make_shared<rclcpp::Node>("bbrs_guard_test", "/blackbox_test", opts);
  EXPECT_THROW((void)guarded_publisher<std_msgs::msg::String>(
                   *node, cfg, "/blackboxrs/replay/timeline", rclcpp::QoS(1)),
               ForbiddenPublisher);
  EXPECT_THROW((void)guarded_publisher<geometry_msgs::msg::Twist>(*node, cfg, "/nav/cmd_vel",
                                                                  rclcpp::QoS(1)),
               ForbiddenPublisher);
  EXPECT_THROW(
      (void)guarded_publisher<std_msgs::msg::String>(*node, cfg, "/some/topic", rclcpp::QoS(1)),
      ForbiddenPublisher);
  EXPECT_EQ(node->count_publishers("/cmd_vel"), 0U) << "nothing was advertised";
  EXPECT_NO_THROW(
      (void)guarded_publisher<std_msgs::msg::String>(*node, cfg, "/blackboxrs/ok", rclcpp::QoS(1)));
  fs::remove_all(dir);
}

// Start an installed executable with arguments; returns its pid.
pid_t spawn(const std::string& exe, const std::vector<std::string>& args, const fs::path& log) {
  const pid_t pid = ::fork();
  if (pid == 0) {
    std::vector<char*> argv;
    std::string e = exe;
    argv.push_back(e.data());
    std::vector<std::string> a = args;
    for (auto& s : a) argv.push_back(s.data());
    argv.push_back(nullptr);
    const int fd = ::open(log.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    ::dup2(fd, 1);
    ::dup2(fd, 2);
    ::execv(exe.c_str(), argv.data());
    ::_exit(127);
  }
  return pid;
}

int wait_exit(pid_t pid, std::chrono::seconds timeout) {
  const auto t0 = std::chrono::steady_clock::now();
  int status = 0;
  while (std::chrono::steady_clock::now() - t0 < timeout) {
    if (::waitpid(pid, &status, WNOHANG) == pid) {
      return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    }
    std::this_thread::sleep_for(50ms);
  }
  ::kill(pid, SIGKILL);
  ::waitpid(pid, &status, 0);
  return -1;
}

TEST(RecorderExecutable, SigintFinalizesEvidence) {
  const fs::path dir = temp_dir();
  const std::string cfg = write_config(dir);
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/recorder", {"--config", cfg},
                          dir / "recorder.log");
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_sigint");
  wait_for_subscribers(*probe, "/bbrs_test/odom", 1);
  Traffic traffic;
  traffic.run(1000ms);
  ASSERT_EQ(::kill(pid, SIGINT), 0);  // stop while data is still arriving
  traffic.run(200ms);
  EXPECT_EQ(wait_exit(pid, 30s), 0) << std::ifstream(dir / "recorder.log").rdbuf();
  const auto bundles = bundles_in(dir / "evidence");
  ASSERT_EQ(bundles.size(), 1U);
  EXPECT_FALSE(bundles[0].string().ends_with(".partial"));
  EXPECT_EQ(blackboxrs::validate_bundle(bundles[0]).status, blackboxrs::ValidationStatus::verified);
  fs::remove_all(dir);
}

TEST(RecorderExecutable, InvalidConfigurationStartsNothing) {
  const fs::path dir = temp_dir();
  std::ofstream(dir / "bad.yaml") << "schema: blackboxrs.runtime.v1\nprofile: missing.yaml\n";
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/recorder",
                          {"--config", (dir / "bad.yaml").string()}, dir / "log");
  EXPECT_EQ(wait_exit(pid, 20s), 5);
  fs::remove_all(dir);
}

TEST(MonitorExecutable, ReportsAStaleCommandForwardedToTheOutput) {
  const fs::path dir = temp_dir();
  const std::string cfg =
      write_config(dir, "monitor: {findings_file: " + (dir / "findings.jsonl").string() +
                            ", command_sources: {/bbrs_test/nav: 0.5}}\n");
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/monitor", {"--config", cfg},
                          dir / "monitor.log");
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_monitor");
  wait_for_subscribers(*probe, "/bbrs_test/cmd_out", 1);
  Traffic traffic(/*keep_output_after_nav_stops=*/true);
  traffic.run(2000ms, 800ms);  // navigation stops at 0.8 s; the output keeps 0.2 m/s
  ::kill(pid, SIGINT);
  EXPECT_EQ(wait_exit(pid, 20s), 0);
  std::ifstream in(dir / "findings.jsonl");
  std::string line;
  std::set<std::string> kinds;
  while (std::getline(in, line)) {
    kinds.insert(blackboxrs::Json::parse(line)["kind"].get<std::string>());
  }
  EXPECT_TRUE(kinds.count("stale_command_forwarded")) << std::ifstream(dir / "monitor.log").rdbuf();
  EXPECT_TRUE(kinds.count("command_source_stale"));
  fs::remove_all(dir);
}

TEST(RecorderExecutable, ContinuousCaptureBelowTheDiskFloorFailsAndExitsOne) {
  const fs::path dir = temp_dir();
  const auto free_mb = fs::space(dir).available / (1024U * 1024U);
  if (free_mb >= 1'000'000U) {
    GTEST_SKIP() << "more than 1 TB free: the floor cannot be set above it";
  }
  const fs::path cfg = dir / "runtime.yaml";
  std::ofstream(cfg) << "schema: blackboxrs.runtime.v1\n"
                     << "profile: " << BLACKBOXRS_ROS_TEST_DIR << "/test_profile.yaml\n"
                     << "capture: {mode: continuous, evidence_dir: " << (dir / "evidence").string()
                     << ", hard_disk_floor_mb: 1000000}\n"
                     << "ros: {namespace: /blackbox_test}\n"
                     << "diagnostics: {publish: false}\n";
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/recorder",
                          {"--config", cfg.string()}, dir / "recorder.log");
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_floor");
  wait_for_subscribers(*probe, "/bbrs_test/odom", 1);
  Traffic traffic;
  traffic.run(500ms);
  // The pipeline fails on the first record; the node notices at its next
  // graph poll and shuts itself down with exit code 1.
  const int rc = wait_exit(pid, 30s);
  std::stringstream log;
  log << std::ifstream(dir / "recorder.log").rdbuf();
  EXPECT_EQ(rc, 1) << log.str();
  EXPECT_NE(log.str().find("disk_pressure"), std::string::npos) << log.str();
  EXPECT_TRUE(bundles_in(dir / "evidence").empty());
  fs::remove_all(dir);
}

TEST(MonitorExecutable, MissingInputsAreReportedIncompleteNotOk) {
  const fs::path dir = temp_dir();
  const std::string cfg = write_config(
      dir, "monitor: {command_sources: {/bbrs_test/nav: 0.5}}\ndiagnostics: {period_s: 0.2}\n");
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/monitor", {"--config", cfg},
                          dir / "monitor.log");
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_monitor_diag");
  std::map<std::string, std::pair<std::string, std::string>> seen;  // name -> (message, reason)
  auto sub = probe->create_subscription<diagnostic_msgs::msg::DiagnosticArray>(
      "/diagnostics", 10, [&seen](const diagnostic_msgs::msg::DiagnosticArray& a) {
        for (const auto& st : a.status) {
          std::string reason;
          for (const auto& kv : st.values) {
            if (kv.key == "incomplete_reason") reason = kv.value;
          }
          seen[st.name] = {st.message, reason};
        }
      });
  const auto t0 = std::chrono::steady_clock::now();
  while (seen.count("blackboxrs: invariant stop_dominance") == 0U &&
         std::chrono::steady_clock::now() - t0 < 20s) {
    rclcpp::spin_some(probe);
    std::this_thread::sleep_for(20ms);
  }
  ::kill(pid, SIGINT);
  EXPECT_EQ(wait_exit(pid, 20s), 0);
  // No traffic at all: no output observed, no command source seen.
  for (const char* inv : {"finite_output", "fresh_output", "stop_dominance"}) {
    const auto it = seen.find(std::string("blackboxrs: invariant ") + inv);
    ASSERT_NE(it, seen.end()) << inv << "\n" << std::ifstream(dir / "monitor.log").rdbuf();
    EXPECT_EQ(it->second.first, "INCOMPLETE") << inv;
    EXPECT_NE(it->second.second.find("no arbitration output message received yet"),
              std::string::npos)
        << inv << ": " << it->second.second;
  }
  fs::remove_all(dir);
}

TEST(PreflightExecutable, GivesAVerdictAndCommandsNothing) {
  const fs::path dir = temp_dir();
  const std::string cfg = write_config(dir, "preflight: {listen_s: 1.5}\n");
  const pid_t pid = spawn(std::string(BLACKBOXRS_ROS_BIN_DIR) + "/preflight",
                          {"--config", cfg, "--json"}, dir / "preflight.json");
  auto probe = std::make_shared<rclcpp::Node>("bbrs_probe_preflight");
  std::jthread traffic_thread([] {
    Traffic traffic;
    traffic.run(2500ms);
  });
  const int rc = wait_exit(pid, 60s);
  EXPECT_TRUE(rc == 0 || rc == 1) << rc;
  std::ifstream in(dir / "preflight.json");
  std::stringstream ss;
  ss << in.rdbuf();
  const std::string text = ss.str();
  const auto json_start = text.find('{');
  ASSERT_NE(json_start, std::string::npos) << text;
  const auto out = blackboxrs::Json::parse(text.substr(json_start));
  EXPECT_TRUE(out["verdict"] == "GO" || out["verdict"] == "NO-GO");
  bool p7 = false;
  for (const auto& c : out["checks"]) {
    if (c["id"] == "P7_no_control_publishers") {
      EXPECT_EQ(c["status"], "PASS") << c.dump();
      p7 = true;
    }
    if (c["id"] == "P9_selftest_evidence") {
      EXPECT_EQ(c["status"], "PASS") << c.dump();
    }
  }
  EXPECT_TRUE(p7);
  fs::remove_all(dir);
}

}  // namespace
}  // namespace blackboxrs_ros

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  ::testing::AddGlobalTestEnvironment(new blackboxrs_ros::Env);
  return RUN_ALL_TESTS();
}
