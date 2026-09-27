#include <gtest/gtest.h>

#include <cmath>

#include "blackboxrs/json.hpp"

namespace blackboxrs {
namespace {

TEST(AsNumber, MatchesPythonRules) {
  const Json one = 1;
  const Json flt = 0.15;
  const Json t = true;
  const Json nan_s = "NaN";
  const Json inf_s = "Infinity";
  const Json ninf_s = "-Infinity";
  const Json word = "fast";
  const Json nul = nullptr;
  const Json obj = Json::object();
  EXPECT_EQ(as_number(&one).state, NumState::ok);
  EXPECT_DOUBLE_EQ(as_number(&one).value, 1.0);
  EXPECT_EQ(as_number(&flt).state, NumState::ok);
  EXPECT_EQ(as_number(&t).state, NumState::malformed) << "a bool is never a number";
  EXPECT_EQ(as_number(&nan_s).state, NumState::nonfinite);
  EXPECT_TRUE(std::isnan(as_number(&nan_s).value));
  EXPECT_EQ(as_number(&inf_s).value, INFINITY);
  EXPECT_EQ(as_number(&ninf_s).value, -INFINITY);
  EXPECT_EQ(as_number(&word).state, NumState::malformed);
  EXPECT_EQ(as_number(&nul).state, NumState::malformed);
  EXPECT_EQ(as_number(&obj).state, NumState::malformed);
  EXPECT_EQ(as_number(nullptr).state, NumState::malformed);
}

TEST(StrictInt, RejectsFloatsAndBools) {
  const Json i = 3;
  const Json f = 3.0;
  const Json b = true;
  EXPECT_TRUE(is_strict_int(&i));
  EXPECT_FALSE(is_strict_int(&f));
  EXPECT_FALSE(is_strict_int(&b));
  EXPECT_FALSE(is_strict_int(nullptr));
}

TEST(Paths, GetSetDelete) {
  Json d = Json::parse(R"({"linear":{"x":0.1,"y":0},"angular":{"z":0}})");
  ASSERT_NE(get_path(d, "linear.x"), nullptr);
  EXPECT_EQ(get_path(d, "linear.q"), nullptr);
  EXPECT_EQ(get_path(d, "linear.x.deeper"), nullptr) << "a number is not traversed";
  Json e = set_path(d, "linear.x", "fast");
  EXPECT_EQ(e["linear"]["x"], "fast");
  EXPECT_EQ(d["linear"]["x"], 0.1) << "set_path copies";
  Json f = set_path(d, "a.b.c", 1);
  EXPECT_EQ(f["a"]["b"]["c"], 1);
  Json g = del_path(d, "linear.x");
  EXPECT_FALSE(g["linear"].contains("x"));
  EXPECT_EQ(del_path(d, "nope.x"), d);
}

TEST(Canonical, SortedCompactAndNonFiniteAsStrings) {
  Json v = {{"b", 1}, {"a", {1.5, NAN, -INFINITY}}, {"c", "é"}};
  EXPECT_EQ(canonical_json(v), "{\"a\":[1.5,\"NaN\",\"-Infinity\"],\"b\":1,\"c\":\"\\u00e9\"}");
}

}  // namespace
}  // namespace blackboxrs
