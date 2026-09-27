#include "blackboxrs/evidence/record.hpp"

#include <charconv>
#include <cmath>

namespace blackboxrs {
namespace {

void append_int(std::string& out, std::int64_t v) {
  char buf[24];
  const auto [end, ec] = std::to_chars(buf, buf + sizeof buf, v);
  out.append(buf, end);
}

void append_string(std::string& out, const std::string& s) {
  append_json_string(out, s);
}

void append_double(std::string& out, double v) {
  append_py_float(out, v);
}

template <class T>
void append_opt_time(std::string& out, const std::optional<T>& t, bool zero_is_null = false) {
  if (!t || (zero_is_null && count_ns(*t) == 0)) {
    out += "null";
  } else {
    append_int(out, count_ns(*t));
  }
}

}  // namespace

const char* record_kind_name(RecordKind k) noexcept {
  switch (k) {
    case RecordKind::msg: return "msg";
    case RecordKind::graph: return "graph";
    case RecordKind::sys: return "sys";
    case RecordKind::marker: return "marker";
    case RecordKind::trigger: return "trigger";
    case RecordKind::health: return "health";
    case RecordKind::clock_jump: return "clock_jump";
    case RecordKind::recorder: return "recorder";
  }
  return "msg";
}

void Record::serialize() {
  std::string out;
  if (kind == RecordKind::msg) {
    out.reserve(160 + (data ? 64U : 0U));
    out += R"({"kind":"msg","topic":)";
    append_string(out, topic);
    out += R"(,"role":)";
    append_string(out, role_name);
    out += R"(,"type":)";
    append_string(out, type);
    out += R"(,"t_mono_ns":)";
    append_int(out, count_ns(t_mono));
    out += R"(,"t_wall_ns":)";
    append_int(out, count_ns(t_wall));
    out += R"(,"t_ros_ns":)";
    append_opt_time(out, t_ros);
    out += R"(,"dds_src_ns":)";
    append_opt_time(out, dds_src, /*zero_is_null=*/true);
    out += R"(,"dds_rx_ns":)";
    append_opt_time(out, dds_rx, /*zero_is_null=*/true);
    out += R"(,"pub_stamp_s":)";
    if (pub_stamp_s) {
      append_double(out, *pub_stamp_s);
    } else {
      out += "null";
    }
    out += R"(,"pub_stamp_domain":)";
    if (pub_stamp_domain) {
      append_string(out, *pub_stamp_domain);
    } else {
      out += "null";
    }
    out += R"(,"data":)";
    if (data) {
      write_json(out, *data);
    } else {
      out += "null";
    }
  } else {
    out += R"({"kind":)";
    append_string(out, record_kind_name(kind));
    out += R"(,"t_mono_ns":)";
    append_int(out, count_ns(t_mono));
    out += R"(,"t_wall_ns":)";
    append_int(out, count_ns(t_wall));
    for (const auto& [k, v] : fields.items()) {
      out += ',';
      append_string(out, k);
      out += ':';
      write_json_any(out, v);
    }
  }
  out += R"(,"seq":)";
  append_int(out, seq);
  out += '}';
  line = std::move(out);
}

std::optional<StampSource> stamp_source(Role role) noexcept {
  switch (role) {
    case Role::helix_fault:
    case Role::recovery_action: return StampSource{"timestamp", "helix_payload_wall"};
    case Role::helix_hold:
    case Role::arbiter_status: return StampSource{"stamp", "helix_payload_wall"};
    case Role::sink_trace: return StampSource{"t_wall", "helix_payload_wall"};
    case Role::odometry: return StampSource{"header.stamp", "robot_clock"};
    case Role::go2_state: return StampSource{"stamp", "robot_clock"};
    case Role::phoenix_observation: return StampSource{"header.stamp", "publisher_header"};
    default: return std::nullopt;
  }
}

std::optional<double> publisher_stamp(Role role, const Json& data) {
  const auto src = stamp_source(role);
  if (!src) {
    return std::nullopt;
  }
  const Json* raw = get_path(data, src->path);
  if (raw == nullptr || raw->is_null() || raw->is_boolean()) {
    return std::nullopt;
  }
  double v = 0.0;
  if (raw->is_number()) {
    v = raw->get<double>();
  } else if (raw->is_object()) {
    const Json* sec = get_path(*raw, "sec");
    const Json* nsec = get_path(*raw, "nanosec");
    if (sec == nullptr || nsec == nullptr || !sec->is_number() || !nsec->is_number()) {
      return std::nullopt;
    }
    v = sec->get<double>() + nsec->get<double>() * 1e-9;
  } else {
    return std::nullopt;
  }
  if (v == 0.0 || !std::isfinite(v)) {
    return std::nullopt;
  }
  return v;
}

Record make_msg_record(std::string topic, Role role, std::string type, Json data, bool stored,
                       MonoTime t_mono, WallTime t_wall, std::optional<RosTime> t_ros,
                       std::optional<SourceTime> dds_src, std::optional<WallTime> dds_rx) {
  Record r;
  r.kind = RecordKind::msg;
  r.topic = std::move(topic);
  r.role = role;
  r.role_name = std::string(role_name(role));
  r.type = std::move(type);
  r.t_mono = t_mono;
  r.t_wall = t_wall;
  r.t_ros = t_ros;
  r.dds_src = dds_src;
  r.dds_rx = dds_rx;
  if (role == Role::sink_trace) {
    // HELIX's sink trace is JSON inside std_msgs/String: parse it in place.
    if (const Json* raw = get_path(data, "data"); raw != nullptr && raw->is_string()) {
      Json parsed = Json::parse(raw->get<std::string>(), nullptr, /*allow_exceptions=*/false);
      if (parsed.is_discarded()) {
        data = Json{{"unparsed", *raw}};
      } else if (parsed.is_object()) {
        data = std::move(parsed);
      }
    }
  }
  if (const auto stamp = publisher_stamp(role, data)) {
    r.pub_stamp_s = stamp;
    r.pub_stamp_domain = stamp_source(role)->domain;
  }
  if (stored) {
    r.typed = decode_payload(role, data);
    r.data = std::make_shared<const Json>(std::move(data));
  }
  return r;
}

Record make_event_record(RecordKind kind, MonoTime t_mono, WallTime t_wall, OrderedJson fields) {
  Record r;
  r.kind = kind;
  r.t_mono = t_mono;
  r.t_wall = t_wall;
  r.fields = std::move(fields);
  return r;
}

}  // namespace blackboxrs
