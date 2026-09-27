#include "blackboxrs/evidence/bundle.hpp"

#include <algorithm>
#include <fstream>
#include <set>
#include <sstream>

#include "blackboxrs/integrity.hpp"
#include "blackboxrs/payload.hpp"

namespace blackboxrs {
namespace fs = std::filesystem;
namespace {

std::string trim(const std::string& s) {
  const auto b = s.find_first_not_of(" \t\r\n");
  if (b == std::string::npos) {
    return {};
  }
  const auto e = s.find_last_not_of(" \t\r\n");
  return s.substr(b, e - b + 1);
}

void require(bool cond, const std::string& msg) {
  if (!cond) {
    throw EvidenceError(msg);
  }
}

bool is_int(const Json& r, const char* key) {
  const auto it = r.find(key);
  return it != r.end() && (it->is_number_integer() || it->is_number_unsigned());
}

bool nonempty_string(const Json& r, const char* key) {
  const auto it = r.find(key);
  return it != r.end() && it->is_string() && !it->get_ref<const std::string&>().empty();
}

}  // namespace

bool is_input_kind(std::string_view kind) noexcept {
  return kind == "msg" || kind == "graph" || kind == "marker" || kind == "sys";
}

BundleRead load_bundle(const fs::path& dir) {
  BundleRead out;
  out.info.path = dir.string();
  const std::string name = dir.filename().string();
  out.info.partial_dir = name.ends_with(".partial");
  {
    std::ifstream mf(dir / "manifest.json");
    bool ok = false;
    if (mf) {
      std::stringstream ss;
      ss << mf.rdbuf();
      out.manifest = Json::parse(ss.str(), nullptr, /*allow_exceptions=*/false);
      ok = !out.manifest.is_discarded() && out.manifest.is_object();
    }
    if (!ok) {
      out.manifest = Json{{"schema", kManifestSchema}, {"status", "unknown"}};
      out.info.manifest_missing = true;
    }
  }
  std::ifstream rf(dir / "records.jsonl");
  if (rf) {
    std::string line;
    while (std::getline(rf, line)) {
      const std::string t = trim(line);
      if (t.empty()) {
        continue;
      }
      Json rec = Json::parse(t, nullptr, /*allow_exceptions=*/false);
      if (rec.is_discarded()) {
        ++out.info.torn_lines;
      } else {
        out.records.push_back(std::move(rec));
      }
    }
  }
  if (out.info.partial_dir) {
    const std::string status = out.manifest.value("status", std::string("unknown"));
    if (status == "capturing" || status == "unknown") {
      out.manifest["status"] = "interrupted_unfinalized";
    }
  }
  return out;
}

std::vector<Json> validate_records(std::vector<Json> records) {
  std::set<std::int64_t> seen;
  bool any_msg = false;
  for (std::size_t i = 0; i < records.size(); ++i) {
    const Json& r = records[i];
    const std::string where = "record #" + std::to_string(i + 1);
    require(r.is_object(), where + ": not a JSON object");
    require(r.contains("kind") && r["kind"].is_string(), where + ": missing 'kind'");
    require(is_int(r, "seq"), where + ": missing integer 'seq'");
    const auto seq = r["seq"].get<std::int64_t>();
    require(seen.insert(seq).second, where + ": duplicate seq " + std::to_string(seq));
    const auto& kind = r["kind"].get_ref<const std::string&>();
    any_msg = any_msg || kind == "msg";
    if (!is_input_kind(kind)) {
      continue;
    }
    const std::string ws = where + " (seq " + std::to_string(seq) + ")";
    require(is_int(r, "t_mono_ns"), ws + ": missing t_mono_ns");
    require(is_int(r, "t_wall_ns"), ws + ": missing t_wall_ns");
    if (kind == "msg") {
      for (const char* k : {"topic", "role", "type"}) {
        require(nonempty_string(r, k), ws + ": message without '" + k + "'");
      }
      const auto d = r.find("data");
      require(d == r.end() || d->is_null() || d->is_object(),
              ws + ": 'data' must be an object or null");
    }
  }
  require(any_msg, "evidence has no message records");
  std::stable_sort(records.begin(), records.end(), [](const Json& a, const Json& b) {
    return a["seq"].get<std::int64_t>() < b["seq"].get<std::int64_t>();
  });
  return records;
}

Evidence load_evidence(const fs::path& dir, bool allow_partial, const std::string& label) {
  require(fs::is_directory(dir), dir.string() + ": not a bundle directory");
  require(fs::is_regular_file(dir / "records.jsonl"), dir.string() + ": no records.jsonl");
  BundleRead b = load_bundle(dir);
  std::vector<std::string> problems;
  if (b.info.manifest_missing) {
    problems.emplace_back("manifest.json missing or unreadable");
  }
  if (b.info.torn_lines > 0) {
    problems.push_back(std::to_string(b.info.torn_lines) + " torn record line(s)");
  }
  const std::string status = b.manifest.value("status", std::string());
  // Same list as Python lab/evidence.py INCOMPLETE_STATUSES.
  if (status == "capturing" || status == "interrupted_unfinalized" || status == "unknown" ||
      status == "write_failed" || status == "complete_with_loss") {
    problems.push_back("bundle status is '" + status + "'");
  }
  if (!problems.empty() && !allow_partial) {
    std::string joined;
    for (std::size_t i = 0; i < problems.size(); ++i) {
      joined += (i != 0U ? "; " : "") + problems[i];
    }
    throw EvidenceError(dir.string() + ": incomplete evidence (" + joined +
                        "); pass allow_partial to replay it anyway");
  }
  std::string text;
  if (b.manifest.contains("profile") && b.manifest["profile"].is_object()) {
    const Json& p = b.manifest["profile"];
    if (p.contains("text") && p["text"].is_string()) {
      text = p["text"].get<std::string>();
    }
  }
  require(!text.empty(), dir.string() + ": manifest has no embedded profile text");
  Evidence ev;
  try {
    ev.profile = parse_profile_text(text);
  } catch (const ProfileError& exc) {
    throw EvidenceError(dir.string() + ": embedded profile is invalid: " + exc.what());
  }
  ev.records = validate_records(std::move(b.records));
  // Digest over the canonical form {"profile_sha256": ..., "records": [...]},
  // streamed so the whole document is never materialised.
  Sha256 h;
  h.update(R"({"profile_sha256":)");
  h.update(canonical_json(Json(ev.profile.sha256)));
  h.update(R"(,"records":[)");
  bool first = true;
  std::int64_t t0 = INT64_MAX;
  for (const Json& r : ev.records) {
    if (!first) {
      h.update(",");
    }
    first = false;
    h.update(canonical_json(r));
    if (is_input_kind(r["kind"].get_ref<const std::string&>())) {
      t0 = std::min(t0, r["t_mono_ns"].get<std::int64_t>());
    }
  }
  h.update("]}");
  ev.digest = h.finish_hex();
  ev.t0 = mono_ns(t0);
  ev.source = label.empty() ? dir.string() : label;
  ev.manifest = std::move(b.manifest);
  const Json& session = ev.manifest.contains("session") ? ev.manifest["session"] : Json();
  ev.synthetic =
      session.is_object() && session.contains("synthetic") && json_truthy(session["synthetic"]);
  ev.partial = !problems.empty();
  ev.problems = std::move(problems);
  ev.info = std::move(b.info);
  return ev;
}

}  // namespace blackboxrs
