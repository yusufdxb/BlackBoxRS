// blackboxrs_monitor: the replay invariants and detectors, run online.
//
// ros2 run blackboxrs_ros monitor --config configs/go2_hardware.yaml
//
// Passive: it subscribes to the command sources, the HELIX hold, the recorded
// arbitration output (ArbiterStatus, or /cmd_vel when the profile records it)
// and odometry, and evaluates the same monitor classes Replay Lab uses
// offline (stop_dominance, fresh_output, finite_output, consistent_state,
// command_source_stale, clock, odometry) on the receipt clock. It judges
// what the robot was commanded; it never commands anything. Findings go to
// the log (once per episode), an optional JSONL file, /blackboxrs/findings and
// /diagnostics.

#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <fstream>
#include <iostream>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>

#include "blackboxrs/replay/monitors.hpp"
#include "blackboxrs/replay/sut.hpp"
#include "blackboxrs_ros/guarded_publisher.hpp"
#include "blackboxrs_ros/introspection_decoder.hpp"
#include "blackboxrs_ros/observed_subscription.hpp"
#include "blackboxrs_ros/runtime.hpp"

namespace blackboxrs_ros {
using blackboxrs::Json;
namespace rp = blackboxrs::replay;

class MonitorNode final : public rclcpp::Node {
 public:
  MonitorNode(blackboxrs::RuntimeConfig cfg, const rclcpp::NodeOptions& options)
      : rclcpp::Node("blackboxrs_monitor", cfg.node_namespace, options),
        cfg_(std::move(cfg)),
        start_(blackboxrs::clock_domain::Mono::now()),
        decoder_(cfg_.profile) {
    std::set<std::string> hold;
    std::set<std::string> status;
    std::set<std::string> odom;
    std::map<std::string, std::vector<std::string>> by_role;
    std::map<std::string, std::string> hosts;
    std::vector<std::size_t> watched;
    for (std::size_t i = 0; i < cfg_.profile.topics.size(); ++i) {
      const auto& t = cfg_.profile.topics[i];
      hosts[t.name] = blackboxrs::is_robot_role(t.role) ? "robot" : "payload";
      const bool source = cfg_.command_sources.count(t.name) != 0U;
      const bool relevant = source || t.role == blackboxrs::Role::helix_hold ||
                            t.role == blackboxrs::Role::arbiter_status ||
                            t.role == blackboxrs::Role::cmd_vel_out ||
                            t.role == blackboxrs::Role::odometry;
      if (!relevant) {
        continue;
      }
      if (decoder_.unavailable(i)) {
        RCLCPP_WARN(get_logger(), "%s: type %s not installed, not monitored", t.name.c_str(),
                    t.type.c_str());
        continue;
      }
      by_role[std::string(blackboxrs::role_name(t.role))].push_back(t.name);
      if (t.role == blackboxrs::Role::helix_hold) hold.insert(t.name);
      if (t.role == blackboxrs::Role::arbiter_status) status.insert(t.name);
      if (t.role == blackboxrs::Role::odometry) odom.insert(t.name);
      watched.push_back(i);
    }
    std::set<std::string> sources;
    for (const auto& [t, w] : cfg_.command_sources) {
      sources.insert(t);
    }
    sut_ = std::make_unique<rp::ObservedOutput>(by_role);
    stop_ = std::make_unique<rp::StopDominance>(hold, sources, cfg_.stop_grace_s, 0.5);
    cmd_ = std::make_unique<rp::CommandPath>(cfg_.command_sources, cfg_.fresh_grace_s);
    // consistent_state judges ArbiterStatus when that is the observed output.
    consistent_ = std::make_unique<rp::ConsistentState>(
        by_role.count("cmd_vel_out") != 0U ? std::set<std::string>{} : status);
    clock_ = std::make_unique<rp::ClockMonitor>(hosts, cfg_.clock_step_threshold_s,
                                                cfg_.clock_offset_info_s);
    odom_ = std::make_unique<rp::OdometryConsistency>(odom);
    monitors_ = {stop_.get(), cmd_.get(), consistent_.get(), clock_.get(), odom_.get()};
    if (cfg_.findings_file) {
      findings_out_.open(*cfg_.findings_file, std::ios::app);
      if (!findings_out_) {
        throw std::runtime_error("cannot open monitor.findings_file " + *cfg_.findings_file);
      }
    }
    RCLCPP_INFO(get_logger(),
                "BlackBoxRS MONITOR mode (passive: judges recorded outputs, commands nothing). "
                "output: %s; %zu topics watched",
                sut_->source_label().value_or("NOT AVAILABLE (outputs are INCOMPLETE)").c_str(),
                watched.size());
    const rclcpp::QoS qos =
        rclcpp::QoS(rclcpp::KeepLast(cfg_.subscription_depth)).best_effort().durability_volatile();
    for (std::size_t i : watched) {
      const auto& t = cfg_.profile.topics[i];
      subs_.push_back(create_observed_subscription(
          *this, t.name, t.type, qos,
          [this, i](std::shared_ptr<rclcpp::SerializedMessage> msg,
                    const rmw_message_info_t& info) { on_message(i, std::move(msg), info); }));
    }
    if (cfg_.diagnostics_publish) {
      findings_pub_ = guarded_publisher<std_msgs::msg::String>(*this, cfg_, "/blackboxrs/findings",
                                                               rclcpp::QoS(50));
      diag_pub_ = guarded_publisher<diagnostic_msgs::msg::DiagnosticArray>(
          *this, cfg_, "/diagnostics", rclcpp::QoS(10));
      diag_timer_ = create_wall_timer(std::chrono::duration<double>(cfg_.diagnostics_period_s),
                                      [this] { publish_diagnostics(); });
    }
    tick_timer_ =
        create_wall_timer(std::chrono::duration<double>(cfg_.monitor_period_s), [this] { tick(); });
  }

 private:
  blackboxrs::ReplayTime now_rt() const {
    return blackboxrs::to_replay(blackboxrs::clock_domain::Mono::now(), start_);
  }

  // A monitor defect must not take the node down silently (an exception out
  // of a callback ends spin()): it is counted, logged once, and reported as
  // an ERROR diagnostic, and the monitor keeps judging later messages.
  template <class F>
  void guarded(const char* where, F&& f) {
    try {
      f();
    } catch (const std::exception& exc) {
      if (monitor_errors_++ == 0) {
        RCLCPP_ERROR(get_logger(), "monitor error in %s: %s (further errors are only counted)",
                     where, exc.what());
      }
      batch_.clear();
    }
  }

  void on_message(std::size_t i, std::shared_ptr<rclcpp::SerializedMessage> msg,
                  const rmw_message_info_t& info) {
    guarded("message", [&] { handle_message(i, std::move(msg), info); });
  }

  void handle_message(std::size_t i, std::shared_ptr<rclcpp::SerializedMessage> msg,
                      const rmw_message_info_t& info) {
    const auto& spec = cfg_.profile.topics[i];
    const auto r = decoder_.decode(i, SerializedPayload(std::move(msg)));
    blackboxrs::Event e;
    e.t = now_rt();
    e.order = blackboxrs::OrderKey{0, ++seq_, 0};
    e.eid = "m" + std::to_string(seq_);
    blackboxrs::MessageBody m;
    m.topic = spec.name;
    m.role = spec.role;
    m.role_name = std::string(blackboxrs::role_name(spec.role));
    m.type = spec.type;
    if (info.source_timestamp != 0) m.src = blackboxrs::source_ns(info.source_timestamp);
    m.rx_wall = blackboxrs::wall_ns(
        info.received_timestamp != 0 ? info.received_timestamp
                                     : blackboxrs::count_ns(blackboxrs::clock_domain::Wall::now()));
    if (r.data) {
      m.set_data(*r.data);
    } else {
      ++decode_errors_;
      return;
    }
    if (cfg_.command_sources.count(spec.name) != 0U) {
      source_seen_ = true;
    }
    e.body = std::move(m);
    sut_->on_event(e, e.t);
    for (rp::Monitor* mon : monitors_) {
      mon->on_event(e, batch_);
    }
    if (auto pub = sut_->take_publication(e.t)) {
      output_seen_ = output_seen_ || pub->robot_raw.has_value();
      for (rp::Monitor* mon : monitors_) {
        mon->on_decision(*pub, batch_);
      }
    }
    flush();
  }

  void tick() {
    guarded("tick", [&] { handle_tick(); });
  }

  void handle_tick() {
    const rp::Decision d = sut_->tick(now_rt());
    for (rp::Monitor* mon : monitors_) {
      mon->on_decision(d, batch_);
    }
    flush();
  }

  void flush() {
    for (const auto& f : batch_) {
      ++findings_;
      Json j = {{"t_monitor_s", blackboxrs::ns_to_seconds(blackboxrs::count_ns(f.t))},
                {"wall_ns", blackboxrs::count_ns(blackboxrs::clock_domain::Wall::now())},
                {"monitor", f.monitor},
                {"kind", f.kind},
                {"severity", rp::severity_name(f.severity)},
                {"subject", f.subject},
                {"message", f.message},
                {"invariant", f.invariant ? Json(*f.invariant) : Json()},
                {"data", blackboxrs::jsonable(f.data)}};
      if (f.severity == rp::Severity::critical) {
        RCLCPP_ERROR(get_logger(), "[%s] %s %s: %s", rp::severity_name(f.severity), f.kind.c_str(),
                     f.subject.c_str(), f.message.c_str());
      } else if (f.severity == rp::Severity::warning) {
        RCLCPP_WARN(get_logger(), "[%s] %s %s: %s", rp::severity_name(f.severity), f.kind.c_str(),
                    f.subject.c_str(), f.message.c_str());
      }
      if (findings_out_) {
        findings_out_ << blackboxrs::canonical_json(j) << "\n" << std::flush;
      }
      if (findings_pub_) {
        std_msgs::msg::String s;
        s.data = blackboxrs::canonical_json(j);
        findings_pub_->publish(s);
      }
    }
    batch_.clear();
  }

  // Why an invariant cannot be judged (yet), as the offline replay gates it:
  // missing inputs are INCOMPLETE, never a quiet PASS.
  [[nodiscard]] std::optional<std::string> incomplete_reason(const std::string& name) const {
    const bool output_invariant =
        name == "finite_output" || name == "fresh_output" || name == "stop_dominance";
    if (output_invariant && !sut_->available()) {
      return "no arbitration output topic (/cmd_vel or ArbiterStatus) is monitored: outputs are "
             "not judged";
    }
    if (output_invariant && !output_seen_) {
      return "no arbitration output message received yet: outputs are not judged";
    }
    if (name == "fresh_output" && !source_seen_) {
      return "no message on any command source yet: the arbitration path is not exercised";
    }
    if (name == "stop_dominance" && stop_->hold_ever_asserted() &&
        stop_->invariants().front()->checks == 0) {
      return "a hold was asserted but no robot-facing command has been judged against it";
    }
    return std::nullopt;
  }

  void publish_diagnostics() {
    diagnostic_msgs::msg::DiagnosticArray arr;
    arr.header.stamp = now();
    for (rp::Monitor* mon : monitors_) {
      for (const rp::InvariantState* inv : mon->invariants()) {
        diagnostic_msgs::msg::DiagnosticStatus st;
        st.name = "blackboxrs: invariant " + inv->name;
        auto status = inv->status();
        const auto why =
            status == rp::InvariantStatus::fail ? std::nullopt : incomplete_reason(inv->name);
        if (why) {
          status = rp::InvariantStatus::incomplete;
        }
        st.level =
            status == rp::InvariantStatus::fail   ? diagnostic_msgs::msg::DiagnosticStatus::ERROR
            : status == rp::InvariantStatus::pass ? diagnostic_msgs::msg::DiagnosticStatus::OK
                                                  : diagnostic_msgs::msg::DiagnosticStatus::WARN;
        // Not exercised or incomplete is never reported as OK.
        st.message = rp::status_name(status);
        diagnostic_msgs::msg::KeyValue kv;
        kv.key = "checks";
        kv.value = std::to_string(inv->checks);
        st.values.push_back(kv);
        kv.key = "violations";
        kv.value = std::to_string(inv->violations);
        st.values.push_back(kv);
        if (why) {
          kv.key = "incomplete_reason";
          kv.value = *why;
          st.values.push_back(kv);
        }
        arr.status.push_back(st);
      }
    }
    diagnostic_msgs::msg::DiagnosticStatus st;
    st.name = "blackboxrs: monitor";
    st.level = monitor_errors_ != 0  ? diagnostic_msgs::msg::DiagnosticStatus::ERROR
               : decode_errors_ != 0 ? diagnostic_msgs::msg::DiagnosticStatus::WARN
                                     : diagnostic_msgs::msg::DiagnosticStatus::OK;
    st.message = std::to_string(findings_) + " finding(s), " + std::to_string(decode_errors_) +
                 " decode error(s), " + std::to_string(monitor_errors_) + " monitor error(s)";
    arr.status.push_back(st);
    diag_pub_->publish(arr);
  }

  blackboxrs::RuntimeConfig cfg_;
  blackboxrs::MonoTime start_;
  IntrospectionDecoder decoder_;
  std::unique_ptr<rp::ObservedOutput> sut_;
  std::unique_ptr<rp::StopDominance> stop_;
  std::unique_ptr<rp::CommandPath> cmd_;
  std::unique_ptr<rp::ConsistentState> consistent_;
  std::unique_ptr<rp::ClockMonitor> clock_;
  std::unique_ptr<rp::OdometryConsistency> odom_;
  std::vector<rp::Monitor*> monitors_;
  rp::Findings batch_;
  std::ofstream findings_out_;
  std::vector<std::shared_ptr<ObservedSubscription>> subs_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr findings_pub_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diag_pub_;
  rclcpp::TimerBase::SharedPtr tick_timer_;
  rclcpp::TimerBase::SharedPtr diag_timer_;
  std::int64_t seq_ = 0;
  std::uint64_t findings_ = 0;
  std::uint64_t decode_errors_ = 0;
  std::uint64_t monitor_errors_ = 0;
  bool source_seen_ = false;
  bool output_seen_ = false;
};

}  // namespace blackboxrs_ros

int main(int argc, char** argv) {
  const auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  const std::string path = blackboxrs_ros::config_path_from_args(args);
  if (path.empty()) {
    std::cerr << "usage: monitor --config RUNTIME.yaml [--ros-args ...]\n";
    rclcpp::shutdown();
    return 2;
  }
  blackboxrs::RuntimeConfig cfg;
  try {
    cfg = blackboxrs::load_runtime_config(path);
  } catch (const blackboxrs::ConfigError& exc) {
    std::cerr << "configuration rejected, nothing started: " << exc.what() << "\n";
    rclcpp::shutdown();
    return 5;
  }
  if (!cfg.monitor_enabled) {
    std::cerr << "monitor.enabled is false in " << path << "\n";
    rclcpp::shutdown();
    return 0;
  }
  {
    auto node = std::make_shared<blackboxrs_ros::MonitorNode>(
        cfg, blackboxrs_ros::passive_node_options({}));
    rclcpp::spin(node);
  }
  rclcpp::shutdown();
  return 0;
}
