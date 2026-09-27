#include <gtest/gtest.h>

#include "blackboxrs/runtime_config.hpp"
#include "test_support.hpp"

namespace blackboxrs {
namespace {

std::string write_config(const testing::TempDir& tmp, const std::string& body) {
  const auto path = tmp.path() / "cfg.yaml";
  testing::write_file(
      path, "schema: blackboxrs.runtime.v1\nprofile: " +
                (testing::repo_root() / "blackboxrs/flight/profiles/go2_helix.yaml").string() +
                "\n" + body);
  return path.string();
}

TEST(RuntimeConfig, HardwareCandidatesValidate) {
  for (const char* f : {"configs/go2_hardware.yaml", "configs/go2_hardware_stage_e.yaml"}) {
    const RuntimeConfig c = load_runtime_config((testing::repo_root() / f).string());
    EXPECT_EQ(c.profile.name, "go2_helix");
    EXPECT_EQ(c.profile.topic("/cmd_vel"), nullptr)
        << "HELIX preflight C6: /cmd_vel stays unsubscribed";
    EXPECT_EQ(c.sha256.size(), 64U);
  }
}

TEST(RuntimeConfig, DefaultsAreSane) {
  testing::TempDir tmp;
  const RuntimeConfig c = load_runtime_config(write_config(tmp, ""));
  EXPECT_EQ(c.capture_mode, CaptureMode::triggered);
  EXPECT_EQ(c.command_sources.size(), 2U) << "profile cmd_vel_source topics";
}

struct Bad {
  const char* body;
  const char* expect;
};

class RejectsInvalid : public ::testing::TestWithParam<Bad> {};

TEST_P(RejectsInvalid, NamesTheKey) {
  testing::TempDir tmp;
  try {
    (void)load_runtime_config(write_config(tmp, GetParam().body));
    FAIL() << "accepted: " << GetParam().body;
  } catch (const ConfigError& exc) {
    EXPECT_NE(std::string(exc.what()).find(GetParam().expect), std::string::npos) << exc.what();
  }
}

INSTANTIATE_TEST_SUITE_P(
    Config, RejectsInvalid,
    ::testing::Values(
        Bad{"bogus: 1\n", "unknown key bogus"}, Bad{"capture: {mode: sometimes}\n", "capture.mode"},
        Bad{"capture: {evidence: /tmp}\n", "unknown key capture.evidence"},
        Bad{"queues: {ingest_capacity: 10}\n", "queues.ingest_capacity"},
        Bad{"queues: {ingest_capacity: 100, control_reserve: 100}\n", "control_reserve"},
        Bad{"queues: {drain_deadline_s: -1}\n", "queues.drain_deadline_s"},
        Bad{"writer: {fsync_every_s: fast}\n", "writer.fsync_every_s"},
        Bad{"monitor: {stop_grace_s: 0.9}\n", "monitor.stop_grace_s"},
        Bad{"monitor: {command_sources: {/not/in/profile: 0.5}}\n", "not a profile topic"},
        Bad{"safety: {forbidden_publish_topics: [relative]}\n", "fully qualified"},
        Bad{"ros: {namespace: nope}\n", "ros.namespace"},
        Bad{"capture: {session_id: a/b}\n", "session_id"}));

TEST(RuntimeConfig, MissingProfileAndSchemaRefused) {
  testing::TempDir tmp;
  testing::write_file(tmp.path() / "a.yaml", "schema: blackboxrs.runtime.v1\n");
  EXPECT_THROW((void)load_runtime_config((tmp.path() / "a.yaml").string()), ConfigError);
  testing::write_file(tmp.path() / "b.yaml", "profile: x.yaml\n");
  EXPECT_THROW((void)load_runtime_config((tmp.path() / "b.yaml").string()), ConfigError);
  testing::write_file(tmp.path() / "c.yaml",
                      "schema: blackboxrs.runtime.v1\nprofile: missing.yaml\n");
  EXPECT_THROW((void)load_runtime_config((tmp.path() / "c.yaml").string()), ConfigError);
}

TEST(PublishGuard, MotionTopicsAreAlwaysForbidden) {
  testing::TempDir tmp;
  const RuntimeConfig c =
      load_runtime_config(write_config(tmp, "safety: {forbidden_publish_topics: [/extra/stop]}\n"));
  std::string why;
  for (const auto& t : builtin_forbidden_topics()) {
    EXPECT_FALSE(publish_allowed(c, t, &why)) << t;
  }
  EXPECT_FALSE(publish_allowed(c, "/extra/stop"));
  EXPECT_FALSE(publish_allowed(c, "/api/anything")) << "vendor API prefix";
  EXPECT_FALSE(publish_allowed(c, "/helix_dry/cmd_vel"));
  EXPECT_FALSE(publish_allowed(c, "/some/other/topic", &why));
  EXPECT_NE(why.find("allowlist"), std::string::npos);
  EXPECT_TRUE(publish_allowed(c, "/diagnostics"));
  EXPECT_TRUE(publish_allowed(c, "/blackboxrs/status"));
  EXPECT_TRUE(publish_allowed(c, "/blackboxrs/replay/timeline"));
}

}  // namespace
}  // namespace blackboxrs
