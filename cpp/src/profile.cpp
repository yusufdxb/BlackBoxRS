#include "blackboxrs/profile.hpp"

#include <yaml-cpp/yaml.h>

#include <set>

#include "blackboxrs/integrity.hpp"

namespace blackboxrs {
namespace {

double positive(const std::string& name, const YAML::Node& n) {
  double v = 0.0;
  try {
    v = n.as<double>();
  } catch (const YAML::Exception&) {
    throw ProfileError(name + " must be a number");
  }
  if (!(v > 0.0)) {
    throw ProfileError(name + " must be > 0");
  }
  return v;
}

double positive_or(const YAML::Node& parent, const char* key, const std::string& label,
                   double def) {
  const YAML::Node n = parent[key];
  if (!n || n.IsNull()) {
    return def;
  }
  return positive(label, n);
}

std::optional<double> optional_positive(const YAML::Node& parent, const char* key,
                                        const std::string& label) {
  const YAML::Node n = parent[key];
  if (!n || n.IsNull()) {
    return std::nullopt;
  }
  return positive(label, n);
}

std::string str_or(const YAML::Node& parent, const char* key, const std::string& def) {
  const YAML::Node n = parent[key];
  if (!n || n.IsNull()) {
    return def;
  }
  return n.as<std::string>();
}

bool bool_or(const YAML::Node& n, bool def) {
  if (!n || n.IsNull()) {
    return def;
  }
  return n.as<bool>();
}

// Trigger entries are a bool or a mapping with `enabled` (Python _flag).
bool flag(const YAML::Node& n, bool def, YAML::Node* cfg) {
  if (!n || n.IsNull()) {
    return def;
  }
  if (n.IsScalar()) {
    try {
      return n.as<bool>();
    } catch (const YAML::Exception&) {
      throw ProfileError("trigger value must be bool or mapping");
    }
  }
  if (n.IsMap()) {
    if (cfg != nullptr) {
      *cfg = n;
    }
    return bool_or(n["enabled"], true);
  }
  throw ProfileError("trigger value must be bool or mapping");
}

std::vector<std::string> string_list(const YAML::Node& n) {
  std::vector<std::string> out;
  if (!n || n.IsNull()) {
    return out;
  }
  if (!n.IsSequence()) {
    throw ProfileError("expected a list");
  }
  for (const auto& item : n) {
    out.push_back(item.as<std::string>());
  }
  return out;
}

std::size_t count_slashes(const std::string& s) {
  std::size_t n = 0;
  for (char c : s) {
    n += c == '/' ? 1U : 0U;
  }
  return n;
}

}  // namespace

const TopicSpec* Profile::topic(std::string_view topic_name) const noexcept {
  for (const auto& t : topics) {
    if (t.name == topic_name) {
      return &t;
    }
  }
  return nullptr;
}

Profile parse_profile_text(const std::string& text) {
  YAML::Node raw;
  try {
    raw = YAML::Load(text);
  } catch (const YAML::Exception& exc) {
    throw ProfileError(std::string("invalid YAML: ") + exc.what());
  }
  if (!raw.IsMap()) {
    throw ProfileError("profile must be a mapping");
  }
  if (raw["extends"] && !raw["extends"].IsNull()) {
    throw ProfileError("profile text has an unresolved 'extends'; embed the resolved profile");
  }
  Profile p;
  p.text = text;
  p.sha256 = sha256_hex(text);
  try {
    std::set<std::string> seen;
    const YAML::Node topics = raw["topics"];
    if (topics && !topics.IsNull()) {
      if (!topics.IsSequence()) {
        throw ProfileError("topics must be a list");
      }
      std::size_t i = 0;
      for (const auto& t : topics) {
        if (!t.IsMap() || !t["name"] || !t["type"]) {
          throw ProfileError("topics[" + std::to_string(i) + "] needs name and type");
        }
        TopicSpec spec;
        spec.name = t["name"].as<std::string>();
        if (spec.name.empty() || spec.name.front() != '/') {
          throw ProfileError("topic '" + spec.name + "' must be fully qualified");
        }
        if (!seen.insert(spec.name).second) {
          throw ProfileError("topic " + spec.name + " listed twice");
        }
        spec.type = t["type"].as<std::string>();
        if (count_slashes(spec.type) != 2) {
          throw ProfileError("topic " + spec.name + ": type must be pkg/msg/Type");
        }
        const std::string role = str_or(t, "role", "other");
        const auto r = parse_role(role);
        if (!r) {
          throw ProfileError("topic " + spec.name + ": unknown role '" + role + "'");
        }
        spec.role = *r;
        spec.required = bool_or(t["required"], false);
        spec.expected_hz = optional_positive(t, "expected_hz", spec.name + ".expected_hz");
        spec.stale_after_sec =
            optional_positive(t, "stale_after_sec", spec.name + ".stale_after_sec");
        spec.store_max_hz = optional_positive(t, "store_max_hz", spec.name + ".store_max_hz");
        spec.fields = string_list(t["fields"]);
        p.topics.push_back(std::move(spec));
        ++i;
      }
    }
    if (p.topics.empty()) {
      throw ProfileError("profile declares no topics");
    }
    const YAML::Node b = raw["buffer"] ? raw["buffer"] : YAML::Node(YAML::NodeType::Map);
    p.buffer.pre_trigger_sec = positive_or(b, "pre_trigger_sec", "buffer.pre_trigger_sec", 10.0);
    p.buffer.post_trigger_sec = positive_or(b, "post_trigger_sec", "buffer.post_trigger_sec", 15.0);
    p.buffer.max_records =
        static_cast<std::int64_t>(positive_or(b, "max_records", "buffer.max_records", 400'000.0));
    p.buffer.max_bytes = static_cast<std::int64_t>(
        positive_or(b, "max_bytes", "buffer.max_bytes", 256.0 * 1024 * 1024));
    const YAML::Node s = raw["sampling"] ? raw["sampling"] : YAML::Node(YAML::NodeType::Map);
    p.sampling.graph_poll_sec = positive_or(s, "graph_poll_sec", "sampling.graph_poll_sec", 0.5);
    p.sampling.system_sample_hz =
        positive_or(s, "system_sample_hz", "sampling.system_sample_hz", 2.0);
    p.sampling.health_tick_sec =
        positive_or(s, "health_tick_sec", "sampling.health_tick_sec", 0.25);

    const YAML::Node tr = raw["triggers"] ? raw["triggers"] : YAML::Node(YAML::NodeType::Map);
    const TriggerSpec defaults;
    p.triggers.helix_hold_asserted = flag(tr["helix_hold_asserted"], true, nullptr);
    YAML::Node ras_cfg;
    p.triggers.recovery_action_stop = flag(tr["recovery_action_stop"], true, &ras_cfg);
    if (ras_cfg && ras_cfg["actions"]) {
      p.triggers.recovery_actions = string_list(ras_cfg["actions"]);
    }
    if (ras_cfg && ras_cfg["statuses"]) {
      p.triggers.recovery_statuses = string_list(ras_cfg["statuses"]);
    }
    YAML::Node afz_cfg;
    p.triggers.arbiter_forced_zero = flag(tr["arbiter_forced_zero"], true, &afz_cfg);
    if (afz_cfg && afz_cfg["reasons"]) {
      p.triggers.arbiter_reasons = string_list(afz_cfg["reasons"]);
    }
    p.triggers.node_disappeared = flag(tr["node_disappeared"], true, nullptr);
    p.triggers.topic_stale = flag(tr["topic_stale"], true, nullptr);
    p.triggers.manual_marker = flag(tr["manual_marker"], true, nullptr);
    if (tr["max_incidents_per_run"]) {
      p.triggers.max_incidents_per_run = tr["max_incidents_per_run"].as<std::int64_t>();
    }
    p.name = str_or(raw, "profile", "profile");
    p.description = str_or(raw, "description", "");
    p.evidence_dir = str_or(raw, "evidence_dir", "~/blackboxrs_evidence");
    if (raw["min_free_disk_mb"]) {
      p.min_free_disk_mb = raw["min_free_disk_mb"].as<std::int64_t>();
    }
    p.co_hosted_roles = string_list(raw["co_hosted_roles"]);
    p.expected_nodes = string_list(raw["expected_nodes"]);
  } catch (const YAML::Exception& exc) {
    throw ProfileError(std::string("profile: ") + exc.what());
  }
  return p;
}

}  // namespace blackboxrs
