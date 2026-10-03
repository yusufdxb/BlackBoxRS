// The introspection decoder must produce what the Python recorder's
// extract_fields / to_plain produce for the same message (expected values
// captured from blackboxrs/flight/records.py with rclpy, Humble).

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <functional>
#include <geometry_msgs/msg/twist.hpp>
#include <iostream>
#include <nav_msgs/msg/odometry.hpp>
#include <new>
#include <rclcpp/serialization.hpp>
#include <rclcpp/typesupport_helpers.hpp>
#include <rosidl_typesupport_introspection_cpp/field_types.hpp>
#include <rosidl_typesupport_introspection_cpp/message_introspection.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <set>
#include <std_msgs/msg/bool.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/u_int8_multi_array.hpp>

#include "blackboxrs/json.hpp"
#include "blackboxrs/profile.hpp"
#include "blackboxrs_ros/introspection_decoder.hpp"

namespace blackboxrs_ros {
namespace {
using blackboxrs::Json;
namespace ti = rosidl_typesupport_introspection_cpp;

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

// ---------------------------------------------------------------------------
// Field plans (resolved once per topic) against per-message name lookup
// ---------------------------------------------------------------------------

const ti::MessageMembers* nested(const ti::MessageMember& m) {
  return static_cast<const ti::MessageMembers*>(m.members_->data);
}

// A message of any type, built and filled through its introspection members.
// Every scalar gets a different value, so a plan that read the wrong member
// or the wrong offset would produce a different document.
class FilledMessage {
 public:
  explicit FilledMessage(const std::string& type) {
    lib_cpp_ = rclcpp::get_typesupport_library(type, "rosidl_typesupport_cpp");
    ts_cpp_ = rclcpp::get_typesupport_handle(type, "rosidl_typesupport_cpp", *lib_cpp_);
    lib_intro_ = rclcpp::get_typesupport_library(type, "rosidl_typesupport_introspection_cpp");
    const auto* ts =
        rclcpp::get_typesupport_handle(type, "rosidl_typesupport_introspection_cpp", *lib_intro_);
    members_ = static_cast<const ti::MessageMembers*>(ts->data);
    buf_ = ::operator new(members_->size_of_, kAlign);
    members_->init_function(buf_, rosidl_runtime_cpp::MessageInitialization::ALL);
    fill(*members_, buf_);
  }
  ~FilledMessage() {
    members_->fini_function(buf_);
    ::operator delete(buf_, kAlign);
  }
  FilledMessage(const FilledMessage&) = delete;
  FilledMessage& operator=(const FilledMessage&) = delete;
  FilledMessage(FilledMessage&&) = delete;
  FilledMessage& operator=(FilledMessage&&) = delete;

  [[nodiscard]] rclcpp::SerializedMessage serialize() const {
    rclcpp::SerializedMessage out;
    rclcpp::SerializationBase(ts_cpp_).serialize_message(buf_, &out);
    return out;
  }
  [[nodiscard]] const ti::MessageMembers& members() const { return *members_; }

 private:
  static constexpr std::align_val_t kAlign{alignof(std::max_align_t)};

  // Writes the next value into a scalar of type `type_id` at `p`.
  void scalar(std::uint8_t type_id, void* p) {
    const int k = ++counter_;
    switch (type_id) {
      case ti::ROS_TYPE_FLOAT: *static_cast<float*>(p) = 0.25F * static_cast<float>(k); break;
      case ti::ROS_TYPE_DOUBLE: *static_cast<double*>(p) = 0.5 * k + 0.125; break;
      case ti::ROS_TYPE_LONG_DOUBLE: *static_cast<long double*>(p) = 0.75L * k; break;
      case ti::ROS_TYPE_CHAR:
      case ti::ROS_TYPE_OCTET:
      case ti::ROS_TYPE_UINT8:
        *static_cast<std::uint8_t*>(p) = static_cast<std::uint8_t>(k % 251);
        break;
      case ti::ROS_TYPE_WCHAR:
        *static_cast<char16_t*>(p) = static_cast<char16_t>(0x41 + k % 26);
        break;
      case ti::ROS_TYPE_BOOLEAN: *static_cast<bool*>(p) = k % 2 == 0; break;
      case ti::ROS_TYPE_INT8:
        *static_cast<std::int8_t*>(p) = static_cast<std::int8_t>(-(k % 120));
        break;
      case ti::ROS_TYPE_UINT16:
        *static_cast<std::uint16_t*>(p) = static_cast<std::uint16_t>(k * 7);
        break;
      case ti::ROS_TYPE_INT16:
        *static_cast<std::int16_t*>(p) = static_cast<std::int16_t>(-k * 7);
        break;
      case ti::ROS_TYPE_UINT32:
        *static_cast<std::uint32_t*>(p) = static_cast<std::uint32_t>(k) * 1000U;
        break;
      case ti::ROS_TYPE_INT32: *static_cast<std::int32_t*>(p) = -k * 1000; break;
      case ti::ROS_TYPE_UINT64:
        *static_cast<std::uint64_t*>(p) = static_cast<std::uint64_t>(k) << 33U;
        break;
      case ti::ROS_TYPE_INT64:
        *static_cast<std::int64_t*>(p) = -(static_cast<std::int64_t>(k) << 33U);
        break;
      case ti::ROS_TYPE_STRING: *static_cast<std::string*>(p) = "s" + std::to_string(k); break;
      case ti::ROS_TYPE_WSTRING:
        *static_cast<std::u16string*>(p) =
            u"w" + std::u16string(1, static_cast<char16_t>(0x61 + k % 26));
        break;
      default: break;
    }
  }

  void fill(const ti::MessageMembers& mm, void* msg) {
    for (std::uint32_t i = 0; i < mm.member_count_; ++i) {
      const ti::MessageMember& m = mm.members_[i];
      void* field = static_cast<unsigned char*>(msg) + m.offset_;
      if (!m.is_array_) {
        if (m.type_id_ == ti::ROS_TYPE_MESSAGE) {
          fill(*nested(m), field);
        } else {
          scalar(m.type_id_, field);
        }
        continue;
      }
      const bool fixed = m.array_size_ != 0 && !m.is_upper_bound_;
      if (!fixed && m.resize_function != nullptr) {
        m.resize_function(field, m.is_upper_bound_ ? std::min<std::size_t>(3, m.array_size_) : 3);
      }
      const std::size_t n = m.size_function(field);
      for (std::size_t k = 0; k < n; ++k) {
        if (m.type_id_ == ti::ROS_TYPE_MESSAGE) {
          fill(*nested(m), m.get_function(field, k));
        } else if (m.type_id_ == ti::ROS_TYPE_STRING || m.type_id_ == ti::ROS_TYPE_WSTRING ||
                   m.assign_function == nullptr) {
          scalar(m.type_id_, m.get_function(field, k));
        } else {
          alignas(16) unsigned char value[16] = {};
          scalar(m.type_id_, value);
          m.assign_function(field, k, value);  // also right for std::vector<bool>
        }
      }
    }
  }

  std::shared_ptr<rcpputils::SharedLibrary> lib_cpp_;
  std::shared_ptr<rcpputils::SharedLibrary> lib_intro_;
  const rosidl_message_type_support_t* ts_cpp_ = nullptr;
  const ti::MessageMembers* members_ = nullptr;
  void* buf_ = nullptr;
  int counter_ = 0;
};

// Field extraction as the decoder did it before it planned fields: every
// field resolved by member name on every message, the leaf's value taken
// from the whole-message conversion (same per-member rules), stored with
// set_path, unresolvable fields reported in profile order.
Json reference_extract(const ti::MessageMembers& root, const Json& whole,
                       const std::vector<std::string>& fields, std::vector<std::string>& missing) {
  Json out = Json::object();
  for (const auto& f : fields) {
    std::vector<std::string> parts;
    for (std::size_t start = 0;;) {
      const auto dot = f.find('.', start);
      parts.push_back(f.substr(start, dot == std::string::npos ? std::string::npos : dot - start));
      if (dot == std::string::npos) {
        break;
      }
      start = dot + 1;
    }
    const ti::MessageMembers* mm = &root;
    const Json* value = &whole;
    bool found_all = false;
    for (std::size_t k = 0; k < parts.size(); ++k) {
      const ti::MessageMember* found = nullptr;
      for (std::uint32_t i = 0; i < mm->member_count_; ++i) {
        if (parts[k] == mm->members_[i].name_) {
          found = &mm->members_[i];
          break;
        }
      }
      if (found == nullptr) {
        break;
      }
      value = &value->at(parts[k]);
      if (k + 1 == parts.size()) {
        found_all = true;
        break;
      }
      if (found->type_id_ != ti::ROS_TYPE_MESSAGE || found->is_array_) {
        break;
      }
      mm = nested(*found);
    }
    if (found_all) {
      out = blackboxrs::set_path(std::move(out), f, *value);
    } else {
      missing.push_back(f);
    }
  }
  return out;
}

// Every member path of a type to `depth` levels, including paths through
// arrays and scalars (which no plan can resolve), plus malformed paths.
std::vector<std::string> probe_fields(const ti::MessageMembers& root) {
  std::vector<std::string> out{"", ".", "..", "nope", "nope.x", ".x", "x.", "x..y"};
  std::function<void(const ti::MessageMembers&, const std::string&, int)> walk =
      [&](const ti::MessageMembers& mm, const std::string& prefix, int depth) {
        for (std::uint32_t i = 0; i < mm.member_count_; ++i) {
          const ti::MessageMember& m = mm.members_[i];
          const std::string path = prefix + m.name_;
          out.push_back(path);
          if (m.type_id_ == ti::ROS_TYPE_MESSAGE && depth < 4) {
            walk(*nested(m), path + ".", depth + 1);
          } else {
            out.push_back(path + ".x");  // through a scalar or a scalar array
          }
        }
      };
  walk(root, "", 1);
  const std::string first = root.member_count_ != 0 ? root.members_[0].name_ : "x";
  for (const std::string& extra : {first + ".", "." + first, first + ".." + first, first}) {
    out.push_back(extra);  // trailing/leading/repeated dots, and a duplicate field
  }
  return out;
}

std::vector<std::filesystem::path> profile_files() {
  std::vector<std::filesystem::path> out;
  for (const auto& e : std::filesystem::directory_iterator(
           std::filesystem::path(BLACKBOXRS_REPO_ROOT) / "blackboxrs/flight/profiles")) {
    if (e.path().extension() == ".yaml") {
      out.push_back(e.path());
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

TEST(Decoder, PlannedFieldsMatchNameLookupForEveryAvailableProfileType) {
  std::set<std::string> covered;
  std::set<std::string> unavailable;
  std::size_t checked = 0;
  for (const auto& file : profile_files()) {
    const blackboxrs::Profile profile = blackboxrs::load_profile_file(file.string());
    ASSERT_FALSE(profile.topics.empty()) << file;
    for (const auto& topic : profile.topics) {
      {
        blackboxrs::Profile one;
        one.topics.push_back(topic);
        if (IntrospectionDecoder(one).unavailable(0)) {
          unavailable.insert(topic.type);
          continue;
        }
      }
      covered.insert(topic.type);
      const FilledMessage msg(topic.type);
      const rclcpp::SerializedMessage bytes = msg.serialize();
      const Json whole = decode_to_json(topic.type, bytes, {});
      // The profile's own fields, then every probe path.
      for (const auto& fields : {topic.fields, probe_fields(msg.members())}) {
        if (fields.empty()) {
          continue;
        }
        std::vector<std::string> want_missing;
        const Json want = reference_extract(msg.members(), whole, fields, want_missing);
        std::vector<std::string> got_missing;
        const Json got = decode_to_json(topic.type, bytes, fields, &got_missing);
        EXPECT_EQ(got, want) << file.filename() << " " << topic.name << " (" << topic.type << ")";
        EXPECT_EQ(got_missing, want_missing) << topic.name << " (" << topic.type << ")";
        ++checked;
      }
    }
  }
  std::cout << "field plans checked for " << covered.size() << " types (" << checked
            << " field lists); type support not installed here:";
  for (const auto& t : unavailable) {
    std::cout << " " << t;
  }
  std::cout << "\n";
  // The package's test dependencies guarantee these.
  for (const char* t : {"geometry_msgs/msg/Twist", "nav_msgs/msg/Odometry", "sensor_msgs/msg/Imu",
                        "sensor_msgs/msg/JointState", "std_msgs/msg/Float64MultiArray",
                        "std_msgs/msg/String", "std_msgs/msg/Bool"}) {
    EXPECT_EQ(covered.count(t), 1U) << t;
  }
}

TEST(Decoder, PlansAreReusedAcrossMessagesAndTopicsOfOneType) {
  // Two topics of one type with different fields, decoded repeatedly from
  // different messages: each message's own values, each topic's own fields.
  blackboxrs::Profile p;
  blackboxrs::TopicSpec a;
  a.name = "/odom_a";
  a.type = "nav_msgs/msg/Odometry";
  a.fields = {"header.stamp", "pose.pose.position.x", "nope"};
  blackboxrs::TopicSpec b = a;
  b.name = "/odom_b";
  b.fields = {"twist.twist.linear", "header.frame_id"};
  p.topics = {a, b};
  IntrospectionDecoder d(p);
  for (int i = 0; i < 5; ++i) {
    nav_msgs::msg::Odometry o;
    o.header.stamp.sec = i;
    o.header.frame_id = "f" + std::to_string(i);
    o.pose.pose.position.x = 0.5 * i;
    o.twist.twist.linear.y = -1.0 * i;
    auto bytes = std::make_shared<rclcpp::SerializedMessage>(serialize(o));
    const auto ra = d.decode(0, SerializedPayload(bytes));
    const auto rb = d.decode(1, SerializedPayload(bytes));
    ASSERT_TRUE(ra.data && rb.data);
    EXPECT_EQ((*ra.data)["header"]["stamp"]["sec"], i);
    EXPECT_EQ((*ra.data)["pose"]["pose"]["position"]["x"], 0.5 * i);
    EXPECT_FALSE(ra.data->contains("twist"));
    EXPECT_EQ(ra.missing, std::vector<std::string>{"nope"});
    EXPECT_EQ((*rb.data)["twist"]["twist"]["linear"]["y"], -1.0 * i);
    EXPECT_EQ((*rb.data)["header"]["frame_id"], "f" + std::to_string(i));
    EXPECT_FALSE((*rb.data)["header"].contains("stamp"));
    EXPECT_TRUE(rb.missing.empty());
  }
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
