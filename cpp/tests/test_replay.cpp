#include <gtest/gtest.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <map>
#include <memory>
#include <stdexcept>
#include <vector>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/integrity.hpp"
#include "blackboxrs/replay/engine.hpp"
#include "blackboxrs/replay/faults.hpp"
#include "blackboxrs/replay/pacing.hpp"
#include "blackboxrs/replay/sut.hpp"
#include "test_support.hpp"

namespace blackboxrs::replay {
namespace {

const Evidence& nominal() {
  static const Evidence ev = load_evidence(testing::golden_evidence("nominal_motion"), false);
  return ev;
}
const Evidence& clean_stop() {
  static const Evidence ev = load_evidence(testing::golden_evidence("clean_stop"), false);
  return ev;
}

class GoldenCase : public ::testing::TestWithParam<std::string> {};

TEST_P(GoldenCase, MeetsExpectationAndIsDeterministic) {
  const Case c = load_case(GetParam());
  const Evidence ev = load_evidence(c.evidence, false, c.evidence);
  const Json a = replay(ev, c.config);
  const Json b = replay(ev, c.config);
  EXPECT_EQ(check_expectations(a, c.expect), std::vector<std::string>{}) << c.name;
  EXPECT_EQ(canonical_json(a), canonical_json(b)) << c.name;
}

std::vector<std::string> all_cases() {
  return list_case_files(testing::golden_cases().string());
}

INSTANTIATE_TEST_SUITE_P(Replay, GoldenCase, ::testing::ValuesIn(all_cases()),
                         [](const ::testing::TestParamInfo<std::string>& p) {
                           std::string n = std::filesystem::path(p.param).stem().string();
                           for (char& ch : n) {
                             if (!std::isalnum(static_cast<unsigned char>(ch))) ch = '_';
                           }
                           return n;
                         });

TEST(Replay, GoldenSetHasEveryFailureClass) {
  EXPECT_EQ(all_cases().size(), 30U);
}

TEST(Replay, PacingNeverChangesTheResult) {
  ReplayConfig cfg;
  cfg.faults.push_back(parse_cli_fault("drop:topic=/nav/cmd_vel,from_s=4.0", 0));
  const Json unpaced = replay(nominal(), cfg);
  Nanos slept{0};
  ReplayOptions opts;
  opts.pacer = [&](Nanos step) { slept += step; };  // a fake sleep records the virtual steps
  const Json paced = replay(nominal(), cfg, opts);
  EXPECT_EQ(canonical_json(unpaced), canonical_json(paced));
  EXPECT_EQ(slept.count(), unpaced["replay"]["window_end_ns"].get<std::int64_t>())
      << "the pacer is told every virtual step, and only that";
}

// A fake steady clock for DeadlinePacer: the replay's own work and the
// sleeps' overshoot are simulated by moving `now` explicitly.
struct FakeWall {
  using Clock = DeadlinePacer::Clock;
  Clock::time_point now{std::chrono::seconds(1000)};
  Nanos overshoot{0};                     // every sleep wakes this late
  std::vector<Clock::time_point> sleeps;  // the deadlines slept until
  DeadlinePacer pacer(double speed) {
    return DeadlinePacer(
        speed, [this] { return now; },
        [this](Clock::time_point t) {
          sleeps.push_back(t);
          now = std::max(now, t) + overshoot;
        });
  }
};

TEST(DeadlinePacer, DeadlinesAreCumulativeFromOneOriginSoOverheadDoesNotDrift) {
  using namespace std::chrono_literals;
  FakeWall w;
  w.overshoot = 1ms;
  DeadlinePacer p = w.pacer(2.0);
  EXPECT_FALSE(p.deadline().has_value());
  const auto origin = w.now + 300us;  // the schedule starts at the first step
  for (int k = 1; k <= 100; ++k) {
    w.now += 300us;  // the replay's processing between steps
    p.pace(10ms);    // 10 ms virtual at speed 2: 5 ms of wall time
    EXPECT_EQ(*p.deadline(), origin + k * 5ms);
  }
  ASSERT_EQ(w.sleeps.size(), 100U);
  for (std::size_t k = 0; k < w.sleeps.size(); ++k) {
    EXPECT_EQ(w.sleeps[k], origin + static_cast<int>(k + 1) * 5ms)
        << "each deadline is origin + cumulative virtual time / speed";
  }
  // Per-step sleeps would end at 100 x (5 + 1 + 0.3) ms = 630 ms; absolute
  // deadlines end one overshoot after the intended 500 ms.
  EXPECT_EQ(w.now - origin, 501ms);
}

TEST(DeadlinePacer, BehindScheduleItDoesNotSleepAndCatchesUp) {
  using namespace std::chrono_literals;
  FakeWall w;
  DeadlinePacer p = w.pacer(1.0);
  const auto origin = w.now;
  p.pace(Nanos{0});  // the first step starts the schedule
  w.now += 25ms;     // a slow stretch: 2.5 steps late
  p.pace(10ms);
  p.pace(10ms);
  EXPECT_TRUE(w.sleeps.empty()) << "late steps return at once";
  p.pace(10ms);  // deadline 30 ms, now 25 ms
  ASSERT_EQ(w.sleeps.size(), 1U);
  EXPECT_EQ(w.sleeps[0], origin + 30ms) << "still measured from the origin, not from the delay";
}

TEST(DeadlinePacer, TargetsComeFromCumulativeTimeNotRoundedIntervals) {
  FakeWall w;
  DeadlinePacer p = w.pacer(3.0);
  const auto origin = w.now;
  for (int k = 0; k < 9; ++k) {
    p.pace(Nanos{1});  // 1/3 ns each: rounding every interval would never advance
  }
  EXPECT_EQ(*p.deadline(), origin + Nanos{3});
  EXPECT_EQ(w.sleeps.back(), origin + Nanos{3});
}

TEST(DeadlinePacer, ZeroStepsNeverSleepAndRebaseRestartsTheSchedule) {
  using namespace std::chrono_literals;
  FakeWall w;
  DeadlinePacer p = w.pacer(1.0);
  p.pace(Nanos{0});
  p.pace(Nanos{0});
  EXPECT_TRUE(w.sleeps.empty());
  p.pace(10ms);
  w.now += 1h;  // an interactive pause (--step) far behind the schedule
  p.rebase();
  const auto resumed = w.now;
  p.pace(10ms);
  ASSERT_EQ(w.sleeps.size(), 2U);
  EXPECT_EQ(w.sleeps[1], resumed + 10ms) << "the pause is not caught up";
}

TEST(DeadlinePacer, RejectsNonPositiveSpeed) {
  // Braces, not parentheses: clang parses DeadlinePacer(std::numeric_limits<
  // double>::quiet_NaN()) as a declaration (most vexing parse).
  EXPECT_THROW(DeadlinePacer{0.0}, std::invalid_argument);
  EXPECT_THROW(DeadlinePacer{-1.0}, std::invalid_argument);
  EXPECT_THROW(DeadlinePacer{std::numeric_limits<double>::quiet_NaN()}, std::invalid_argument);
}

TEST(Replay, DeadlinePacingNeverChangesTheResult) {
  ReplayConfig cfg;
  cfg.faults.push_back(parse_cli_fault("drop:topic=/nav/cmd_vel,from_s=4.0", 0));
  const Json unpaced = replay(nominal(), cfg);
  FakeWall w;
  w.overshoot = std::chrono::microseconds(70);
  DeadlinePacer pacer = w.pacer(4.0);
  const auto origin = w.now;
  ReplayOptions opts;
  opts.pacer = [&](Nanos step) { pacer.pace(step); };
  const Json paced = replay(nominal(), cfg, opts);
  EXPECT_EQ(canonical_json(unpaced), canonical_json(paced)) << "virtual time, order and ticks";
  const auto window = unpaced["replay"]["window_end_ns"].get<std::int64_t>();
  EXPECT_EQ(*pacer.deadline(), origin + Nanos{window / 4});
  ASSERT_FALSE(w.sleeps.empty());
  EXPECT_EQ(w.sleeps.back(), origin + Nanos{window / 4});
}

TEST(Replay, ClockNeverMovesBackwards) {
  ReplayClock c(replay_ns(100));
  EXPECT_EQ(c.advance_to(replay_ns(150)).count(), 50);
  EXPECT_EQ(c.advance_to(replay_ns(150)).count(), 0) << "equal timestamps are a zero step";
  EXPECT_THROW((void)c.advance_to(replay_ns(149)), ClockError);
}

TEST(Replay, StaleCommandThroughLegacyMuxFails) {
  ReplayConfig cfg;
  cfg.preset = "twist_mux_legacy";
  cfg.faults.push_back(parse_cli_fault("drop:topic=/nav/cmd_vel,from_s=4.0", 0));
  const Json r = replay(nominal(), cfg);
  EXPECT_EQ(r["verdict"]["result"], "FAIL");
  EXPECT_EQ(r["invariants"]["fresh_output"]["status"], "FAIL");
  EXPECT_EQ(exit_code_for(r), ExitCode::fail);
}

TEST(Replay, StaleCommandThroughHelixArbiterIsDetectedAndStopped) {
  ReplayConfig cfg;
  cfg.faults.push_back(parse_cli_fault("drop:topic=/nav/cmd_vel,from_s=4.0", 0));
  const Json r = replay(nominal(), cfg);
  EXPECT_EQ(r["verdict"]["result"], "DETECTED");
  EXPECT_EQ(r["invariants"]["fresh_output"]["status"], "PASS");
  EXPECT_EQ(detection_kinds(r), std::vector<std::string>{"command_source_stale"});
}

TEST(Replay, NanCommandNeverReachesTheRobotThroughHelixArbiter) {
  ReplayConfig cfg;
  cfg.faults.push_back(
      parse_cli_fault("nan:topic=/nav/cmd_vel,field=linear.x,from_s=3.0,to_s=3.5", 0));
  const Json r = replay(nominal(), cfg);
  EXPECT_EQ(r["invariants"]["finite_output"]["status"], "PASS");
  EXPECT_GT(r["sut_state"]["counters"]["rejected"].get<int>(), 0);
}

TEST(Replay, MissingOutputIsIncompleteNeverPass) {
  // Observed mode over evidence whose output topics are all dropped: the
  // replay cannot see what reached the robot, so it must not pass.
  ReplayConfig cfg;
  cfg.sut_mode = "observed";
  cfg.faults.push_back(parse_cli_fault("drop:topics=/cmd_vel|/helix/arbiter/status", 0));
  const Json r = replay(nominal(), cfg);
  EXPECT_NE(r["verdict"]["result"], "PASS");
  EXPECT_EQ(r["invariants"]["finite_output"]["status"], "INCOMPLETE");
}

TEST(Replay, PartialEvidenceCapsTheVerdict) {
  testing::TempDir tmp;
  const auto dir = tmp.path() / "b";
  testing::copy_dir(testing::golden_evidence("nominal_motion"), dir);
  std::string recs = testing::read_file(dir / "records.jsonl");
  testing::write_file(dir / "records.jsonl", recs + "{\"kind\":\"msg\",\"se");
  const Evidence ev = load_evidence(dir, true);
  const Json r = replay(ev, ReplayConfig{});
  EXPECT_EQ(r["verdict"]["result"], "INCOMPLETE");
}

TEST(Replay, WindowStartsWithoutEarlierStateAndSaysSo) {
  ReplayConfig cfg;
  cfg.from_s = 3.0;
  const Json r = replay(clean_stop(), cfg);
  ASSERT_EQ(r["replay"]["notes"].size(), 1U);
  EXPECT_EQ(r["replay"]["window_start_ns"], 3'000'000'000);
}

TEST(Replay, BadWindowRefused) {
  ReplayConfig cfg;
  cfg.from_s = 5.0;
  cfg.to_s = 4.0;
  EXPECT_THROW((void)replay(nominal(), cfg), EvidenceError);
}

TEST(Replay, GraceWindowsAreBounded) {
  ReplayConfig cfg;
  cfg.stop_grace_s = 0.6;
  EXPECT_THROW((void)replay(nominal(), cfg), std::invalid_argument);
}

TEST(Replay, FaultOnlyOnRecordedOutputRefusedInReferenceMode) {
  ReplayConfig cfg;
  cfg.faults.push_back(parse_cli_fault("nan:topic=/cmd_vel,field=linear.x", 0));
  EXPECT_THROW((void)replay(nominal(), cfg), FaultError);
}

// --- faults --------------------------------------------------------------------

std::vector<Event> nominal_events(TopicTable& topics) {
  topics = topic_table(nominal(), Json::object());
  std::vector<Event> ev;
  for (const Json& r : nominal().records) {
    if (is_input_kind(r["kind"].get<std::string>())) {
      ev.push_back(event_from_record(r, nominal().t0));
    }
  }
  std::sort(ev.begin(), ev.end());
  return ev;
}

TEST(Faults, AnEqualOrderKeyIsRefused) {
  std::vector<Event> v(2);
  v[0].t = v[1].t = replay_ns(5);
  v[0].order = v[1].order = OrderKey{0, 9, 0};
  v[0].eid = "a";
  v[1].eid = "b";
  EXPECT_THROW(check_total_order(v), EvidenceError) << "an ambiguous order is never guessed";
}

TEST(Faults, MatchingNothingIsAnError) {
  TopicTable topics;
  auto ev = nominal_events(topics);
  std::vector<InjectionLog> log;
  EXPECT_THROW((void)apply_faults(ev, {parse_cli_fault("drop:topic=/nav/cmd_vel,from_s=99", 0)},
                                  topics, log),
               FaultError);
}

TEST(Faults, UnknownTopicKindAndParameterRefused) {
  EXPECT_THROW((void)parse_cli_fault("explode:topic=/x", 0), FaultError);
  EXPECT_THROW((void)parse_cli_fault("drop:topic=/x,bogus=1", 0), FaultError);
  EXPECT_THROW((void)parse_cli_fault("delay:topic=/x", 0), FaultError) << "delay_s required";
  EXPECT_THROW((void)parse_cli_fault("drop:topic=/x,every_n=1.5", 0), FaultError);
  TopicTable topics;
  auto ev = nominal_events(topics);
  std::vector<InjectionLog> log;
  EXPECT_THROW((void)apply_faults(ev, {parse_cli_fault("drop:topic=/nope", 0)}, topics, log),
               FaultError);
}

TEST(Faults, DuplicateCopiesAreOrderedAfterTheOriginal) {
  TopicTable topics;
  auto ev = nominal_events(topics);
  std::vector<InjectionLog> log;
  const auto out = apply_faults(
      ev, {parse_cli_fault("duplicate:topic=/helix/hold,from_s=1.0,to_s=2.0", 0)}, topics, log);
  ASSERT_EQ(log.size(), 1U);
  EXPECT_GT(log[0].events_affected, 0);
  EXPECT_EQ(out.size(), ev.size() + static_cast<std::size_t>(log[0].events_affected));
  for (const Event& e : out) {
    if (e.eid.find(".dup") != std::string::npos) {
      EXPECT_EQ(e.order.origin, 0);
      EXPECT_GE(e.order.b, 1);
      EXPECT_EQ(e.injected, std::vector<std::string>{"F1"});
    }
  }
  EXPECT_NO_THROW(check_total_order(out));
}

TEST(Faults, EveryTouchedEventCarriesTheFaultId) {
  TopicTable topics;
  auto ev = nominal_events(topics);
  std::vector<InjectionLog> log;
  const auto out =
      apply_faults(ev, {parse_cli_fault("clock_skew:host=payload,offset_s=1.5", 0)}, topics, log);
  std::int64_t tagged = 0;
  for (const Event& e : out) {
    tagged += e.injected.empty() ? 0 : 1;
  }
  EXPECT_EQ(tagged, log[0].events_affected);
}

TEST(Faults, DataFaultReDecodesTheTypedView) {
  TopicTable topics;
  auto ev = nominal_events(topics);
  std::vector<InjectionLog> log;
  const auto out = apply_faults(
      ev,
      {parse_cli_fault("malformed:topic=/nav/cmd_vel,field=linear.x,value=fast,from_s=3.0,to_s=3.2",
                       0)},
      topics, log);
  bool seen = false;
  for (const Event& e : out) {
    if (!e.injected.empty()) {
      const auto& v = std::get<VelocityCommand>(e.message()->typed);
      EXPECT_EQ(v.lx().state, NumState::malformed);
      EXPECT_EQ(v.forwarded[0], "fast");
      seen = true;
    }
  }
  EXPECT_TRUE(seen);
}

const Event& first_message_with_data(const std::vector<Event>& events) {
  for (const Event& e : events) {
    if (e.is_message() && e.msg().data != nullptr) {
      return e;
    }
  }
  throw std::logic_error("no message with data");
}

TEST(Event, PayloadIsAViewIntoTheImmutableRecord) {
  TopicTable topics;
  const auto ev = nominal_events(topics);
  std::size_t with_data = 0;
  for (const Event& e : ev) {
    if (!e.is_message() || e.msg().data == nullptr) {
      continue;
    }
    ++with_data;
    const Json& stored = e.record->at("data");
    EXPECT_EQ(e.msg().data.get(), &stored) << e.eid << ": no second copy of the payload";
    const Json copy = stored;
    EXPECT_EQ(canonical_json(*e.msg().data), canonical_json(copy));
  }
  EXPECT_GT(with_data, 100U);
}

TEST(Event, PayloadViewKeepsItsRecordAliveAndOutlivesTheEvent) {
  TopicTable topics;
  std::shared_ptr<const Json> data;
  std::weak_ptr<const Json> record;
  std::string expect;
  {
    const auto ev = nominal_events(topics);
    const Event& e = first_message_with_data(ev);
    data = e.msg().data;
    record = e.record;
    expect = canonical_json(*data);
  }  // every event, and every other owner of the record, is gone
  ASSERT_FALSE(record.expired()) << "the payload view owns its record";
  EXPECT_EQ(canonical_json(*data), expect);  // (ASan: no use after free)
  data.reset();
  EXPECT_TRUE(record.expired()) << "and nothing else keeps it";
}

TEST(Event, AFaultReplacesThePayloadAndNeverTheRecordedOne) {
  TopicTable topics;
  const auto ev = nominal_events(topics);
  std::map<std::string, std::string> before;  // eid -> record text
  for (const Event& e : ev) {
    before[e.eid] = canonical_json(*e.record);
  }
  std::vector<InjectionLog> log;
  const auto out = apply_faults(
      ev,
      {parse_cli_fault("malformed:topic=/nav/cmd_vel,field=linear.x,value=fast,from_s=3.0,to_s=3.2",
                       0)},
      topics, log);
  std::map<std::string, const Event*> original;
  for (const Event& e : ev) {
    original[e.eid] = &e;
  }
  std::size_t changed = 0;
  for (const Event& e : out) {
    if (e.injected.empty()) {
      continue;
    }
    ++changed;
    const Event& o = *original.at(e.eid);
    EXPECT_EQ(e.record, o.record) << "provenance: the record as stored";
    EXPECT_NE(e.msg().data.get(), &e.record->at("data")) << "a new payload, not the record's";
    EXPECT_EQ(e.msg().data->at("linear").at("x"), "fast");
    EXPECT_NE(o.msg().data->at("linear").at("x"), "fast") << "the input event is untouched";
    EXPECT_EQ(o.msg().data.get(), &o.record->at("data"));
  }
  EXPECT_GT(changed, 0U);
  for (const Event& e : ev) {
    EXPECT_EQ(canonical_json(*e.record), before.at(e.eid)) << e.eid << ": evidence unchanged";
  }
}

TEST(Faults, CliSyntaxMatchesPython) {
  const Fault f = parse_cli_fault("drop:topics=/a|/b,from_s=1,every_n=2", 0);
  EXPECT_EQ(f.id, "F1");
  EXPECT_EQ(f.list("topics"), (std::vector<std::string>{"/a", "/b"}));
  EXPECT_EQ(f.f64("from_s"), 1.0);
  EXPECT_EQ(f.i64("every_n"), 2);
  EXPECT_TRUE(f.json("to_s").is_null());
  const Fault j =
      parse_cli_fault(R"({"kind":"nan","topic":"/x","field":"linear.x","id":"custom"})", 3);
  EXPECT_EQ(j.id, "custom");
}

// --- arbitration models ------------------------------------------------------

Event msg_event(std::int64_t t_ns, std::int64_t seq, const std::string& topic, Role role,
                Json data) {
  Event e;
  e.t = replay_ns(t_ns);
  e.order = OrderKey{0, seq, 0};
  e.eid = "r" + std::to_string(seq);
  MessageBody m;
  m.topic = topic;
  m.role = role;
  m.role_name = std::string(role_name(role));
  m.rx_wall = wall_ns(t_ns);
  m.set_data(std::move(data));
  e.body = std::move(m);
  return e;
}

Json twist(double x) {
  return {{"linear", {{"x", x}, {"y", 0.0}, {"z", 0.0}}},
          {"angular", {{"x", 0.0}, {"y", 0.0}, {"z", 0.0}}}};
}
Json hold(bool h, int epoch, int seq) {
  return {{"hold", h}, {"epoch", epoch}, {"seq", seq}, {"fault_id", h ? "F" : ""}};
}

TEST(HelixModel, MissingHoldStateForcesZero) {
  ReferenceArbiter a(build_arbiter_config("helix_arbiter", Json::object()), 0);
  a.on_event(msg_event(0, 1, "/nav/cmd_vel", Role::cmd_vel_source, twist(0.2)), replay_ns(0));
  const Decision d = a.tick(replay_ns(20'000'000));
  EXPECT_EQ(d.reason, kReasonMissing);
  EXPECT_TRUE(d.robot_cmd->is_zero());
}

TEST(HelixModel, HoldDominatesTeleop) {
  ReferenceArbiter a(build_arbiter_config("helix_arbiter", Json::object()), 0);
  a.on_event(msg_event(0, 1, "/helix/hold", Role::helix_hold, hold(true, 1, 1)), replay_ns(0));
  a.on_event(msg_event(1, 2, "/teleop/cmd_vel", Role::cmd_vel_source, twist(0.4)), replay_ns(1));
  const Decision d = a.tick(replay_ns(20'000'000));
  EXPECT_EQ(d.reason, kReasonHold);
  EXPECT_TRUE(d.robot_cmd->is_zero());
}

TEST(HelixModel, StaleHoldForcesZeroAndOlderResumeIsIgnored) {
  ReferenceArbiter a(build_arbiter_config("helix_arbiter", Json::object()), 0);
  a.on_event(msg_event(0, 1, "/helix/hold", Role::helix_hold, hold(false, 1, 5)), replay_ns(0));
  a.on_event(msg_event(1, 2, "/nav/cmd_vel", Role::cmd_vel_source, twist(0.2)), replay_ns(1));
  EXPECT_EQ(a.tick(replay_ns(20'000'000)).reason, kReasonSource);
  // An older RESUME (lower seq) while the state is fresh is dropped (P8).
  a.on_event(msg_event(30'000'000, 3, "/helix/hold", Role::helix_hold, hold(false, 1, 4)),
             replay_ns(30'000'000));
  EXPECT_EQ(a.state()["counters"]["hold_reordered"], 1);
  // No hold message for longer than 0.5 s: stale, zero (P3).
  EXPECT_EQ(a.tick(replay_ns(600'000'000)).reason, kReasonStale);
}

TEST(HelixModel, NonFiniteInputRejectedAndOlderCommandDiscarded) {
  ReferenceArbiter a(build_arbiter_config("helix_arbiter", Json::object()), 0);
  a.on_event(msg_event(0, 1, "/helix/hold", Role::helix_hold, hold(false, 1, 1)), replay_ns(0));
  a.on_event(msg_event(1, 2, "/nav/cmd_vel", Role::cmd_vel_source, twist(0.2)), replay_ns(1));
  Json bad = twist(0.2);
  bad["linear"]["x"] = "NaN";
  a.on_event(msg_event(2, 3, "/nav/cmd_vel", Role::cmd_vel_source, bad), replay_ns(2));
  const Decision d = a.tick(replay_ns(20'000'000));
  EXPECT_EQ(d.reason, kReasonNoInput) << "P4: the older valid command is not a fallback";
  EXPECT_TRUE(d.robot_cmd->is_zero());
}

TEST(LegacyMux, ForwardsNaNAndKeepsTheLastCommandWhenInputsGoStale) {
  ReferenceArbiter a(build_arbiter_config("twist_mux_legacy", Json::object()), 0);
  a.on_event(msg_event(0, 1, "/nav/cmd_vel", Role::cmd_vel_source, twist(0.15)), replay_ns(0));
  ASSERT_TRUE(a.take_publication(replay_ns(0)).has_value());
  const Decision later = a.tick(replay_ns(900'000'000));
  EXPECT_EQ(later.reason, kReasonSilent);
  EXPECT_DOUBLE_EQ(later.robot_cmd->vx, 0.15) << "the sink keeps the last command";
  Json bad = twist(0.1);
  bad["linear"]["x"] = "NaN";
  a.on_event(msg_event(950'000'000, 2, "/nav/cmd_vel", Role::cmd_vel_source, bad),
             replay_ns(950'000'000));
  const auto pub = a.take_publication(replay_ns(950'000'000));
  ASSERT_TRUE(pub.has_value());
  EXPECT_FALSE(pub->robot_cmd.has_value()) << "not finite";
  EXPECT_EQ((*pub->robot_raw)[0], "NaN");
}

TEST(ArbiterConfig, OverridesValidated) {
  EXPECT_THROW((void)build_arbiter_config("nope", Json::object()), std::invalid_argument);
  EXPECT_THROW((void)build_arbiter_config("helix_arbiter", Json{{"stop_mode", "source"}}),
               std::invalid_argument)
      << "fixed semantics cannot be overridden";
  EXPECT_THROW((void)build_arbiter_config("helix_arbiter", Json{{"hold_timeout_s", 0}}),
               std::invalid_argument);
  EXPECT_THROW((void)build_arbiter_config("helix_arbiter", Json{{"freshness_clock", "wall"}}),
               std::invalid_argument);
  const auto c =
      build_arbiter_config("helix_arbiter", Json{{"freshness_clock", "source_timestamp"}});
  EXPECT_EQ(c.freshness_clock, FreshnessClock::source_timestamp);
}

}  // namespace
}  // namespace blackboxrs::replay
