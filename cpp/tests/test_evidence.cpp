#include <gtest/gtest.h>

#include "blackboxrs/event.hpp"
#include "blackboxrs/evidence/bundle.hpp"
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
