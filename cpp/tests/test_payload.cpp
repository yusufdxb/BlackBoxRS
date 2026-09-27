#include <gtest/gtest.h>

#include "blackboxrs/payload.hpp"

namespace blackboxrs {
namespace {

TEST(Payload, TwistKeepsForwardedValuesAndStates) {
  const Json d = Json::parse(R"({"linear":{"x":"fast","y":"NaN","z":0},"angular":{"x":0,"y":0}})");
  const auto v = std::get<VelocityCommand>(decode_payload(Role::cmd_vel_source, d));
  EXPECT_EQ(v.lx().state, NumState::malformed);
  EXPECT_EQ(v.ly().state, NumState::nonfinite);
  EXPECT_EQ(v.az().state, NumState::malformed) << "angular.z missing";
  EXPECT_EQ(v.forwarded[0], "fast");
  EXPECT_TRUE(v.forwarded[2].is_null());
}

TEST(Payload, HoldFlagIsBoolOrNot) {
  const auto h = std::get<HoldState>(
      decode_payload(Role::helix_hold, Json::parse(R"({"hold":1,"epoch":2,"seq":3.0})")));
  EXPECT_EQ(h.hold, Flag::other_truthy) << "1 is truthy but not a bool";
  EXPECT_FALSE(flag_is_bool(h.hold));
  EXPECT_TRUE(flag_truthy(h.hold));
  EXPECT_EQ(h.epoch.value, 2);
  EXPECT_FALSE(h.seq.value.has_value()) << "3.0 is not an int";
  EXPECT_FALSE(h.fault_id.present);
}

TEST(Payload, TextFieldFollowsPythonStr) {
  const Json n = nullptr;
  const Json b = false;
  EXPECT_EQ(read_text(&n).text, "None");
  EXPECT_EQ(read_text(&b).text, "False");
  const Json s = "F12";
  EXPECT_TRUE(read_text(&s).is_string);
}

TEST(Payload, Truthiness) {
  EXPECT_FALSE(json_truthy(Json(0)));
  EXPECT_FALSE(json_truthy(Json("")));
  EXPECT_FALSE(json_truthy(Json::object()));
  EXPECT_TRUE(json_truthy(Json("x")));
  EXPECT_TRUE(json_truthy(Json(0.5)));
}

TEST(Payload, OtherRolesAreOpaque) {
  EXPECT_TRUE(std::holds_alternative<OpaquePayload>(
      decode_payload(Role::go2_state, Json::parse(R"({"tick":1})"))));
}

}  // namespace
}  // namespace blackboxrs
