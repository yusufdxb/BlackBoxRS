#include "blackboxrs/event.hpp"

#include <algorithm>

namespace blackboxrs {
namespace {

std::optional<std::int64_t> int_or_none(const Json& r, const char* key) {
  const auto it = r.find(key);
  if (it == r.end() || it->is_null()) {
    return std::nullopt;
  }
  if (it->is_number_integer() || it->is_number_unsigned()) {
    return it->get<std::int64_t>();
  }
  if (it->is_number_float()) {
    return static_cast<std::int64_t>(it->get<double>());
  }
  return std::nullopt;
}

std::vector<std::string> string_list(const Json& r, const char* key) {
  std::vector<std::string> out;
  const auto it = r.find(key);
  if (it == r.end() || !it->is_array()) {
    return out;
  }
  for (const auto& v : *it) {
    if (v.is_string()) {
      out.push_back(v.get<std::string>());
    }
  }
  return out;
}

std::string string_or(const Json& r, const char* key, const char* def) {
  const auto it = r.find(key);
  return it != r.end() && it->is_string() ? it->get<std::string>() : std::string(def);
}

bool truthy_key(const Json& r, const char* key) {
  const auto it = r.find(key);
  return it != r.end() && json_truthy(*it);
}

}  // namespace

const std::string& Event::topic() const noexcept {
  static const std::string kEmpty;
  const MessageBody* m = message();
  return m != nullptr ? m->topic : kEmpty;
}

std::string_view Event::kind_name() const noexcept {
  switch (body.index()) {
    case 0: return "msg";
    case 1: return "graph";
    case 2: return "marker";
    default: return "sys";
  }
}

void Event::touch(const std::string& fault_id) {
  if (std::find(injected.begin(), injected.end(), fault_id) == injected.end()) {
    injected.push_back(fault_id);
  }
}

Event event_from_record(const Json& r, MonoTime evidence_start) {
  Event e;
  const std::int64_t mono = r["t_mono_ns"].get<std::int64_t>();
  const std::int64_t wall = r["t_wall_ns"].get<std::int64_t>();
  const std::int64_t seq = r["seq"].get<std::int64_t>();
  e.t = to_replay(mono_ns(mono), evidence_start);
  e.order = OrderKey{0, seq, 0};
  e.eid = "r" + std::to_string(seq);
  e.rec_mono = mono_ns(mono);
  e.rec_wall = wall_ns(wall);
  e.record = std::make_shared<const Json>(r);
  const auto& kind = r["kind"].get_ref<const std::string&>();
  if (kind == "msg") {
    MessageBody m;
    m.topic = r["topic"].get<std::string>();
    m.role_name = r["role"].get<std::string>();
    m.role = parse_role(m.role_name).value_or(Role::other);
    m.type = r["type"].get<std::string>();
    if (const auto src = int_or_none(r, "dds_src_ns"); src && *src != 0) {
      m.src = source_ns(*src);
    }
    const auto rx = int_or_none(r, "dds_rx_ns");
    m.rx_wall = wall_ns(rx && *rx != 0 ? *rx : wall);
    if (const auto it = r.find("pub_stamp_s"); it != r.end() && it->is_number()) {
      m.pub_stamp_s = it->get<double>();
    }
    if (const auto it = r.find("pub_stamp_domain"); it != r.end() && it->is_string()) {
      m.pub_stamp_domain = it->get<std::string>();
    }
    if (const auto it = r.find("data"); it != r.end() && it->is_object()) {
      m.set_data(*it);
    }
    e.body = std::move(m);
  } else if (kind == "graph") {
    GraphBody g;
    g.full = truthy_key(r, "full");
    g.nodes = string_list(r, "nodes");
    g.nodes_gone = string_list(r, "nodes_gone");
    g.nodes_new = string_list(r, "nodes_new");
    if (const auto it = r.find("publishers"); it != r.end() && it->is_object()) {
      g.has_publishers = true;
      for (const auto& [topic, pubs] : it->items()) {
        std::vector<std::string> names;
        if (pubs.is_array()) {
          for (const auto& n : pubs) {
            if (n.is_string()) {
              names.push_back(n.get<std::string>());
            }
          }
        }
        g.publishers.emplace(topic, std::move(names));
      }
    }
    e.body = std::move(g);
  } else if (kind == "marker") {
    e.body = MarkerBody{string_or(r, "note", ""), string_or(r, "source", "")};
  } else {
    e.body = SysBody{};
  }
  return e;
}

}  // namespace blackboxrs
