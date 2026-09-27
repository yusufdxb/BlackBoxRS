#include <gtest/gtest.h>
#include <sys/resource.h>

#include <atomic>
#include <csignal>
#include <thread>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "blackboxrs/recorder/bounded_queue.hpp"
#include "blackboxrs/recorder/recorder.hpp"
#include "test_support.hpp"

namespace blackboxrs::recorder {
namespace {
namespace fs = std::filesystem;
using namespace std::chrono_literals;

// ---------------------------------------------------------------------------
// bounded queue
// ---------------------------------------------------------------------------

TEST(BoundedQueue, RejectsWhenFullAndCountsIt) {
  BoundedQueue<int> q(4, 1);
  for (int i = 0; i < 3; ++i) {
    int v = i;
    EXPECT_EQ(q.try_push(v), PushResult::ok);
  }
  int v = 9;
  EXPECT_EQ(q.try_push(v), PushResult::full) << "data lane stops at capacity - reserve";
  EXPECT_EQ(v, 9) << "a rejected item is not consumed";
  EXPECT_EQ(q.try_push(v, Lane::control), PushResult::ok) << "control may use the reserve";
  EXPECT_EQ(q.try_push(v, Lane::control), PushResult::full);
  const auto s = q.stats();
  EXPECT_EQ(s.depth, 4U);
  EXPECT_EQ(s.high_water, 4U);
  EXPECT_EQ(s.rejected_full_data, 1U);
  EXPECT_EQ(s.rejected_full_control, 1U);
}

TEST(BoundedQueue, FifoAcrossTheRingBoundary) {
  BoundedQueue<int> q(3);
  std::vector<int> out;
  for (int round = 0; round < 5; ++round) {
    for (int i = 0; i < 3; ++i) {
      int v = round * 10 + i;
      ASSERT_EQ(q.try_push(v), PushResult::ok);
    }
    out.clear();
    ASSERT_EQ(q.pop_batch(out, 10, std::chrono::steady_clock::now(), {}), 3U);
    EXPECT_EQ(out, (std::vector<int>{round * 10, round * 10 + 1, round * 10 + 2}));
  }
}

TEST(BoundedQueue, CloseRejectsPushesButKeepsQueuedItems) {
  BoundedQueue<int> q(8);
  int v = 1;
  ASSERT_EQ(q.try_push(v), PushResult::ok);
  q.close();
  int w = 2;
  EXPECT_EQ(q.try_push(w), PushResult::closed);
  std::vector<int> out;
  EXPECT_EQ(q.pop_batch(out, 8, std::chrono::steady_clock::now() + 1s, {}), 1U);
  EXPECT_EQ(q.pop_batch(out, 8, std::chrono::steady_clock::now() + 1s, {}), 0U)
      << "closed and empty returns at once";
}

TEST(BoundedQueue, PopWakesOnStopRequest) {
  BoundedQueue<int> q(8);
  std::stop_source src;
  std::jthread t([&] {
    std::this_thread::sleep_for(20ms);
    src.request_stop();
  });
  std::vector<int> out;
  const auto t0 = std::chrono::steady_clock::now();
  EXPECT_EQ(q.pop_batch(out, 8, t0 + 10s, src.get_token()), 0U);
  EXPECT_LT(std::chrono::steady_clock::now() - t0, 5s);
}

TEST(BoundedQueue, ConcurrentProducersConserveEveryItem) {
  BoundedQueue<int> q(64);
  constexpr int kProducers = 4;
  constexpr int kEach = 20'000;
  std::atomic<int> ok{0};
  std::atomic<int> full{0};
  std::atomic<bool> done{false};
  std::int64_t consumed = 0;
  std::jthread consumer([&] {
    std::vector<int> out;
    while (!done.load() || q.stats().depth != 0) {
      out.clear();
      consumed += static_cast<std::int64_t>(
          q.pop_batch(out, 32, std::chrono::steady_clock::now() + 1ms, {}));
    }
  });
  {
    std::vector<std::jthread> producers;
    for (int p = 0; p < kProducers; ++p) {
      producers.emplace_back([&] {
        for (int i = 0; i < kEach; ++i) {
          int v = i;
          (q.try_push(v) == PushResult::ok ? ok : full).fetch_add(1);
        }
      });
    }
  }
  done.store(true);
  consumer.join();
  EXPECT_EQ(ok.load() + full.load(), kProducers * kEach);
  EXPECT_EQ(consumed, ok.load());
  EXPECT_EQ(q.stats().rejected_full_data, static_cast<std::uint64_t>(full.load()));
}

// ---------------------------------------------------------------------------
// recorder
// ---------------------------------------------------------------------------

class JsonPayload final : public Payload {
 public:
  explicit JsonPayload(Json j) : json_(std::move(j)) {}
  [[nodiscard]] std::size_t size_bytes() const noexcept override { return 64; }
  const Json& json() const { return json_; }

 private:
  Json json_;
};

class JsonDecoder final : public MessageDecoder {
 public:
  explicit JsonDecoder(std::chrono::microseconds cost = 0us) : cost_(cost) {}
  DecodeResult decode(std::size_t, const Payload& p) override {
    if (cost_.count() != 0) {
      std::this_thread::sleep_for(cost_);
    }
    DecodeResult r;
    r.data = std::optional<Json>(std::in_place, dynamic_cast<const JsonPayload&>(p).json());
    return r;
  }

 private:
  std::chrono::microseconds cost_;
};

const char* kProfile = R"(profile: test
buffer: {pre_trigger_sec: 1.0, post_trigger_sec: 0.3}
sampling: {graph_poll_sec: 0.5, system_sample_hz: 20.0, health_tick_sec: 0.02}
topics:
  - {name: /nav/cmd_vel, type: geometry_msgs/msg/Twist, role: cmd_vel_source}
  - {name: /helix/hold, type: helix_msgs/msg/HelixHold, role: helix_hold, stale_after_sec: 0.5}
  - {name: /lowstate, type: unitree_go/msg/LowState, role: go2_state, store_max_hz: 20.0}
)";

Json twist(double x) {
  return {{"linear", {{"x", x}, {"y", 0.0}, {"z", 0.0}}},
          {"angular", {{"x", 0.0}, {"y", 0.0}, {"z", 0.0}}}};
}

RecorderConfig config_for(const fs::path& dir, bool continuous) {
  RecorderConfig c;
  c.continuous = continuous;
  c.writer.session_dir = dir;
  c.writer.chunk_records = 16;
  c.ingest_capacity = 4096;
  c.drain_deadline = 2000ms;
  c.manifest.session = {{"session_id", "test"}, {"synthetic", true}};
  c.manifest.profile = Json::object();
  c.manifest.config_sha256 = "test";
  c.manifest.writer_build = {{"implementation", "blackboxrs-cpp"}};
  return c;
}

MessageArrival arrival(std::uint32_t topic, Json data) {
  MessageArrival m;
  m.topic = topic;
  m.t_mono = clock_domain::Mono::now();
  m.t_wall = clock_domain::Wall::now();
  m.src = source_ns(count_ns(m.t_wall) - 1000);
  m.rx = m.t_wall;
  m.payload = std::make_shared<JsonPayload>(std::move(data));
  return m;
}

std::vector<fs::path> bundles_in(const fs::path& dir) {
  std::vector<fs::path> out;
  for (const auto& e : fs::directory_iterator(dir)) {
    if (e.is_directory()) {
      out.push_back(e.path());
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

std::uint64_t accounted(const RecorderMetrics& m) {
  return m.processed + m.dropped_ingest + m.dropped_at_shutdown;
}

TEST(Recorder, ContinuousCaptureIsVerifiedAndReplayable) {
  testing::TempDir tmp;
  Profile p = parse_profile_text(kProfile);
  RecorderMetrics m;
  {
    Recorder rec(p, config_for(tmp.path(), true), std::make_unique<JsonDecoder>());
    for (int i = 0; i < 200; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
      rec.on_message(arrival(1, {{"hold", false}, {"epoch", 1}, {"seq", i}, {"fault_id", ""}}));
      rec.on_message(arrival(2, {{"tick", i}}));
      std::this_thread::sleep_for(1ms);
    }
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_EQ(m.state, "stopped");
  EXPECT_EQ(m.received, 600U);
  EXPECT_EQ(accounted(m), m.received) << "every received message is processed or counted";
  EXPECT_EQ(m.dropped_ingest, 0U);
  EXPECT_GT(m.decimated, 0U) << "/lowstate is stored at 20 Hz only";
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  EXPECT_FALSE(dirs[0].string().ends_with(".partial"));
  const ValidationReport v = validate_bundle(dirs[0]);
  EXPECT_EQ(v.status, ValidationStatus::verified) << v.to_json().dump(2);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "complete");
  EXPECT_EQ(b.manifest["triggers"][0]["type"], "recording_started");
  std::size_t msgs = 0;
  for (const auto& r : b.records) {
    msgs += r["kind"] == "msg" ? 1U : 0U;
  }
  EXPECT_EQ(msgs, 600U) << "every message has a record, decimated or not";
}

TEST(Recorder, TriggeredIncidentOpensOnHoldAndClosesAfterPostWindow) {
  testing::TempDir tmp;
  Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), false),
               std::make_unique<JsonDecoder>());
  for (int i = 0; i < 20; ++i) {
    rec.on_message(arrival(0, twist(0.1)));
    std::this_thread::sleep_for(2ms);
  }
  rec.on_message(arrival(1, {{"hold", true}, {"epoch", 1}, {"seq", 1}, {"fault_id", "F7"}}));
  for (int i = 0; i < 60; ++i) {  // past the 0.3 s post-trigger window
    rec.on_message(arrival(0, twist(0.0)));
    std::this_thread::sleep_for(10ms);
  }
  rec.stop();
  // The hold incident closes after its post window. The single hold message
  // then goes stale (0.5 s), which is a second, separate incident; stopping
  // cuts that one short, so it is interrupted.
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 2U);
  const BundleRead hold = load_bundle(dirs[0]);
  EXPECT_EQ(hold.manifest["status"], "complete");
  EXPECT_EQ(hold.manifest["triggers"][0]["type"], "helix_hold_asserted");
  EXPECT_EQ(hold.manifest["triggers"][0]["fault_id"], "F7");
  EXPECT_EQ(validate_bundle(dirs[0]).status, ValidationStatus::verified);
  const BundleRead stale = load_bundle(dirs[1]);
  EXPECT_EQ(stale.manifest["triggers"][0]["type"], "topic_stale");
  EXPECT_EQ(stale.manifest["status"], "interrupted");
  EXPECT_EQ(validate_bundle(dirs[1]).status, ValidationStatus::invalid)
      << "an interrupted incident is not clean evidence";
}

TEST(Recorder, OverloadIsCountedPerTopicAndMarksTheBundle) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), true);
  cfg.ingest_capacity = 32;
  cfg.control_reserve = 4;
  RecorderMetrics m;
  {
    // A decoder slower than the producer: the queue must fill and reject.
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>(200us));
    for (int i = 0; i < 3000; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_GT(m.dropped_ingest, 0U);
  EXPECT_EQ(m.topics[0].dropped_ingest, m.dropped_ingest) << "attributed to its topic";
  EXPECT_EQ(accounted(m), m.received);
  EXPECT_LE(m.ingest.high_water, 32U) << "bounded";
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "complete_with_loss")
      << "a bundle that lost messages is never marked complete";
  EXPECT_GT(b.manifest["recorder_stats"]["messages_lost_before_core_during_bundle"].get<int>(), 0);
}

TEST(Recorder, StopWhileBusyDrainsFinalizesAndJoins) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), true);
  cfg.drain_deadline = 50ms;  // shorter than the backlog takes
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>(500us));
    for (int i = 0; i < 1000; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    rec.stop("test_stop");  // queue non-empty, writer active
    m = rec.metrics();
    EXPECT_EQ(rec.on_message(arrival(0, twist(0.1))), PushResult::closed);
  }
  EXPECT_EQ(m.state, "stopped");
  EXPECT_GT(m.dropped_at_shutdown, 0U) << "what the deadline cut off is counted, not lost";
  EXPECT_EQ(accounted(m), m.received);
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  EXPECT_FALSE(dirs[0].string().ends_with(".partial"));
  EXPECT_EQ(load_bundle(dirs[0]).manifest["status"], "complete_with_loss");
  EXPECT_EQ(validate_bundle(dirs[0]).status, ValidationStatus::invalid)
      << "complete_with_loss never validates as clean evidence";
}

TEST(Recorder, ConcurrentProducersAreFullyAccounted) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), true);
  cfg.ingest_capacity = 256;
  cfg.control_reserve = 16;
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>());
    {
      std::vector<std::jthread> producers;
      for (std::uint32_t t = 0; t < 3; ++t) {
        producers.emplace_back([&rec, t] {
          for (int i = 0; i < 5000; ++i) {
            rec.on_message(
                arrival(t, t == 1 ? Json{{"hold", false}, {"epoch", 1}, {"seq", i}} : twist(0.1)));
          }
        });
      }
    }
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_EQ(m.received, 15000U);
  EXPECT_EQ(accounted(m), m.received);
}

TEST(Recorder, DiskFloorFailsAContinuousCapture) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), true);
  cfg.hard_disk_floor_mb = std::int64_t{1} << 40;  // more than any disk
  Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>());
  rec.on_message(arrival(0, twist(0.1)));
  rec.stop();
  const auto m = rec.metrics();
  EXPECT_EQ(m.core["incidents_skipped"], 1);
  EXPECT_TRUE(bundles_in(tmp.path()).empty());
  EXPECT_EQ(m.state, "failed") << "a continuous capture that records nothing has failed";
  EXPECT_NE(m.fatal_error.find("disk_pressure"), std::string::npos) << m.fatal_error;
}

TEST(Recorder, DiskFloorInTriggeredModeIsCountedNotFatal) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), false);
  cfg.hard_disk_floor_mb = std::int64_t{1} << 40;
  Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>());
  rec.on_message(arrival(1, {{"hold", true}, {"epoch", 1}, {"seq", 1}, {"fault_id", "F1"}}));
  rec.stop();
  const auto m = rec.metrics();
  EXPECT_EQ(m.state, "stopped");
  EXPECT_EQ(m.core["incidents_skipped"], 1);
  EXPECT_EQ(m.core["skip_reasons"].size(), 1U);
}

// A decoder that runs out of memory on the n-th message.
class FailingDecoder final : public MessageDecoder {
 public:
  explicit FailingDecoder(int fail_at) : fail_at_(fail_at) {}
  DecodeResult decode(std::size_t, const Payload& p) override {
    if (++n_ == fail_at_) {
      throw std::bad_alloc();
    }
    DecodeResult r;
    r.data = std::optional<Json>(std::in_place, dynamic_cast<const JsonPayload&>(p).json());
    return r;
  }

 private:
  int fail_at_;
  int n_ = 0;
};

TEST(Recorder, PipelineFailureClosesTheBundleAsPipelineFailed) {
  testing::TempDir tmp;
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), true),
                 std::make_unique<FailingDecoder>(100));
    for (int i = 0; i < 300; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    std::this_thread::sleep_for(100ms);
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_EQ(m.state, "failed");
  EXPECT_NE(m.fatal_error.find("bad_alloc"), std::string::npos) << m.fatal_error;
  EXPECT_EQ(m.processed, 99U);
  EXPECT_EQ(m.received, 300U - m.rejected_after_stop);
  EXPECT_EQ(accounted(m), m.received) << "the failed message and everything queued are counted";
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "pipeline_failed");
  EXPECT_THROW((void)load_evidence(dirs[0], false, ""), EvidenceError);
  EXPECT_EQ(validate_bundle(dirs[0]).status, ValidationStatus::invalid);
}

TEST(Recorder, InvalidUtf8InATriggerIsReplacedNotLost) {
  testing::TempDir tmp;
  {
    Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), false),
                 std::make_unique<JsonDecoder>());
    rec.on_message(arrival(1, {{"hold", true},
                               {"epoch", 1},
                               {"seq", 1},
                               {"fault_id", std::string("F\xff\xfe")},
                               {"reason", std::string("bad \xc3")}}));
    rec.mark(std::string("note \x80"), "test");
    for (int i = 0; i < 40; ++i) {  // past the post window
      rec.on_message(arrival(1, {{"hold", true}, {"epoch", 1}, {"seq", i + 2}}));
      std::this_thread::sleep_for(10ms);
    }
    rec.stop();
    const auto m = rec.metrics();
    EXPECT_EQ(m.writer.write_errors, 0U);
  }
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  EXPECT_FALSE(dirs[0].string().ends_with(".partial"));
  EXPECT_EQ(validate_bundle(dirs[0]).status, ValidationStatus::verified);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["triggers"][0]["fault_id"], "F\xef\xbf\xbd\xef\xbf\xbd");
}

TEST(Recorder, ABacklogDoesNotMakeATopicStale) {
  // The hold topic publishes every 100 ms (stale after 0.5 s), behind a
  // second of decode backlog. Judged at "now", it would go stale while its
  // messages are merely queued; judged at the time of the data processed so
  // far, it never does.
  testing::TempDir tmp;
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), false),
                 std::make_unique<JsonDecoder>(4ms));
    rec.on_message(arrival(1, {{"hold", false}, {"epoch", 1}, {"seq", 0}}));
    for (int i = 0; i < 250; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    for (int k = 1; k <= 13; ++k) {
      std::this_thread::sleep_for(100ms);
      rec.on_message(arrival(1, {{"hold", false}, {"epoch", 1}, {"seq", k}}));
    }
    while (rec.metrics().processed < 264U) {
      std::this_thread::sleep_for(2ms);
    }
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_EQ(m.dropped_ingest, 0U);
  EXPECT_EQ(m.core["triggers_fired"], 0) << m.core.dump();
  EXPECT_TRUE(bundles_in(tmp.path()).empty());
}

TEST(Recorder, LossInThePreTriggerWindowMarksTheBundle) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), false);
  cfg.ingest_capacity = 32;
  cfg.control_reserve = 4;
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>(200us));
    for (int i = 0; i < 500; ++i) {  // overload: most are dropped
      rec.on_message(arrival(0, twist(0.1)));
    }
    while (rec.metrics().ingest.depth != 0) {
      std::this_thread::sleep_for(1ms);
    }
    // The hold arrives after the drops, well inside their pre-trigger window.
    for (int i = 0; i < 20; ++i) {
      rec.on_message(arrival(1, {{"hold", true}, {"epoch", 1}, {"seq", i + 1}}));
      std::this_thread::sleep_for(25ms);
    }
    rec.stop();
    m = rec.metrics();
  }
  ASSERT_GT(m.dropped_ingest, 0U);
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "complete_with_loss");
  EXPECT_EQ(b.manifest["recorder_stats"]["messages_lost_before_core_during_bundle"],
            m.dropped_ingest);
}

TEST(Recorder, LossOutsideTheBundleWindowDoesNotMarkIt) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), false);
  cfg.ingest_capacity = 32;
  cfg.control_reserve = 4;
  {
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>(200us));
    for (int i = 0; i < 500; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    // pre-trigger window 1 s + 1 s margin: wait it out.
    std::this_thread::sleep_for(2300ms);
    for (int i = 0; i < 20; ++i) {
      rec.on_message(arrival(1, {{"hold", true}, {"epoch", 1}, {"seq", i + 1}}));
      std::this_thread::sleep_for(25ms);
    }
    rec.stop();
    ASSERT_GT(rec.metrics().dropped_ingest, 0U);
  }
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  EXPECT_EQ(load_bundle(dirs[0]).manifest["status"], "complete");
}

TEST(Recorder, DdsMessageLostMarksTheBundle) {
  testing::TempDir tmp;
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), true),
                 std::make_unique<JsonDecoder>());
    rec.on_message(arrival(0, twist(0.1)));
    GraphSnapshot g;
    g.t_mono = clock_domain::Mono::now();
    g.t_wall = clock_domain::Wall::now();
    g.topic_status["/nav/cmd_vel"] = {{"message_lost", std::uint64_t{7}}};
    rec.on_graph(g);
    g.topic_status["/nav/cmd_vel"] = {{"message_lost", std::uint64_t{9}}};  // cumulative
    rec.on_graph(g);
    rec.on_message(arrival(0, twist(0.1)));
    std::this_thread::sleep_for(50ms);
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_EQ(m.dropped_dds, 9U);
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "complete_with_loss");
  EXPECT_EQ(b.manifest["recorder_stats"]["messages_lost_before_core_during_bundle"], 9);
}

TEST(Recorder, AStalledWriterIsBoundedAndFailsTheBundle) {
  testing::TempDir tmp;
  RecorderConfig cfg = config_for(tmp.path(), true);
  cfg.writer.queue_capacity = 8;
  cfg.writer.stall_timeout = 50ms;
  std::atomic<bool> release{false};
  cfg.writer.before_op = [&release] {
    while (!release.load()) {
      std::this_thread::sleep_for(1ms);
    }
  };
  RecorderMetrics m;
  {
    Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>());
    for (int i = 0; i < 50; ++i) {
      rec.on_message(arrival(0, twist(0.1)));
    }
    // The pipeline keeps going although the writer takes nothing.
    const auto t0 = std::chrono::steady_clock::now();
    while (rec.metrics().processed < 50U) {
      ASSERT_LT(std::chrono::steady_clock::now() - t0, 10s) << "pipeline blocked by the writer";
      std::this_thread::sleep_for(5ms);
    }
    release.store(true);
    rec.stop();
    m = rec.metrics();
  }
  EXPECT_GT(m.writer.ops_dropped_stalled, 0U);
  const auto dirs = bundles_in(tmp.path());
  ASSERT_EQ(dirs.size(), 1U);
  EXPECT_TRUE(dirs[0].string().ends_with(".partial"));
  const BundleRead b = load_bundle(dirs[0]);
  EXPECT_EQ(b.manifest["status"], "write_failed");
  EXPECT_NE(b.manifest["writer"]["first_write_error"].get<std::string>().find("writer stalled"),
            std::string::npos);
}

TEST(Recorder, MessagesAfterStopAreNotCountedAsReceived) {
  testing::TempDir tmp;
  Recorder rec(parse_profile_text(kProfile), config_for(tmp.path(), true),
               std::make_unique<JsonDecoder>());
  rec.on_message(arrival(0, twist(0.1)));
  rec.stop();
  EXPECT_EQ(rec.on_message(arrival(0, twist(0.1))), PushResult::closed);
  const auto m = rec.metrics();
  EXPECT_EQ(m.received, 1U);
  EXPECT_EQ(m.rejected_after_stop, 1U);
  EXPECT_EQ(accounted(m), m.received);
}

TEST(RecorderDeathTest, WriteFailureIsNeverFinalized) {
  // A real OS write failure: RLIMIT_FSIZE makes write() fail with EFBIG once
  // the file would exceed the limit. Runs in a child process.
  testing::TempDir tmp;
  const fs::path dir = tmp.path();
  EXPECT_EXIT(
      {
        std::signal(SIGXFSZ, SIG_IGN);
        rlimit lim{};
        lim.rlim_cur = 64 * 1024;
        lim.rlim_max = 64 * 1024;
        setrlimit(RLIMIT_FSIZE, &lim);
        RecorderConfig cfg = config_for(dir, true);
        {
          Recorder rec(parse_profile_text(kProfile), cfg, std::make_unique<JsonDecoder>());
          for (int i = 0; i < 3000; ++i) {
            rec.on_message(arrival(0, twist(0.1)));
          }
          rec.stop();
          const auto m = rec.metrics();
          if (m.writer.write_errors == 0 || m.writer.bundles_failed != 1 ||
              m.writer.bundles_finalized != 0) {
            std::_Exit(10);
          }
        }
        const auto dirs = bundles_in(dir);
        if (dirs.size() != 1 || !dirs[0].string().ends_with(".partial")) std::_Exit(11);
        const BundleRead b = load_bundle(dirs[0]);
        if (b.manifest["status"] != "write_failed") std::_Exit(12);
        if (validate_bundle(dirs[0]).status != ValidationStatus::invalid) std::_Exit(13);
        std::_Exit(0);
      },
      ::testing::ExitedWithCode(0), "");
}

// ---------------------------------------------------------------------------
// integrity validation
// ---------------------------------------------------------------------------

fs::path recorded_bundle(const fs::path& dir) {
  Recorder rec(parse_profile_text(kProfile), config_for(dir, true),
               std::make_unique<JsonDecoder>());
  for (int i = 0; i < 100; ++i) {
    rec.on_message(arrival(0, twist(0.01 * i)));
  }
  rec.stop();
  return bundles_in(dir).at(0);
}

TEST(Integrity, FlippedByteIsDetectedAndLocated) {
  testing::TempDir tmp;
  const fs::path b = recorded_bundle(tmp.path());
  std::string recs = testing::read_file(b / "records.jsonl");
  recs[recs.size() / 2] ^= 0x01;
  testing::write_file(b / "records.jsonl", recs);
  const auto v = validate_bundle(b);
  EXPECT_EQ(v.status, ValidationStatus::invalid);
  const std::string all = v.to_json().dump();
  EXPECT_NE(all.find("fails its CRC-32C"), std::string::npos) << all;
  EXPECT_NE(all.find("SHA-256 does not match"), std::string::npos);
}

TEST(Integrity, TruncationAndAppendAreDetected) {
  testing::TempDir tmp;
  const fs::path b = recorded_bundle(tmp.path());
  const std::string recs = testing::read_file(b / "records.jsonl");
  testing::write_file(b / "records.jsonl", recs.substr(0, recs.size() - 40));
  EXPECT_NE(validate_bundle(b).to_json().dump().find("truncated"), std::string::npos);
  testing::write_file(b / "records.jsonl", recs + recs.substr(0, 200));
  EXPECT_NE(validate_bundle(b).to_json().dump().find("appended after finalization"),
            std::string::npos);
}

TEST(Integrity, PythonBundlesAreUnverifiableNotVerified) {
  const auto v = validate_bundle(testing::golden_evidence("clean_stop"));
  EXPECT_EQ(v.status, ValidationStatus::unverifiable);
}

}  // namespace
}  // namespace blackboxrs::recorder
