// The C++ helix_arbiter model against the real HELIX arbiter.
//
// examples/replay_lab/arbiter_parity/helix_arbiter_core.json holds input
// streams and the decisions HELIX's own arbiter_core.Arbiter made for them
// (scripts/cpp/make_arbiter_traces.py, pinned by the SHA-256 of
// arbiter_core.py). Every tick must match on reason, winner and command.

#include <gtest/gtest.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "blackboxrs/replay/sut.hpp"
#include "test_support.hpp"

namespace blackboxrs::replay {
namespace {

// BLACKBOXRS_ARBITER_TRACES overrides the frozen file, so a live run against a
// HELIX checkout (tests/cpp/test_helix_arbiter_parity.py) uses this same test.
std::filesystem::path traces_path() {
  if (const char* p = std::getenv("BLACKBOXRS_ARBITER_TRACES"); p != nullptr && *p != '\0') {
    return p;
  }
  return testing::repo_root() / "examples/replay_lab/arbiter_parity/helix_arbiter_core.json";
}

Json load_traces() {
  std::ifstream in(traces_path());
  std::stringstream ss;
  ss << in.rdbuf();
  return Json::parse(ss.str());
}

Event op_event(const Json& op, std::int64_t seq) {
  Event e;
  e.t = replay_ns(seconds_to_ns(op["t"].get<double>()));
  e.order = OrderKey{0, seq, 0};
  e.eid = "r" + std::to_string(seq);
  MessageBody m;
  Json data;
  if (op["op"] == "source") {
    m.topic = op["topic"].get<std::string>();
    m.role = Role::cmd_vel_source;
    const Json& l = op["linear"];
    const Json& a = op["angular"];
    data = {{"linear", {{"x", l[0]}, {"y", l[1]}, {"z", l[2]}}},
            {"angular", {{"x", a[0]}, {"y", a[1]}, {"z", a[2]}}}};
  } else {
    m.topic = "/helix/hold";
    m.role = Role::helix_hold;
    data = {{"hold", op["hold"]},
            {"fault_id", op["fault_id"]},
            {"epoch", op["epoch"]},
            {"seq", op["seq"]}};
  }
  m.role_name = std::string(role_name(m.role));
  m.rx_wall = wall_ns(count_ns(e.t));
  m.set_data(std::move(data));
  e.body = std::move(m);
  return e;
}

class HelixParity : public ::testing::TestWithParam<std::size_t> {};

TEST(HelixParityFile, IsPinnedToAHelixBuild) {
  const Json doc = load_traces();
  EXPECT_EQ(doc["helix_arbiter_core"]["sha256"].get<std::string>().size(), 64U);
  EXPECT_GE(doc["streams"].size(), 30U);
  // The model's configuration is HELIX's arbiter.yaml, which the traces record.
  const ArbiterConfig cfg = build_arbiter_config("helix_arbiter", Json::object());
  EXPECT_EQ(doc["config"]["hold_timeout_s"].get<double>(), cfg.hold_timeout_s);
  EXPECT_EQ(doc["config"]["max_abs_linear"].get<double>(), cfg.max_abs_linear);
  EXPECT_EQ(doc["config"]["max_abs_angular"].get<double>(), cfg.max_abs_angular);
  ASSERT_EQ(doc["config"]["sources"].size(), cfg.sources.size());
  for (std::size_t i = 0; i < cfg.sources.size(); ++i) {
    EXPECT_EQ(doc["config"]["sources"][i]["topic"], cfg.sources[i].topic);
    EXPECT_EQ(doc["config"]["sources"][i]["priority"], cfg.sources[i].priority);
    EXPECT_EQ(doc["config"]["sources"][i]["timeout_s"].get<double>(), cfg.sources[i].timeout_s);
  }
}

TEST_P(HelixParity, EveryDecisionMatchesHelix) {
  static const Json doc = load_traces();
  const Json& stream = doc["streams"][GetParam()];
  ReferenceArbiter model(build_arbiter_config("helix_arbiter", Json::object()), 0);
  std::size_t k = 0;
  std::int64_t seq = 0;
  std::size_t mismatches = 0;
  for (const Json& op : stream["ops"]) {
    if (op["op"] == "tick") {
      const Decision d = model.tick(replay_ns(seconds_to_ns(op["t"].get<double>())));
      const Json& want = stream["decisions"][k++];
      const Json got_cmd = Json::array({d.robot_cmd->vx, d.robot_cmd->vy, d.robot_cmd->wz});
      if (d.reason != want["reason"] || d.source != want["source"] || got_cmd != want["command"]) {
        if (++mismatches <= 5) {
          ADD_FAILURE() << stream["name"].get<std::string>() << " t=" << op["t"] << ": model "
                        << d.reason << "/" << d.source << " " << got_cmd << ", HELIX "
                        << want["reason"] << "/" << want["source"] << " " << want["command"];
        }
      }
    } else {
      model.on_event(op_event(op, ++seq), replay_ns(seconds_to_ns(op["t"].get<double>())));
    }
  }
  EXPECT_EQ(k, stream["decisions"].size());
  EXPECT_EQ(mismatches, 0U) << stream["name"];
}

std::vector<std::size_t> stream_indices() {
  std::vector<std::size_t> v(load_traces()["streams"].size());
  for (std::size_t i = 0; i < v.size(); ++i) {
    v[i] = i;
  }
  return v;
}

INSTANTIATE_TEST_SUITE_P(Streams, HelixParity, ::testing::ValuesIn(stream_indices()));

}  // namespace
}  // namespace blackboxrs::replay
