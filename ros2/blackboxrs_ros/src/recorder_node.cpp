#include "blackboxrs_ros/recorder_node.hpp"

#include <filesystem>

#include "blackboxrs_ros/guarded_publisher.hpp"
#include "blackboxrs_ros/introspection_decoder.hpp"

namespace blackboxrs_ros {
namespace fs = std::filesystem;
using blackboxrs::Json;
namespace rec = blackboxrs::recorder;

std::atomic<bool>& RecorderNode::marker_requested() {
  static std::atomic<bool> flag{false};
  return flag;
}

RecorderNode::RecorderNode(blackboxrs::RuntimeConfig cfg, const rclcpp::NodeOptions& options,
                           std::string session_id)
    : rclcpp::Node("blackboxrs_recorder", cfg.node_namespace, options), cfg_(std::move(cfg)) {
  if (session_id.empty()) {
    session_id = cfg_.session_id.value_or(make_session_id());
  }
  session_dir_ = (fs::path(cfg_.evidence_dir) / session_id).string();
  const bool sim = get_parameter("use_sim_time").as_bool();
  RCLCPP_INFO(get_logger(),
              "BlackBoxRS RECORD mode (passive: subscribe only, no motion or control topics). "
              "profile %s (%zu topics), capture %s, evidence %s, config sha256 %.12s",
              cfg_.profile.name.c_str(), cfg_.profile.topics.size(),
              cfg_.capture_mode == blackboxrs::CaptureMode::continuous ? "continuous" : "triggered",
              session_dir_.c_str(), cfg_.sha256.c_str());

  auto decoder = std::make_unique<IntrospectionDecoder>(cfg_.profile);
  graph_ = std::make_unique<GraphProbe>(cfg_.profile, get_namespace());
  std::vector<bool> decodable(cfg_.profile.topics.size(), false);
  for (std::size_t i = 0; i < cfg_.profile.topics.size(); ++i) {
    if (const auto why = decoder->unavailable(i)) {
      graph_->mark_type_unavailable(cfg_.profile.topics[i].name, *why);
      RCLCPP_WARN(get_logger(), "%s: type %s not installed, not recorded (%s)",
                  cfg_.profile.topics[i].name.c_str(), cfg_.profile.topics[i].type.c_str(),
                  why->c_str());
    } else {
      decodable[i] = true;
    }
  }

  rec::RecorderConfig rc;
  rc.ingest_capacity = cfg_.ingest_capacity;
  rc.control_reserve = cfg_.control_reserve;
  rc.drain_deadline = cfg_.drain_deadline;
  rc.hard_disk_floor_mb = cfg_.hard_disk_floor_mb;
  rc.continuous = cfg_.capture_mode == blackboxrs::CaptureMode::continuous;
  rc.writer.session_dir = session_dir_;
  rc.writer.fsync_every = cfg_.fsync_every;
  rc.writer.chunk_records = cfg_.chunk_records;
  rc.writer.chunk_bytes = cfg_.chunk_bytes;
  rc.writer.queue_capacity = cfg_.writer_capacity;
  rc.manifest = manifest_context(cfg_, session_id, "record", sim);
  recorder_ = std::make_unique<rec::Recorder>(cfg_.profile, rc, std::move(decoder));

  // Subscriptions: best effort (never back-pressures a reliable writer) and
  // volatile (no replay of history the recorder did not see live).
  const rclcpp::QoS qos =
      rclcpp::QoS(rclcpp::KeepLast(cfg_.subscription_depth)).best_effort().durability_volatile();
  for (std::size_t i = 0; i < cfg_.profile.topics.size(); ++i) {
    message_lost_.push_back(std::make_unique<std::atomic<std::uint64_t>>(0));
    if (!decodable[i]) {
      continue;
    }
    const auto& spec = cfg_.profile.topics[i];
    const auto index = static_cast<std::uint32_t>(i);
    auto* recorder = recorder_.get();
    auto cb = [recorder, index](std::shared_ptr<rclcpp::SerializedMessage> msg,
                                const rmw_message_info_t& info) {
      // Hot path: stamp clocks, move the handle. No decoding, no I/O, no lock
      // other than the queue's, never blocks.
      rec::MessageArrival m;
      m.topic = index;
      m.t_mono = blackboxrs::clock_domain::Mono::now();
      m.t_wall = blackboxrs::clock_domain::Wall::now();
      if (info.source_timestamp != 0) {
        m.src = blackboxrs::source_ns(info.source_timestamp);
      }
      if (info.received_timestamp != 0) {
        m.rx = blackboxrs::wall_ns(info.received_timestamp);
      }
      m.payload = std::make_shared<SerializedPayload>(std::move(msg));
      (void)recorder->on_message(std::move(m));
    };
    rclcpp::SubscriptionOptions opts;
    auto* lost = message_lost_.back().get();
    opts.event_callbacks.message_lost_callback = [lost](rclcpp::QOSMessageLostInfo& info) {
      lost->store(info.total_count);
    };
    try {
      subscriptions_.push_back(
          create_observed_subscription(*this, spec.name, spec.type, qos, cb, opts));
    } catch (const rclcpp::UnsupportedEventTypeException&) {
      // The RMW has no message-lost event: subscribe without it.
      subscriptions_.push_back(create_observed_subscription(*this, spec.name, spec.type, qos, cb));
    }
  }

  if (cfg_.diagnostics_publish) {
    diag_pub_ = guarded_publisher<diagnostic_msgs::msg::DiagnosticArray>(
        *this, cfg_, "/diagnostics", rclcpp::QoS(10));
    status_pub_ = guarded_publisher<std_msgs::msg::String>(*this, cfg_, "/blackboxrs/status",
                                                           rclcpp::QoS(1).transient_local());
    diag_timer_ = create_wall_timer(std::chrono::duration<double>(cfg_.diagnostics_period_s),
                                    [this] { publish_diagnostics(); });
  }
  graph_timer_ =
      create_wall_timer(std::chrono::duration<double>(cfg_.profile.sampling.graph_poll_sec),
                        [this] { poll_graph(); });
  poll_graph();
  RCLCPP_INFO(get_logger(), "recording %zu of %zu profile topics", subscriptions_.size(),
              cfg_.profile.topics.size());
}

RecorderNode::~RecorderNode() {
  stop("node_destroyed");
}

void RecorderNode::stop(const std::string& reason) {
  if (stopped_) {
    return;
  }
  stopped_ = true;
  graph_timer_.reset();
  diag_timer_.reset();
  subscriptions_.clear();  // no more callbacks
  recorder_->stop(reason);
  const auto m = recorder_->metrics();
  RCLCPP_INFO(get_logger(),
              "stopped (%s): %lu received, %lu processed, %lu dropped at ingest, %lu dropped at "
              "shutdown, %lu records written, %lu bundle(s) finalized, %lu failed",
              reason.c_str(), static_cast<unsigned long>(m.received),
              static_cast<unsigned long>(m.processed), static_cast<unsigned long>(m.dropped_ingest),
              static_cast<unsigned long>(m.dropped_at_shutdown),
              static_cast<unsigned long>(m.writer.records_written),
              static_cast<unsigned long>(m.writer.bundles_finalized),
              static_cast<unsigned long>(m.writer.bundles_failed));
}

void RecorderNode::poll_graph() {
  // A graph query failing (for example while the context shuts down) must
  // not end the recording; it is logged, and the next poll tries again.
  try {
    poll_graph_once();
  } catch (const std::exception& exc) {
    RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 10000, "graph poll failed: %s", exc.what());
  }
}

void RecorderNode::poll_graph_once() {
  // A failed pipeline records nothing more: end the process (exit code 1)
  // rather than keep a recorder alive that only looks healthy.
  if (recorder_->metrics().state == "failed") {
    if (!failure_reported_) {
      failure_reported_ = true;
      RCLCPP_FATAL(get_logger(), "recorder failed: %s; shutting down",
                   recorder_->metrics().fatal_error.c_str());
      rclcpp::shutdown();
    }
    return;
  }
  auto snap = graph_->probe(*this);
  for (std::size_t i = 0; i < cfg_.profile.topics.size(); ++i) {
    snap.topic_status[cfg_.profile.topics[i].name]["message_lost"] = message_lost_[i]->load();
  }
  recorder_->on_graph(std::move(snap));
  if (marker_requested().exchange(false)) {
    recorder_->mark("SIGUSR1 marker", "operator");
  }
}

void RecorderNode::publish_diagnostics() {
  const auto m = recorder_->metrics();
  diagnostic_msgs::msg::DiagnosticArray arr;
  arr.header.stamp = now();
  diagnostic_msgs::msg::DiagnosticStatus st;
  st.name = "blackboxrs: recorder";
  st.hardware_id = hostname();
  if (m.state == "failed" || m.writer.write_errors != 0 || m.writer.bundles_failed != 0) {
    st.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    st.message = m.fatal_error.empty() ? "evidence write errors" : m.fatal_error;
  } else if (m.core.value("incidents_skipped", 0) != 0) {
    // Triggered mode: an incident that should have been captured was not.
    st.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
    st.message =
        "incident(s) not captured: " +
        m.core["skip_reasons"].dump(-1, ' ', false, blackboxrs::Json::error_handler_t::replace);
  } else if (m.dropped_ingest != 0 || m.dropped_dds != 0 || m.control_rejected != 0) {
    st.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
    st.message = m.dropped_ingest != 0 ? "messages dropped at the ingest queue"
                 : m.dropped_dds != 0  ? "messages lost in DDS (message_lost)"
                                       : "graph snapshots or markers refused (queue full)";
  } else {
    st.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
    st.message = "recording";
  }
  auto kv = [&](const std::string& k, const std::string& v) {
    diagnostic_msgs::msg::KeyValue x;
    x.key = k;
    x.value = v;
    st.values.push_back(x);
  };
  kv("state", m.state);
  kv("received", std::to_string(m.received));
  kv("processed", std::to_string(m.processed));
  kv("dropped_ingest", std::to_string(m.dropped_ingest));
  kv("dropped_dds", std::to_string(m.dropped_dds));
  kv("incidents_skipped", std::to_string(m.core.value("incidents_skipped", 0)));
  kv("queue_depth", std::to_string(m.ingest.depth));
  kv("queue_high_water", std::to_string(m.ingest.high_water));
  kv("records_written", std::to_string(m.writer.records_written));
  kv("write_errors", std::to_string(m.writer.write_errors));
  kv("writer_max_lag_ms", std::to_string(static_cast<double>(m.writer.max_lag_ns) / 1e6));
  kv("incident_open", m.core.value("incident_open", false) ? "true" : "false");
  arr.status.push_back(st);
  diag_pub_->publish(arr);
  std_msgs::msg::String s;
  s.data = m.to_json().dump(-1, ' ', false, blackboxrs::Json::error_handler_t::replace);
  status_pub_->publish(s);
}

}  // namespace blackboxrs_ros
