#include <gtest/gtest.h>

#include "blackboxrs/event.hpp"
#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/integrity.hpp"
#include "blackboxrs/profile.hpp"
#include "blackboxrs/replay/monitors.hpp"
#include "test_support.hpp"

namespace blackboxrs {
namespace {
using testing::TempDir;

TEST(Evidence, LoadsGoldenBundle) {
  const Evidence ev = load_evidence(testing::golden_evidence("clean_stop"), false);
  EXPECT_EQ(ev.records.size(), 688U);
  EXPECT_TRUE(ev.synthetic);
  EXPECT_FALSE(ev.partial);
  EXPECT_EQ(ev.profile.name, "replay_lab_fixture");
  EXPECT_EQ(count_ns(ev.t0), 5'000'000'000'000);
  EXPECT_EQ(ev.digest.size(), 64U);
  for (std::size_t i = 1; i < ev.records.size(); ++i) {
    ASSERT_LT(ev.records[i - 1]["seq"].get<std::int64_t>(),
              ev.records[i]["seq"].get<std::int64_t>());
  }
}

TEST(Evidence, DigestIsStableAcrossLoads) {
  const auto a = load_evidence(testing::golden_evidence("nominal_motion"), false);
  const auto b = load_evidence(testing::golden_evidence("nominal_motion"), false);
  EXPECT_EQ(a.digest, b.digest);
}

TEST(Evidence, TornLineRefusedUnlessPartialAllowed) {
  TempDir tmp;
  const auto dir = tmp.path() / "b";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  std::string recs = testing::read_file(dir / "records.jsonl");
  recs += R"({"kind":"msg","seq":99999,"t_mono)";  // killed mid-write
  testing::write_file(dir / "records.jsonl", recs);
  EXPECT_THROW((void)load_evidence(dir, false), EvidenceError);
  const Evidence ev = load_evidence(dir, true);
  EXPECT_TRUE(ev.partial);
  ASSERT_EQ(ev.problems.size(), 1U);
  EXPECT_EQ(ev.problems[0], "1 torn record line(s)");
}

TEST(Evidence, UnfinalizedPartialDirectoryIsIncomplete) {
  TempDir tmp;
  const auto dir = tmp.path() / "inc_x.partial";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  Json m = Json::parse(testing::read_file(dir / "manifest.json"));
  m["status"] = "capturing";
  testing::write_file(dir / "manifest.json", m.dump());
  const BundleRead b = load_bundle(dir);
  EXPECT_TRUE(b.info.partial_dir);
  EXPECT_EQ(b.manifest["status"], "interrupted_unfinalized");
  EXPECT_THROW((void)load_evidence(dir, false), EvidenceError);
}

class IncompleteStatus : public ::testing::TestWithParam<std::string> {};

TEST_P(IncompleteStatus, IsRefusedUnlessPartialAllowed) {
  TempDir tmp;
  const auto dir = tmp.path() / "b";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  Json m = Json::parse(testing::read_file(dir / "manifest.json"));
  m["status"] = GetParam();
  testing::write_file(dir / "manifest.json", m.dump());
  EXPECT_THROW((void)load_evidence(dir, false), EvidenceError);
  const Evidence ev = load_evidence(dir, true);
  EXPECT_TRUE(ev.partial);
  EXPECT_EQ(ev.problems.at(0), "bundle status is '" + GetParam() + "'");
}

INSTANTIATE_TEST_SUITE_P(Evidence, IncompleteStatus,
                         ::testing::Values("write_failed", "complete_with_loss", "interrupted",
                                           "pipeline_failed"));

// The golden bundle plus an integrity.json that matches it.
std::filesystem::path with_integrity(const std::filesystem::path& root, bool complete) {
  const auto dir = root / "b";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  const std::string data = testing::read_file(dir / "records.jsonl");
  Sha256 h;
  h.update(data);
  const Json ir = {{"schema", "blackboxrs.integrity.v1"},
                   {"records", std::count(data.begin(), data.end(), '\n')},
                   {"bytes", data.size()},
                   {"sha256", h.finish_hex()},
                   {"chunks", Json::array()},
                   {"complete", complete}};
  testing::write_file(dir / "integrity.json", ir.dump());
  return dir;
}

TEST(Evidence, MatchingIntegrityRecordIsAccepted) {
  TempDir tmp;
  EXPECT_FALSE(load_evidence(with_integrity(tmp.path(), true), false).partial);
}

TEST(Evidence, RecordsThatDisagreeWithIntegrityAreRefused) {
  TempDir tmp;
  const auto dir = with_integrity(tmp.path(), true);
  std::string data = testing::read_file(dir / "records.jsonl");
  data[data.find("\"linear\"") + 1] = 'L';  // still valid JSON, same length
  testing::write_file(dir / "records.jsonl", data);
  EXPECT_THROW((void)load_evidence(dir, false), EvidenceError);
  EXPECT_EQ(load_evidence(dir, true).problems,
            std::vector<std::string>{"records.jsonl sha256 does not match integrity.json"});
  const auto lines = static_cast<std::size_t>(std::count(data.begin(), data.end(), '\n'));
  testing::write_file(dir / "records.jsonl", data + "\n");
  EXPECT_EQ(
      load_evidence(dir, true).problems,
      (std::vector<std::string>{"records.jsonl is " + std::to_string(data.size() + 1) +
                                    " bytes, integrity.json says " + std::to_string(data.size()),
                                "records.jsonl has " + std::to_string(lines + 1) +
                                    " lines, integrity.json says " + std::to_string(lines),
                                "records.jsonl sha256 does not match integrity.json"}));
}

TEST(Evidence, IntegrityMarkedIncompleteIsRefused) {
  TempDir tmp;
  const auto dir = with_integrity(tmp.path(), false);
  EXPECT_THROW((void)load_evidence(dir, false), EvidenceError);
  EXPECT_EQ(load_evidence(dir, true).problems.at(0), "integrity.json marks the capture incomplete");
}

class BadWindow : public ::testing::TestWithParam<std::pair<std::string, std::string>> {};

TEST_P(BadWindow, IsRefused) {
  const std::string text = "profile: t\nbuffer: {post_trigger_sec: " + GetParam().first +
                           "}\ntopics:\n  - {name: /x, type: a/msg/B}\n";
  try {
    (void)parse_profile_text(text);
    FAIL() << "accepted post_trigger_sec " << GetParam().first;
  } catch (const ProfileError& exc) {
    EXPECT_NE(std::string(exc.what()).find(GetParam().second), std::string::npos) << exc.what();
  }
}

INSTANTIATE_TEST_SUITE_P(Profile, BadWindow,
                         ::testing::Values(std::pair{".nan", "finite"}, std::pair{".inf", "finite"},
                                           std::pair{"1e10", "<= 1000000000.0"},
                                           std::pair{"-1", "> 0"}));

TEST(Profile, TriggerRolesCannotBeDecimated) {
  for (const char* role : {"helix_hold", "recovery_action", "arbiter_status"}) {
    const std::string text =
        std::string("profile: t\ntopics:\n  - {name: /x, type: a/msg/B, role: ") + role +
        ", store_max_hz: 5}\n";
    EXPECT_THROW((void)parse_profile_text(text), ProfileError) << role;
  }
}

TEST(StopDominance, ADecisionWithoutOutputIsNotACheck) {
  replay::StopDominance m({"/helix/hold"}, {"/nav/cmd_vel"}, 0.0, 0.5);
  Event e;
  e.t = replay_ns(1'000'000'000);
  e.order = OrderKey{0, 1, 0};
  e.eid = "r1";
  MessageBody b;
  b.topic = "/helix/hold";
  b.role = Role::helix_hold;
  b.role_name = "helix_hold";
  b.type = "helix_msgs/msg/HelixHold";
  b.set_data({{"hold", true}, {"epoch", 1}, {"seq", 1}, {"fault_id", "f"}});
  e.body = std::move(b);
  replay::Findings out;
  m.on_event(e, out);
  replay::Decision d;
  d.t = replay_ns(1'100'000'000);
  m.on_decision(d, out);  // robot_raw empty: nothing published yet
  EXPECT_EQ(m.invariants().front()->checks, 0);
  m.finish(replay_ns(2'000'000'000), out);
  EXPECT_EQ(m.invariants().front()->status(), replay::InvariantStatus::incomplete);
  EXPECT_TRUE(out.empty());
}

TEST(Evidence, DuplicateSeqRefused) {
  std::vector<Json> recs = {
      Json::parse(
          R"({"kind":"msg","seq":1,"t_mono_ns":1,"t_wall_ns":1,"topic":"/a","role":"other","type":"a/msg/B","data":{}})"),
      Json::parse(
          R"({"kind":"msg","seq":1,"t_mono_ns":2,"t_wall_ns":2,"topic":"/a","role":"other","type":"a/msg/B","data":{}})")};
  EXPECT_THROW((void)validate_records(recs), EvidenceError);
}

TEST(Evidence, RecordsWithoutClocksRefused) {
  std::vector<Json> recs = {Json::parse(
      R"({"kind":"msg","seq":1,"t_wall_ns":1,"topic":"/a","role":"other","type":"a/msg/B"})")};
  EXPECT_THROW((void)validate_records(recs), EvidenceError);
}

TEST(Evidence, NoMessageRecordsRefused) {
  std::vector<Json> recs = {Json::parse(R"({"kind":"sys","seq":1,"t_mono_ns":1,"t_wall_ns":1})")};
  EXPECT_THROW((void)validate_records(recs), EvidenceError);
}

TEST(Evidence, MissingManifestReported) {
  TempDir tmp;
  const auto dir = tmp.path() / "b";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  std::filesystem::remove(dir / "manifest.json");
  const BundleRead b = load_bundle(dir);
  EXPECT_TRUE(b.info.manifest_missing);
  EXPECT_THROW((void)load_evidence(dir, true), EvidenceError) << "no embedded profile";
}

TEST(Evidence, UnknownManifestSchemaRefused) {
  TempDir tmp;
  const auto dir = tmp.path() / "b";
  testing::copy_dir(testing::golden_evidence("clean_stop"), dir);
  Json m = Json::parse(testing::read_file(dir / "manifest.json"));
  m["schema"] = "blackboxrs.flight.manifest.v9";
  testing::write_file(dir / "manifest.json", m.dump());
  try {
    (void)load_evidence(dir, true);
    FAIL() << "accepted an unknown schema";
  } catch (const EvidenceError& exc) {
    EXPECT_NE(std::string(exc.what()).find("unsupported evidence schema"), std::string::npos);
  }
}

TEST(Events, EqualTimestampsOrderBySeqThenCopyThenSynthesized) {
  // Three evidence events at one receipt time, a fault copy of the first and
  // a fault-synthesized event, inserted in scrambled order.
  auto ev = [](std::int64_t seq, std::int32_t origin, std::int64_t b, const char* id) {
    Event e;
    e.t = replay_ns(1'000);
    e.order = OrderKey{origin, seq, b};
    e.eid = id;
    e.body = SysBody{};
    return e;
  };
  std::vector<Event> v{ev(0, 1, 0, "F1.0"), ev(7, 0, 0, "r7"), ev(3, 0, 1, "r3.dup1"),
                       ev(3, 0, 0, "r3"), ev(5, 0, 0, "r5")};
  std::sort(v.begin(), v.end());
  std::vector<std::string> ids;
  for (const auto& e : v) {
    ids.push_back(e.eid);
  }
  EXPECT_EQ(ids, (std::vector<std::string>{"r3", "r3.dup1", "r5", "r7", "F1.0"}));
}

TEST(Events, FromRecordKeepsClockDomainsApart) {
  const Json r = Json::parse(R"({"kind":"msg","topic":"/nav/cmd_vel","role":"cmd_vel_source",
    "type":"geometry_msgs/msg/Twist","t_mono_ns":5000001371498,"t_wall_ns":1789000000001371392,
    "t_ros_ns":1789000000001371392,"dds_src_ns":1789000000000000000,"dds_rx_ns":1789000000000400128,
    "pub_stamp_s":null,"pub_stamp_domain":null,
    "data":{"linear":{"x":0.15,"y":0.0,"z":0.0},"angular":{"x":0.0,"y":0.0,"z":0.0}},"seq":3})");
  const Event e = event_from_record(r, mono_ns(5'000'000'000'000));
  EXPECT_EQ(e.t_ns(), 1'371'498);
  EXPECT_EQ(e.eid, "r3");
  const MessageBody* m = e.message();
  ASSERT_NE(m, nullptr);
  EXPECT_EQ(m->role, Role::cmd_vel_source);
  ASSERT_TRUE(m->src.has_value());
  EXPECT_EQ(count_ns(*m->src), 1'789'000'000'000'000'000);
  EXPECT_EQ(count_ns(*m->rx_wall), 1'789'000'000'000'400'128);
  EXPECT_FALSE(m->pub_stamp_s.has_value());
  const auto* v = std::get_if<VelocityCommand>(&m->typed);
  ASSERT_NE(v, nullptr);
  EXPECT_DOUBLE_EQ(v->lx().value, 0.15);
}

TEST(Events, ReceiptFallsBackToCallbackWallTime) {
  const Json r = Json::parse(R"({"kind":"msg","topic":"/x","role":"other","type":"a/msg/B",
    "t_mono_ns":10,"t_wall_ns":77,"dds_src_ns":0,"dds_rx_ns":null,"seq":1,"data":null})");
  const Event e = event_from_record(r, mono_ns(0));
  EXPECT_FALSE(e.message()->src.has_value()) << "a zero source stamp is absent, not epoch";
  EXPECT_EQ(count_ns(*e.message()->rx_wall), 77);
  EXPECT_EQ(e.message()->data, nullptr);
}

}  // namespace
}  // namespace blackboxrs
