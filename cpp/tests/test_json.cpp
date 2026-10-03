#include <gtest/gtest.h>

#include <cmath>
#include <string>
#include <vector>

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

TEST(Paths, EmptySegmentsLookUpTheEmptyKeyLikePythonSplit) {
  // Python: "".split(".") == [""], ".a".split(".") == ["", "a"],
  // "a.".split(".") == ["a", ""], "a..b".split(".") == ["a", "", "b"].
  const Json d = Json::parse(
      R"({"": {"a": 1, "": {"": 2}}, "a": {"": 3, "b": {"c": 4}}, "n": 5, "arr": [{"x": 1}]})");
  ASSERT_NE(get_path(d, ""), nullptr);
  EXPECT_EQ(get_path(d, ""), &d[""]);
  EXPECT_EQ(*get_path(d, ".a"), 1);
  EXPECT_EQ(*get_path(d, "a."), 3);
  EXPECT_EQ(*get_path(d, ".."), 2) << "three empty segments";
  EXPECT_EQ(*get_path(d, "a.b.c"), 4);
  EXPECT_EQ(get_path(d, "a..b"), nullptr) << "a[''] is a number, not traversed";
  EXPECT_EQ(get_path(d, "a.b.c."), nullptr) << "trailing dot past a leaf";
  EXPECT_EQ(get_path(d, "n.x"), nullptr) << "a number is not traversed";
  EXPECT_EQ(get_path(d, "arr.0"), nullptr) << "an array is not traversed";
  EXPECT_EQ(get_path(d, "arr.0.x"), nullptr);
  EXPECT_EQ(get_path(d, "missing"), nullptr);
  EXPECT_EQ(get_path(d, "a.missing"), nullptr);
  EXPECT_EQ(get_path(d, "a.b.missing"), nullptr);
  const Json no_empty = Json::parse(R"({"a": {"b": 1}})");
  EXPECT_EQ(get_path(no_empty, ""), nullptr);
  EXPECT_EQ(get_path(no_empty, ".a"), nullptr);
  EXPECT_EQ(get_path(no_empty, "a."), nullptr);
  EXPECT_EQ(get_path(no_empty, "a..b"), nullptr);
  EXPECT_EQ(get_path(Json(5), ""), nullptr) << "a non-object root is never traversed";
  EXPECT_EQ(get_path(Json::array({1}), "0"), nullptr);
  EXPECT_EQ(get_path(Json(), "a"), nullptr);
}

TEST(Paths, GetPathMatchesTheSplitThenWalkReference) {
  // The lookup walks segments in place; it must find exactly what splitting
  // the path first and then walking finds.
  auto reference = [](const Json& data, std::string_view dotted) -> const Json* {
    std::vector<std::string> parts;
    std::size_t start = 0;
    while (true) {
      const std::size_t dot = dotted.find('.', start);
      parts.emplace_back(dotted.substr(
          start, dot == std::string_view::npos ? std::string_view::npos : dot - start));
      if (dot == std::string_view::npos) {
        break;
      }
      start = dot + 1;
    }
    const Json* cur = &data;
    for (const auto& p : parts) {
      if (!cur->is_object() || !cur->contains(p)) {
        return nullptr;
      }
      cur = &(*cur)[p];
    }
    return cur;
  };
  const std::vector<Json> docs{
      Json::parse(R"({"linear":{"x":0.1,"y":"NaN"},"angular":{"z":null}})"),
      Json::parse(R"({"":{"":{"":0}},"a":{"":{"b":[1]}},"header":{"stamp":{"sec":1}}})"),
      Json::parse(R"({"pose":{"pose":{"position":{"x":1,"y":2}}},"twist":{"twist":{}}})"),
      Json::array({1, 2}),
      Json("text"),
      Json(),
  };
  const std::vector<std::string> paths{"",
                                       ".",
                                       "..",
                                       "...",
                                       "a",
                                       ".a",
                                       "a.",
                                       "a..b",
                                       "a..",
                                       "linear.x",
                                       "linear",
                                       "linear.x.y",
                                       "linear..x",
                                       "angular.z",
                                       "angular.z.w",
                                       "header",
                                       "header.stamp",
                                       "header.stamp.sec",
                                       "header.stamp.sec.",
                                       "pose.pose.position.x",
                                       "pose.pose.position.z",
                                       "twist.twist",
                                       "twist.twist.linear.x",
                                       "0",
                                       "a..b.0"};
  for (const Json& d : docs) {
    for (const auto& p : paths) {
      EXPECT_EQ(get_path(d, p), reference(d, p)) << "path '" << p << "' in " << d.dump();
    }
  }
}

TEST(Canonical, SortedCompactAndNonFiniteAsStrings) {
  Json v = {{"b", 1}, {"a", {1.5, NAN, -INFINITY}}, {"c", "é"}};
  EXPECT_EQ(canonical_json(v), "{\"a\":[1.5,\"NaN\",\"-Infinity\"],\"b\":1,\"c\":\"\\u00e9\"}");
}

}  // namespace
}  // namespace blackboxrs
