#include "blackboxrs/recorder/recorder.hpp"

#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <filesystem>

namespace blackboxrs::recorder {
namespace fs = std::filesystem;
using SteadyClock = std::chrono::steady_clock;

namespace {

constexpr const char* kStates[] = {"recording", "draining", "stopped", "failed"};

std::chrono::nanoseconds period_of(double hz) {
  return std::chrono::nanoseconds(static_cast<std::int64_t>(1e9 / hz));
}

}  // namespace

Json RecorderMetrics::to_json() const {
  Json topics_json = Json::object();
  for (const auto& t : topics) {
    topics_json[t.name] = {{"received", t.received},
                           {"dropped_ingest", t.dropped_ingest},
                           {"decode_errors", t.decode_errors}};
  }
  return {{"state", state},
          {"uptime_s", uptime_s},
          {"ingest_queue",
           {{"capacity", ingest.capacity},
            {"control_reserve", ingest.control_reserve},
            {"depth", ingest.depth},
            {"high_water", ingest.high_water},
            {"pushed", ingest.pushed},
            {"popped", ingest.popped},
            {"rejected_full_data", ingest.rejected_full_data},
            {"rejected_full_control", ingest.rejected_full_control},
            {"rejected_closed", ingest.rejected_closed}}},
          {"writer",
           {{"records_written", writer.records_written},
            {"records_unwritten", writer.records_unwritten},
            {"bytes_written", writer.bytes_written},
            {"write_errors", writer.write_errors},
            {"bundles_opened", writer.bundles_opened},
            {"bundles_finalized", writer.bundles_finalized},
            {"bundles_failed", writer.bundles_failed},
            {"last_lag_ms", static_cast<double>(writer.last_lag_ns) / 1e6},
            {"max_lag_ms", static_cast<double>(writer.max_lag_ns) / 1e6},
            {"queue_depth", writer.queue.depth},
            {"queue_high_water", writer.queue.high_water},
            {"throughput_bytes_per_s",
             uptime_s > 0 ? static_cast<double>(writer.bytes_written) / uptime_s : 0.0}}},
          {"received", received},
          {"dropped_ingest", dropped_ingest},
          {"dropped_at_shutdown", dropped_at_shutdown},
          {"rejected_after_stop", rejected_after_stop},
          {"decoded", decoded},
          {"decode_errors", decode_errors},
          {"decimated", decimated},
          {"processed", processed},
          {"topics", topics_json},
          {"core", core},
          {"system", system},
          {"fatal_error", fatal_error.empty() ? Json() : Json(fatal_error)}};
}

Recorder::Recorder(Profile profile, RecorderConfig config, std::unique_ptr<MessageDecoder> decoder)
    : profile_(std::move(profile)),
      config_(std::move(config)),
      started_(SteadyClock::now()),
      received_(profile_.topics.size()),
      dropped_(profile_.topics.size()),
      ingest_(config_.ingest_capacity, config_.control_reserve),
      writer_(config_.writer, config_.manifest),
      decoder_(std::move(decoder)),
      store_period_ns_(profile_.topics.size(), 0),
      last_stored_(profile_.topics.size()),
      decode_errors_(profile_.topics.size(), 0),
      pipeline_() {
  if (!decoder_) {
    throw std::invalid_argument("recorder needs a message decoder");
  }
  for (std::size_t i = 0; i < profile_.topics.size(); ++i) {
    const auto& t = profile_.topics[i];
    if (t.store_max_hz) {
      store_period_ns_[i] = static_cast<std::int64_t>(1e9 / *t.store_max_hz);
    }
    topic_status_[t.name] = {{"status", "absent"},
                             {"reason", "not seen on the graph yet"},
                             {"message_lost", 0},
                             {"received", 0}};
  }
  CoreOptions opts;
  opts.continuous = config_.continuous;
  const fs::path session_dir = config_.writer.session_dir;
  const std::int64_t floor_mb = config_.hard_disk_floor_mb;
  core_ = std::make_unique<FlightCore>(
      profile_,
      [this] {
        return writer_.make_sink([this] { return topic_status_json(); },
                                 [this] { return total_dropped(); });
      },
      [session_dir, floor_mb]() -> std::pair<bool, std::string> {
        std::error_code ec;
        fs::path p = session_dir;
        while (!fs::exists(p, ec) && p.has_parent_path() && p != p.parent_path()) {
          p = p.parent_path();
        }
        const auto space = fs::space(p, ec);
        if (ec) {
          return {false, "disk_unknown: " + ec.message()};
        }
        const auto free_mb = static_cast<std::int64_t>(space.available / (1024ULL * 1024ULL));
        if (free_mb < floor_mb) {
          return {false, "disk_pressure: " + std::to_string(free_mb) + " MB free < floor " +
                             std::to_string(floor_mb) + " MB"};
        }
        return {true, ""};
      },
      opts);
  const auto now = SteadyClock::now();
  next_tick_ = now;
  next_sample_ = now;
  next_session_file_ = now;
  pipeline_ = std::jthread([this](std::stop_token st) { run(std::move(st)); });
}

Recorder::~Recorder() {
  stop();
}

std::optional<std::uint32_t> Recorder::topic_index(std::string_view name) const noexcept {
  for (std::size_t i = 0; i < profile_.topics.size(); ++i) {
    if (profile_.topics[i].name == name) {
      return static_cast<std::uint32_t>(i);
    }
  }
  return std::nullopt;
}

PushResult Recorder::on_message(MessageArrival&& m) noexcept {
  if (m.topic >= received_.size()) {
    return PushResult::full;  // not a profile topic: a programming error, counted nowhere
  }
  received_[m.topic].fetch_add(1, std::memory_order_relaxed);
  const std::uint32_t topic = m.topic;
  IngestItem item(std::move(m));
  const PushResult r = ingest_.try_push(item, Lane::data);
  if (r == PushResult::full) {
    dropped_[topic].fetch_add(1, std::memory_order_relaxed);
  } else if (r == PushResult::closed) {
    rejected_after_stop_.fetch_add(1, std::memory_order_relaxed);
  }
  return r;
}

void Recorder::on_graph(GraphSnapshot g) noexcept {
  IngestItem item(std::move(g));
  (void)ingest_.try_push(item, Lane::control);
}

void Recorder::mark(std::string note, std::string source) noexcept {
  IngestItem item(MarkerRequest{clock_domain::Mono::now(), clock_domain::Wall::now(),
                                std::move(note), std::move(source)});
  (void)ingest_.try_push(item, Lane::control);
}

std::uint64_t Recorder::total_dropped() const noexcept {
  std::uint64_t n = 0;
  for (const auto& d : dropped_) {
    n += d.load(std::memory_order_relaxed);
  }
  return n + dropped_at_shutdown_.load(std::memory_order_relaxed);
}

Json Recorder::topic_status_json() const {
  Json out = Json::object();
  for (std::size_t i = 0; i < profile_.topics.size(); ++i) {
    const auto& name = profile_.topics[i].name;
    Json st = topic_status_.at(name);
    st["received"] = received_[i].load(std::memory_order_relaxed);
    st["dropped_ingest"] = dropped_[i].load(std::memory_order_relaxed);
    st["decode_errors"] = decode_errors_[i];
    out[name] = std::move(st);
  }
  return out;
}

void Recorder::run(std::stop_token stop) {
  pipeline_tid_.store(static_cast<int>(::gettid()));
  std::vector<IngestItem> batch;
  batch.reserve(config_.batch);
  try {
    while (!stop.stop_requested()) {
      batch.clear();
      const auto deadline = std::min({next_tick_, next_sample_, next_session_file_});
      ingest_.pop_batch(batch, config_.batch, deadline, stop);
      for (IngestItem& item : batch) {
        handle(item);
      }
      periodic(false);
    }
    // Drain: stop was requested and the queue is closed.
    state_.store(1);
    const auto drain_until = SteadyClock::now() + config_.drain_deadline;
    while (SteadyClock::now() < drain_until) {
      batch.clear();
      if (ingest_.pop_batch(batch, config_.batch, SteadyClock::now(), std::stop_token{}) == 0) {
        break;
      }
      for (IngestItem& item : batch) {
        handle(item);
      }
    }
    batch.clear();
    const std::size_t left = ingest_.drain(batch);
    std::uint64_t left_msgs = 0;
    for (const auto& item : batch) {
      left_msgs += std::holds_alternative<MessageArrival>(item) ? 1U : 0U;
    }
    dropped_at_shutdown_.store(left_msgs);
    (void)left;
    std::string reason;
    {
      std::lock_guard lock(snapshot_mu_);
      reason = stop_reason_;
    }
    core_->shutdown(reason);
    periodic(true);
    state_.store(2);
  } catch (const std::exception& exc) {
    // Never let an exception escape the thread: record it, stop taking data,
    // close what can still be closed.
    {
      std::lock_guard lock(snapshot_mu_);
      fatal_error_ = std::string("pipeline: ") + exc.what();
    }
    state_.store(3);
    ingest_.close();
    try {
      core_->shutdown("pipeline_failed");
    } catch (...) {
    }
  }
}

void Recorder::handle(IngestItem& item) {
  if (auto* m = std::get_if<MessageArrival>(&item)) {
    handle_message(*m);
  } else if (auto* g = std::get_if<GraphSnapshot>(&item)) {
    for (auto& [topic, st] : g->topic_status) {
      auto it = topic_status_.find(topic);
      if (it == topic_status_.end()) {
        continue;
      }
      for (const auto& [k, v] : st.items()) {
        it->second[k] = v;
      }
    }
    core_->graph(g->t_mono, g->t_wall, g->nodes, g->topics, g->publishers);
  } else if (auto* mk = std::get_if<MarkerRequest>(&item)) {
    core_->mark(mk->t_mono, mk->t_wall, mk->note, mk->source);
  }
}

void Recorder::handle_message(MessageArrival& m) {
  const auto& spec = profile_.topics[m.topic];
  bool store = true;
  if (store_period_ns_[m.topic] != 0) {
    auto& last = last_stored_[m.topic];
    const std::int64_t t = count_ns(m.t_mono);
    if (last && t - *last < store_period_ns_[m.topic]) {
      store = false;
    } else {
      last = t;
    }
  }
  Json data = Json::object();
  bool stored = false;
  if (store && m.payload) {
    DecodeResult r;
    try {
      r = decoder_->decode(m.topic, *m.payload);
    } catch (const std::exception& exc) {
      r.error = exc.what();
    }
    if (r.data) {
      data = std::move(*r.data);
      stored = true;
      decoded_.fetch_add(1, std::memory_order_relaxed);
      if (!r.missing.empty()) {
        auto& st = topic_status_[spec.name];
        if (!st.contains("missing_fields")) {
          st["missing_fields"] = r.missing;
        }
      }
    } else {
      ++decode_errors_[m.topic];
      decode_error_total_.fetch_add(1, std::memory_order_relaxed);
      auto& st = topic_status_[spec.name];
      if (!st.contains("first_decode_error")) {
        st["first_decode_error"] = r.error;
      }
    }
  } else if (!store) {
    decimated_.fetch_add(1, std::memory_order_relaxed);
  }
  // The payload handle is released here, before the record is built.
  m.payload.reset();
  core_->ingest(make_msg_record(spec.name, spec.role, spec.type, std::move(data), stored, m.t_mono,
                                m.t_wall, m.t_ros, m.src, m.rx));
  processed_.fetch_add(1, std::memory_order_relaxed);
}

void Recorder::periodic(bool force) {
  const auto now = SteadyClock::now();
  if (force || now >= next_sample_) {
    next_sample_ = now + period_of(profile_.sampling.system_sample_hz);
    if (config_.sample_system) {
      const auto t0 = SteadyClock::now();
      const SystemSample s = sampler_.sample();
      const double ms = std::chrono::duration<double, std::milli>(SteadyClock::now() - t0).count();
      OrderedJson f = OrderedJson::object();
      f["sample_ms"] = std::round(ms * 100.0) / 100.0;
      const Json sj = s.to_json();
      for (const auto& [k, v] : sj.items()) {
        f[k] = OrderedJson::parse(v.dump());
      }
      if (!force) {
        core_->ingest(make_event_record(RecordKind::sys, clock_domain::Mono::now(),
                                        clock_domain::Wall::now(), std::move(f)));
      }
      std::lock_guard lock(snapshot_mu_);
      system_snapshot_ = sj;
    }
  }
  if (force || now >= next_tick_) {
    next_tick_ = now + std::chrono::duration_cast<SteadyClock::duration>(
                           std::chrono::duration<double>(profile_.sampling.health_tick_sec));
    if (!force) {
      core_->tick(clock_domain::Mono::now(), clock_domain::Wall::now());
    }
    std::lock_guard lock(snapshot_mu_);
    core_snapshot_ = core_->stats_json();
    core_snapshot_["incident_open"] = core_->incident_open();
  }
  if (force || now >= next_session_file_) {
    next_session_file_ = now + config_.session_file_every;
    Json state = {{"state", force ? "stopped" : "recording"},
                  {"session", config_.manifest.session},
                  {"topic_status", topic_status_json()},
                  {"metrics", metrics().to_json()},
                  {"bundles", writer_.finalized_bundles()},
                  {"updated_wall_ns", count_ns(clock_domain::Wall::now())}};
    writer_.write_session_file(std::move(state));
  }
}

void Recorder::stop(const std::string& reason) {
  std::call_once(stop_once_, [&] {
    {
      std::lock_guard lock(snapshot_mu_);
      stop_reason_ = reason;
    }
    ingest_.close();  // 1. no new data
    pipeline_.request_stop();
    if (pipeline_.joinable()) {
      pipeline_.join();  // 2-3. drain, close incidents
    }
    writer_.finish();  // 4. finalize bundles
  });
}

RecorderMetrics Recorder::metrics() const {
  RecorderMetrics m;
  const int st = state_.load();
  m.state = kStates[st];
  m.ingest = ingest_.stats();
  m.writer = writer_.stats();
  m.uptime_s = std::chrono::duration<double>(SteadyClock::now() - started_).count();
  for (std::size_t i = 0; i < profile_.topics.size(); ++i) {
    TopicMetrics t;
    t.name = profile_.topics[i].name;
    t.received = received_[i].load(std::memory_order_relaxed);
    t.dropped_ingest = dropped_[i].load(std::memory_order_relaxed);
    m.received += t.received;
    m.dropped_ingest += t.dropped_ingest;
    m.topics.push_back(std::move(t));
  }
  m.dropped_at_shutdown = dropped_at_shutdown_.load();
  m.rejected_after_stop = rejected_after_stop_.load();
  m.decoded = decoded_.load();
  m.decode_errors = decode_error_total_.load();
  m.decimated = decimated_.load();
  m.processed = processed_.load();
  std::lock_guard lock(snapshot_mu_);
  m.core = core_snapshot_;
  m.system = system_snapshot_;
  m.fatal_error = fatal_error_;
  return m;
}

}  // namespace blackboxrs::recorder
