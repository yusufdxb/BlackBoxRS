// The introspection decoder must produce what the Python recorder's
// extract_fields / to_plain produce for the same message (expected values
// captured from blackboxrs/flight/records.py with rclpy, Humble).

#include <gtest/gtest.h>

#include <cmath>
#include <geometry_msgs/msg/twist.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <rclcpp/serialization.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/bool.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/u_int8_multi_array.hpp>

#include "blackboxrs_ros/introspection_decoder.hpp"

namespace blackboxrs_ros {
namespace {
using blackboxrs::Json;

template <class T>
rclcpp::SerializedMessage serialize(const T& msg) {
  rclcpp::SerializedMessage out;
  rclcpp::Serialization<T>().serialize_message(&msg, &out);
  return out;
}

TEST(Decoder, TwistWholeMessageWithNaN) {
  geometry_msgs::msg::Twist t;
  t.linear.x = 0.15;
  t.angular.z = std::nan("");
  const Json got = decode_to_json("geometry_msgs/msg/Twist", serialize(t), {});
  EXPECT_EQ(
      got,
      Json::parse(
          R"({"linear": {"x": 0.15, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": "NaN"}})"));
}

TEST(Decoder, OdometryProfileFieldsAndMissingField) {
  nav_msgs::msg::Odometry o;
  o.header.stamp.sec = 5;
  o.header.stamp.nanosec = 7;
  o.header.frame_id = "odom";
  o.pose.pose.position.x = 1.5;
  std::vector<std::string> missing;
  const Json got = decode_to_json(
      "nav_msgs/msg/Odometry", serialize(o),
      {"header.stamp", "header.frame_id", "pose.pose.position", "twist.twist.linear", "nope.x"},
      &missing);
  EXPECT_EQ(
      got,
      Json::parse(
          R"({"header": {"stamp": {"sec": 5, "nanosec": 7}, "frame_id": "odom"}, "pose": {"pose": {"position": {"x": 1.5, "y": 0.0, "z": 0.0}}}, "twist": {"twist": {"linear": {"x": 0.0, "y": 0.0, "z": 0.0}}}})"));
  EXPECT_EQ(missing, std::vector<std::string>{"nope.x"});
}

TEST(Decoder, Sequences) {
  std_msgs::msg::Float64MultiArray f;
  f.data = {1.0, 2.5};
  EXPECT_EQ(decode_to_json("std_msgs/msg/Float64MultiArray", serialize(f), {}),
            Json::parse(R"({"layout": {"dim": [], "data_offset": 0}, "data": [1.0, 2.5]})"));
  std_msgs::msg::UInt8MultiArray u;
  u.data = {1, 2, 255};
  EXPECT_EQ(decode_to_json("std_msgs/msg/UInt8MultiArray", serialize(u), {}),
            Json::parse(R"({"layout": {"dim": [], "data_offset": 0}, "data": [1, 2, 255]})"));
}

TEST(Decoder, FixedArrayAndBool) {
  sensor_msgs::msg::Imu i;
  EXPECT_EQ(
      decode_to_json("sensor_msgs/msg/Imu", serialize(i), {"orientation_covariance"}),
      Json::parse(R"({"orientation_covariance": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})"));
  std_msgs::msg::Bool b;
  b.data = true;
  EXPECT_EQ(decode_to_json("std_msgs/msg/Bool", serialize(b), {}),
            Json::parse(R"({"data": true})"));
}

TEST(Decoder, LongSequencesAreTruncatedLikePython) {
  sensor_msgs::msg::JointState j;
  j.name = {"a", "b"};
  j.position.assign(300, 0.1);
  const Json got = decode_to_json("sensor_msgs/msg/JointState", serialize(j), {"name", "position"});
  EXPECT_EQ(got["name"], Json::parse(R"(["a", "b"])"));
  EXPECT_EQ(got["position"]["len"], 300);
  EXPECT_EQ(got["position"]["head"].size(), 256U);
  EXPECT_EQ(got["position"]["truncated"], true);
}

TEST(Decoder, UnknownTypeIsReportedNotFatal) {
  blackboxrs::Profile p;
  blackboxrs::TopicSpec spec;
  spec.name = "/x";
  spec.type = "not_a_pkg/msg/Nope";
  p.topics.push_back(spec);
  IntrospectionDecoder d(p);
  EXPECT_TRUE(d.unavailable(0).has_value());
}

}  // namespace
}  // namespace blackboxrs_ros
