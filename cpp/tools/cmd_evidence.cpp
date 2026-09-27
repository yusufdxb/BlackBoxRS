#include <filesystem>
#include <iostream>
#include <map>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "cli_args.hpp"
#include "commands.hpp"

namespace blackboxrs::cli {
namespace fs = std::filesystem;

int cmd_validate(const Argv& argv) {
  OptionSpec spec;
  spec.flags = {"--json"};
  const Args a(argv, spec);
  if (a.positional().size() != 1) {
    throw UsageError("validate takes one bundle directory");
  }
  const ValidationReport rep = validate_bundle(a.positional()[0]);
  if (a.flag("--json")) {
    std::cout << rep.to_json().dump(2) << "\n";
  } else {
    const char* label = rep.status == ValidationStatus::verified       ? "VERIFIED"
                        : rep.status == ValidationStatus::unverifiable ? "UNVERIFIABLE"
                                                                       : "INVALID";
    std::cout << label << "  " << a.positional()[0] << "  (" << rep.records << " records, status "
              << rep.manifest_status << ")\n";
    for (const auto& p : rep.problems) {
      std::cout << "  problem: " << p << "\n";
    }
    for (const auto& n : rep.notes) {
      std::cout << "  note: " << n << "\n";
    }
  }
  // 0 verified, 1 invalid, 3 structurally sound but not verifiable.
  return rep.status == ValidationStatus::verified       ? 0
         : rep.status == ValidationStatus::unverifiable ? 3
                                                        : 1;
}

int cmd_inspect(const Argv& argv) {
  OptionSpec spec;
  spec.flags = {"--json"};
  const Args a(argv, spec);
  if (a.positional().size() != 1) {
    throw UsageError("inspect takes one bundle directory");
  }
  const fs::path dir = a.positional()[0];
  if (!fs::is_directory(dir)) {
    std::cerr << "error: " << dir.string() << ": not a bundle directory\n";
    return 5;
  }
  const BundleRead b = load_bundle(dir);
  std::map<std::string, std::int64_t> kinds;
  std::map<std::string, std::int64_t> topics;
  std::map<std::string, std::int64_t> stored;
  std::optional<std::int64_t> t_first;
  std::optional<std::int64_t> t_last;
  for (const Json& r : b.records) {
    const std::string kind = r.value("kind", std::string("?"));
    ++kinds[kind];
    if (kind == "msg") {
      const std::string t = r.value("topic", std::string("?"));
      ++topics[t];
      if (r.contains("data") && !r["data"].is_null()) {
        ++stored[t];
      }
    }
    if (r.contains("t_mono_ns") && r["t_mono_ns"].is_number_integer()) {
      const auto t = r["t_mono_ns"].get<std::int64_t>();
      t_first = t_first ? std::min(*t_first, t) : t;
      t_last = t_last ? std::max(*t_last, t) : t;
    }
  }
  const double span = t_first ? static_cast<double>(*t_last - *t_first) / 1e9 : 0.0;
  Json out = {{"path", dir.string()},
              {"status", b.manifest.value("status", std::string("unknown"))},
              {"partial_dir", b.info.partial_dir},
              {"torn_lines", b.info.torn_lines},
              {"manifest_missing", b.info.manifest_missing},
              {"records", b.records.size()},
              {"span_s", span},
              {"kinds", kinds},
              {"topics", Json::object()},
              {"triggers", b.manifest.value("triggers", Json::array())},
              {"writer", b.manifest.value("writer", Json::object())},
              {"integrity", fs::exists(dir / "integrity.json") ? "present" : "absent"}};
  for (const auto& [t, n] : topics) {
    out["topics"][t] = {{"records", n},
                        {"stored", stored[t]},
                        {"rate_hz", span > 0 ? static_cast<double>(n) / span : 0.0}};
  }
  if (a.flag("--json")) {
    std::cout << out.dump(2) << "\n";
    return 0;
  }
  std::cout << "bundle    " << dir.string() << "\n"
            << "status    " << out["status"].get<std::string>()
            << (b.info.partial_dir ? "  (.partial: never finalized)" : "") << "\n"
            << "records   " << b.records.size() << " over " << span << " s"
            << (b.info.torn_lines != 0 ? ", " + std::to_string(b.info.torn_lines) + " torn line(s)"
                                       : "")
            << "\n"
            << "integrity " << out["integrity"].get<std::string>() << "\n";
  const Json& session = b.manifest.value("session", Json::object());
  if (session.value("synthetic", false)) {
    std::cout << "          SYNTHETIC evidence\n";
  }
  std::cout << "triggers\n";
  for (const auto& t : out["triggers"]) {
    std::cout << "  " << t.value("role", std::string("?")) << " "
              << t.value("type", std::string("?"));
    if (t.contains("topic")) {
      std::cout << " " << t["topic"].get<std::string>();
    }
    std::cout << "\n";
  }
  std::cout << "topics\n";
  for (const auto& [t, v] : out["topics"].items()) {
    char line[256];
    std::snprintf(line, sizeof line, "  %-40s %7lld records  %7lld stored  %8.1f Hz\n", t.c_str(),
                  static_cast<long long>(v["records"].get<std::int64_t>()),
                  static_cast<long long>(v["stored"].get<std::int64_t>()),
                  v["rate_hz"].get<double>());
    std::cout << line;
  }
  return 0;
}

}  // namespace blackboxrs::cli
