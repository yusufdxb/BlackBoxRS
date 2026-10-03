#include "blackboxrs_ros/introspection_decoder.hpp"

#include <rclcpp/serialization.hpp>
#include <rclcpp/typesupport_helpers.hpp>
#include <rosidl_typesupport_introspection_cpp/field_types.hpp>
#include <rosidl_typesupport_introspection_cpp/message_introspection.hpp>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <new>
#include <string_view>

namespace blackboxrs_ros {
namespace ti = rosidl_typesupport_introspection_cpp;
using blackboxrs::Json;

namespace {

constexpr std::size_t kMaxList = 256;  // Python records.MAX_LIST

Json number(double v) {
  return blackboxrs::number_or_nonfinite(v);
}

std::string u16_to_utf8(const std::u16string& s) {
  std::string out;
  for (char16_t c : s) {
    const auto cp = static_cast<std::uint32_t>(c);
    if (cp < 0x80U) {
      out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800U) {
      out.push_back(static_cast<char>(0xC0U | (cp >> 6U)));
      out.push_back(static_cast<char>(0x80U | (cp & 0x3FU)));
    } else {
      out.push_back(static_cast<char>(0xE0U | (cp >> 12U)));
      out.push_back(static_cast<char>(0x80U | ((cp >> 6U) & 0x3FU)));
      out.push_back(static_cast<char>(0x80U | (cp & 0x3FU)));
    }
  }
  return out;
}

const ti::MessageMembers* members_of(const ti::MessageMember& m) {
  return static_cast<const ti::MessageMembers*>(m.members_->data);
}

Json message_to_json(const ti::MessageMembers& mm, const void* msg);

// One element of type `m.type_id_` stored at `p`.
Json scalar_at(const ti::MessageMember& m, const void* p) {
  switch (m.type_id_) {
    case ti::ROS_TYPE_FLOAT: return number(static_cast<double>(*static_cast<const float*>(p)));
    case ti::ROS_TYPE_DOUBLE: return number(*static_cast<const double*>(p));
    case ti::ROS_TYPE_LONG_DOUBLE:
      return number(static_cast<double>(*static_cast<const long double*>(p)));
    case ti::ROS_TYPE_CHAR: return *static_cast<const unsigned char*>(p);
    case ti::ROS_TYPE_WCHAR: return static_cast<std::uint32_t>(*static_cast<const char16_t*>(p));
    case ti::ROS_TYPE_BOOLEAN: return *static_cast<const bool*>(p);
    case ti::ROS_TYPE_OCTET: return *static_cast<const unsigned char*>(p);
    case ti::ROS_TYPE_UINT8: return *static_cast<const std::uint8_t*>(p);
    case ti::ROS_TYPE_INT8: return *static_cast<const std::int8_t*>(p);
    case ti::ROS_TYPE_UINT16: return *static_cast<const std::uint16_t*>(p);
    case ti::ROS_TYPE_INT16: return *static_cast<const std::int16_t*>(p);
    case ti::ROS_TYPE_UINT32: return *static_cast<const std::uint32_t*>(p);
    case ti::ROS_TYPE_INT32: return *static_cast<const std::int32_t*>(p);
    case ti::ROS_TYPE_UINT64: return *static_cast<const std::uint64_t*>(p);
    case ti::ROS_TYPE_INT64: return *static_cast<const std::int64_t*>(p);
    case ti::ROS_TYPE_STRING: return *static_cast<const std::string*>(p);
    case ti::ROS_TYPE_WSTRING: return u16_to_utf8(*static_cast<const std::u16string*>(p));
    case ti::ROS_TYPE_MESSAGE: return message_to_json(*members_of(m), p);
    default: return nullptr;
  }
}

// Element i of an array field, read through the introspection accessors
// (fetch_function copies it out, which also works for std::vector<bool>).
Json element_at(const ti::MessageMember& m, const void* field, std::size_t i) {
  if (m.type_id_ == ti::ROS_TYPE_MESSAGE || m.type_id_ == ti::ROS_TYPE_STRING ||
      m.type_id_ == ti::ROS_TYPE_WSTRING) {
    return scalar_at(m, m.get_const_function(field, i));
  }
  if (m.fetch_function == nullptr) {
    return scalar_at(m, m.get_const_function(field, i));
  }
  alignas(16) unsigned char buf[16];
  m.fetch_function(field, i, buf);
  return scalar_at(m, buf);
}

Json field_to_json(const ti::MessageMember& m, const void* msg) {
  const void* field = static_cast<const unsigned char*>(msg) + m.offset_;
  if (!m.is_array_) {
    return scalar_at(m, field);
  }
  const std::size_t n = m.size_function(field);
  Json items = Json::array();
  items.get_ref<Json::array_t&>().reserve(std::min(n, kMaxList));
  for (std::size_t i = 0; i < n && i < kMaxList; ++i) {
    items.push_back(element_at(m, field, i));
  }
  if (n > kMaxList) {
    return Json{{"len", n}, {"head", std::move(items)}, {"truncated", true}};
  }
  return items;
}

Json message_to_json(const ti::MessageMembers& mm, const void* msg) {
  Json out = Json::object();
  for (std::uint32_t i = 0; i < mm.member_count_; ++i) {
    out[mm.members_[i].name_] = field_to_json(mm.members_[i], msg);
  }
  return out;
}

// The segments of a dotted path, as Python's str.split(".") gives them.
std::vector<std::string> split_dotted(const std::string& path) {
  std::vector<std::string> parts;
  std::size_t start = 0;
  while (true) {
    const auto dot = path.find('.', start);
    parts.push_back(path.substr(start, dot == std::string::npos ? std::string::npos : dot - start));
    if (dot == std::string::npos) {
      return parts;
    }
    start = dot + 1;
  }
}

}  // namespace

// One profile field resolved against its type: the member at each segment
// of the dotted path (every one but the last a nested, non-array message)
// and the segments themselves as output keys.
struct IntrospectionDecoder::FieldPlan {
  std::vector<const ti::MessageMember*> members;
  std::vector<std::string> keys;
};

struct IntrospectionDecoder::TopicPlan {
  bool whole_message = true;         // the profile lists no fields
  std::vector<FieldPlan> fields;     // resolvable fields, in profile order
  std::vector<std::string> missing;  // fields the type does not have, in profile order
};

namespace {

// Resolve a dotted path through nested messages to one member, as Python's
// getattr chain does: false when a segment names no member, or an earlier
// segment is a scalar or an array.
bool resolve(const ti::MessageMembers& root, const std::string& path,
             std::vector<const ti::MessageMember*>& members, std::vector<std::string>& keys) {
  keys = split_dotted(path);
  members.clear();
  const ti::MessageMembers* mm = &root;
  for (std::size_t k = 0; k < keys.size(); ++k) {
    const ti::MessageMember* found = nullptr;
    for (std::uint32_t i = 0; i < mm->member_count_; ++i) {
      if (keys[k] == mm->members_[i].name_) {
        found = &mm->members_[i];
        break;
      }
    }
    if (found == nullptr) {
      return false;
    }
    members.push_back(found);
    if (k + 1 == keys.size()) {
      return true;
    }
    if (found->type_id_ != ti::ROS_TYPE_MESSAGE || found->is_array_) {
      return false;  // Python's getattr path cannot go through arrays or scalars either
    }
    mm = members_of(*found);
  }
  return false;
}

}  // namespace

// Everything needed to decode one message type, plus a reusable instance.
struct IntrospectionDecoder::Type {
  std::shared_ptr<rcpputils::SharedLibrary> lib_cpp;
  std::shared_ptr<rcpputils::SharedLibrary> lib_intro;
  const rosidl_message_type_support_t* ts_cpp = nullptr;
  const ti::MessageMembers* members = nullptr;
  std::unique_ptr<rclcpp::SerializationBase> serializer;
  void* buffer = nullptr;

  explicit Type(const std::string& type) {
    lib_cpp = rclcpp::get_typesupport_library(type, "rosidl_typesupport_cpp");
    ts_cpp = rclcpp::get_typesupport_handle(type, "rosidl_typesupport_cpp", *lib_cpp);
    lib_intro = rclcpp::get_typesupport_library(type, "rosidl_typesupport_introspection_cpp");
    const auto* ts_intro =
        rclcpp::get_typesupport_handle(type, "rosidl_typesupport_introspection_cpp", *lib_intro);
    members = static_cast<const ti::MessageMembers*>(ts_intro->data);
    serializer = std::make_unique<rclcpp::SerializationBase>(ts_cpp);
    buffer = ::operator new (members->size_of_, std::align_val_t{alignof(std::max_align_t)});
    members->init_function(buffer, rosidl_runtime_cpp::MessageInitialization::ALL);
  }
  ~Type() {
    if (buffer != nullptr) {
      members->fini_function(buffer);
      ::operator delete (buffer, std::align_val_t{alignof(std::max_align_t)});
    }
  }
  Type(const Type&) = delete;
  Type& operator=(const Type&) = delete;
  Type(Type&&) = delete;
  Type& operator=(Type&&) = delete;
};

IntrospectionDecoder::IntrospectionDecoder(const blackboxrs::Profile& profile) {
  std::map<std::string, std::shared_ptr<Type>> by_type;
  for (const auto& t : profile.topics) {
    topic_types_.push_back(t.type);
    std::shared_ptr<Type> type;
    if (const auto it = by_type.find(t.type); it != by_type.end()) {
      type = it->second;
    } else if (errors_.count(t.type) == 0U) {
      try {
        type = std::make_shared<Type>(t.type);
        by_type[t.type] = type;
      } catch (const std::exception& exc) {
        errors_[t.type] = exc.what();
      }
    }
    TopicPlan plan;
    plan.whole_message = t.fields.empty();
    if (type) {
      for (const auto& f : t.fields) {
        FieldPlan fp;
        if (resolve(*type->members, f, fp.members, fp.keys)) {
          plan.fields.push_back(std::move(fp));
        } else {
          plan.missing.push_back(f);
        }
      }
    }
    by_topic_.push_back(type);
    plans_.push_back(std::move(plan));
  }
}

IntrospectionDecoder::~IntrospectionDecoder() = default;

std::optional<std::string> IntrospectionDecoder::unavailable(std::size_t topic) const {
  if (topic < by_topic_.size() && by_topic_[topic]) {
    return std::nullopt;
  }
  const auto it = errors_.find(topic < topic_types_.size() ? topic_types_[topic] : "");
  return it != errors_.end() ? it->second : std::string("unknown topic");
}

blackboxrs::recorder::DecodeResult IntrospectionDecoder::decode(
    std::size_t topic, const blackboxrs::recorder::Payload& payload) {
  blackboxrs::recorder::DecodeResult r;
  if (topic >= by_topic_.size() || !by_topic_[topic]) {
    r.error = "type support not available";
    return r;
  }
  Type& t = *by_topic_[topic];
  const auto& msg = static_cast<const SerializedPayload&>(payload).message();
  try {
    t.serializer->deserialize_message(&msg, t.buffer);
  } catch (const std::exception& exc) {
    r.error = std::string("CDR deserialization failed: ") + exc.what();
    return r;
  }
  const TopicPlan& plan = plans_[topic];
  if (plan.whole_message) {
    r.data = message_to_json(*t.members, t.buffer);
    return r;
  }
  Json out = Json::object();
  for (const FieldPlan& f : plan.fields) {
    // Down to the message holding the leaf member.
    const void* cur = t.buffer;
    for (std::size_t k = 0; k + 1 < f.members.size(); ++k) {
      cur = static_cast<const unsigned char*>(cur) + f.members[k]->offset_;
    }
    Json value = field_to_json(*f.members.back(), cur);
    // Stored like blackboxrs::set_path(out, path, value): intermediate
    // non-objects are replaced by objects.
    Json* node = &out;
    for (std::size_t k = 0; k + 1 < f.keys.size(); ++k) {
      Json& next = (*node)[f.keys[k]];
      if (!next.is_object()) {
        next = Json::object();
      }
      node = &next;
    }
    (*node)[f.keys.back()] = std::move(value);
  }
  r.missing = plan.missing;
  r.data = std::move(out);
  return r;
}

Json decode_to_json(const std::string& type, const rclcpp::SerializedMessage& msg,
                    const std::vector<std::string>& fields, std::vector<std::string>* missing) {
  blackboxrs::Profile p;
  blackboxrs::TopicSpec spec;
  spec.name = "/decode";
  spec.type = type;
  spec.fields = fields;
  p.topics.push_back(spec);
  IntrospectionDecoder d(p);
  if (const auto why = d.unavailable(0)) {
    throw std::runtime_error(*why);
  }
  auto copy = std::make_shared<rclcpp::SerializedMessage>(msg);
  const auto r = d.decode(0, SerializedPayload(copy));
  if (!r.data) {
    throw std::runtime_error(r.error);
  }
  if (missing != nullptr) {
    *missing = r.missing;
  }
  return *r.data;
}

}  // namespace blackboxrs_ros
