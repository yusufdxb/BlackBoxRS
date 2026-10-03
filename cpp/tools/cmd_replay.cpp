#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/integrity.hpp"
#include "blackboxrs/replay/engine.hpp"
#include "blackboxrs/replay/pacing.hpp"
#include "blackboxrs/replay/render.hpp"
#include "cli_args.hpp"
#include "commands.hpp"

namespace blackboxrs::cli {
namespace fs = std::filesystem;
using namespace blackboxrs::replay;

namespace {

int input_error(const std::string& msg) {
  std::cerr << "error: " << msg << "\n";
  return static_cast<int>(ExitCode::error);
}

Json parse_value(const std::string& v) {
  Json j = Json::parse(v, nullptr, false);
  if (j.is_discarded()) {
    std::string t = v;
    t.erase(0, t.find_first_not_of(" \t"));
    t.erase(t.find_last_not_of(" \t") + 1);
    return t;
  }
  return j;
}

std::string read_text(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw std::runtime_error("cannot read " + path);
  }
  std::stringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

void write_text(const std::string& path, const std::string& text) {
  std::ofstream out(path, std::ios::trunc);
  out << text << "\n";
  if (!out) {
    throw std::runtime_error("cannot write " + path);
  }
}

}  // namespace

int cmd_replay(const Argv& argv, bool inject_mode) {
  OptionSpec spec;
  spec.flags = {"--step", "--no-timeline", "--timeline", "--allow-partial"};
  spec.values = {"--sut", "--faults", "--from", "--to", "--speed", "--json"};
  spec.repeated = {"--set", "--inject", "--fault"};
  const Args a(argv, spec);
  if (a.positional().size() != 1) {
    throw UsageError(std::string(inject_mode ? "inject" : "replay") +
                     " takes exactly one case file or bundle directory");
  }
  const std::string target = a.positional()[0];
  if (!fs::exists(target)) {
    throw UsageError("no such file or directory: " + target);
  }
  std::vector<std::string> injects = a.all("--inject");
  for (const auto& f : a.all("--fault")) {
    injects.push_back(f);
  }
  if (inject_mode && injects.empty() && !a.value("--faults")) {
    throw UsageError("inject needs at least one --fault SPEC (see blackboxrs faults)");
  }
  Json result;
  std::optional<Case> c;
  bool changed = false;
  try {
    ReplayConfig cfg;
    std::string evidence_path = target;
    if (fs::is_regular_file(target) && fs::path(target).extension() == ".json") {
      c = load_case(target);
      cfg = c->config;
      evidence_path = c->evidence;
    }
    std::vector<Fault> extra;
    if (const auto ff = a.value("--faults")) {
      Json raw = Json::parse(read_text(*ff), nullptr, false);
      if (raw.is_discarded() || !raw.is_array()) {
        return input_error("--faults " + *ff + ": expected a JSON list of fault objects");
      }
      for (std::size_t i = 0; i < raw.size(); ++i) {
        extra.push_back(parse_fault(raw[i], cfg.faults.size() + i));
      }
    }
    for (const auto& spec_text : injects) {
      extra.push_back(parse_cli_fault(spec_text, cfg.faults.size() + extra.size()));
    }
    if (const auto sut = a.value("--sut")) {
      changed = true;
      if (*sut == "observed") {
        cfg.sut_mode = "observed";
        cfg.overrides = Json::object();
      } else if (*sut == "helix_arbiter" || *sut == "twist_mux_legacy") {
        cfg.sut_mode = "reference";
        cfg.preset = *sut;
      } else {
        throw UsageError("--sut must be helix_arbiter, twist_mux_legacy or observed");
      }
    }
    for (const auto& s : a.all("--set")) {
      const auto eq = s.find('=');
      if (eq == std::string::npos) {
        throw UsageError("--set expects key=value, got '" + s + "'");
      }
      std::string k = s.substr(0, eq);
      k.erase(0, k.find_first_not_of(" \t"));
      k.erase(k.find_last_not_of(" \t") + 1);
      cfg.overrides[k] = parse_value(s.substr(eq + 1));
      changed = true;
    }
    if (!extra.empty()) {
      for (auto& f : extra) {
        cfg.faults.push_back(std::move(f));
      }
      changed = true;
    }
    if (const auto v = a.number("--from")) {
      cfg.from_s = v;
      changed = true;
    }
    if (const auto v = a.number("--to")) {
      cfg.to_s = v;
      changed = true;
    }
    const Evidence ev = load_evidence(evidence_path, a.flag("--allow-partial"), evidence_path);
    ReplayOptions opts;
    std::shared_ptr<DeadlinePacer> pacer;
    if (const auto speed = a.number("--speed")) {
      if (!(*speed > 0)) {
        throw UsageError("--speed must be > 0");
      }
      // Pacing sleeps on the wall clock until each step's absolute deadline
      // (replay/pacing.hpp); it never feeds back into the replay
      // (tests/test_replay_determinism.cpp).
      pacer = std::make_shared<DeadlinePacer>(*speed);
      opts.pacer = [pacer](Nanos step) { pacer->pace(step); };
    }
    auto stepping = std::make_shared<bool>(a.flag("--step"));
    if (*stepping) {
      opts.observer = [stepping, pacer](const TimelineEntry& e) {
        if (!*stepping) {
          return;
        }
        Json j = {{"t_ns", e.t_ns}, {"layer", e.layer}, {"text", e.text}};
        std::cout << timeline_line(j) << std::flush;
        std::string line;
        if (!std::getline(std::cin, line) || line == "q" || line == "Q") {
          *stepping = false;
        }
        if (pacer) {
          pacer->rebase();  // the time spent waiting for the user is not caught up
        }
      };
    }
    result = blackboxrs::replay::replay(ev, cfg, opts);
  } catch (const CaseError& exc) {
    return input_error(exc.what());
  } catch (const EvidenceError& exc) {
    return input_error(exc.what());
  } catch (const FaultError& exc) {
    return input_error(exc.what());
  } catch (const std::invalid_argument& exc) {
    return input_error(exc.what());
  }
  const std::string text = canonical_json(result);
  const auto json_out = a.value("--json");
  if (json_out && *json_out == "-") {
    std::cout << text << "\n";
  } else {
    std::cout << render_text(result, !a.flag("--no-timeline") && !a.flag("--step")) << "\n";
    if (json_out) {
      write_text(*json_out, text);
      std::cout << "result written to " << *json_out << "\n";
    }
  }
  if (c && !c->expect.empty() && !(json_out && *json_out == "-")) {
    if (changed) {
      std::cout << "expectations: not checked (the case was modified on the command line)\n";
    } else {
      const auto mm = check_expectations(result, c->expect);
      std::cout << "expectations: " << (mm.empty() ? "match" : "MISMATCH") << "\n";
      for (const auto& m : mm) {
        std::cout << "  " << m << "\n";
      }
    }
  }
  return static_cast<int>(exit_code_for(result));
}

int cmd_verify(const Argv& argv) {
  OptionSpec spec;
  spec.values = {"--repeat", "--json"};
  const Args a(argv, spec);
  const auto repeat_d = a.number("--repeat").value_or(2.0);
  if (repeat_d < 1 || repeat_d != static_cast<double>(static_cast<int>(repeat_d))) {
    throw UsageError("--repeat must be an integer >= 1");
  }
  const int repeat = static_cast<int>(repeat_d);
  std::vector<std::string> files;
  std::vector<std::string> paths = a.positional();
  if (paths.empty()) {
    paths.emplace_back("examples/replay_lab/cases");
  }
  for (const auto& p : paths) {
    if (fs::is_directory(p)) {
      for (auto& f : list_case_files(p)) {
        files.push_back(std::move(f));
      }
    } else if (fs::is_regular_file(p)) {
      files.push_back(p);
    } else {
      throw UsageError(p + ": no such case file or directory");
    }
  }
  if (files.empty()) {
    throw UsageError("no case files found");
  }
  Json rows = Json::array();
  bool ok_all = true;
  int n_ok = 0;
  for (const auto& f : files) {
    std::vector<std::string> runs;
    Json result;
    Case c;
    try {
      c = load_case(f);
      if (c.expect.empty()) {
        throw CaseError(f + ": case has no expect block");
      }
      const Evidence ev = load_evidence(c.evidence, false, c.evidence);
      for (int r = 0; r < repeat; ++r) {
        Json res = blackboxrs::replay::replay(ev, c.config);
        runs.push_back(canonical_json(res));
        if (r == 0) {
          result = std::move(res);
        }
      }
    } catch (const std::exception& exc) {
      rows.push_back({{"case", f}, {"ok", false}, {"error", exc.what()}});
      ok_all = false;
      std::cout << "ERROR  " << f << ": " << exc.what() << "\n";
      continue;
    }
    const auto mm = check_expectations(result, c.expect);
    const bool stable =
        std::all_of(runs.begin(), runs.end(), [&](const std::string& r) { return r == runs[0]; });
    const bool ok = mm.empty() && stable;
    ok_all = ok_all && ok;
    n_ok += ok ? 1 : 0;
    const std::string digest = sha256_hex(runs[0]);
    rows.push_back({{"case", c.name},
                    {"ok", ok},
                    {"verdict", result["verdict"]["result"]},
                    {"mismatches", mm},
                    {"deterministic", stable},
                    {"repeats", repeat},
                    {"result_sha256", digest}});
    std::string name = c.name;
    if (name.size() < 44) {
      name.append(44 - name.size(), ' ');
    }
    std::string verdict = result["verdict"]["result"].get<std::string>();
    verdict.append(verdict.size() < 10 ? 10 - verdict.size() : 0, ' ');
    std::cout << (ok ? "ok    " : "FAIL  ") << name << " " << verdict << " "
              << (stable ? "deterministic" : "NONDETERMINISTIC") << " x" << repeat << "  "
              << digest.substr(0, 12) << "\n";
    for (const auto& m : mm) {
      std::cout << "        " << m << "\n";
    }
  }
  std::cout << n_ok << "/" << files.size() << " cases ok\n";
  if (const auto out = a.value("--json")) {
    const std::string text = canonical_json({{"cases", rows}, {"all_ok", ok_all}});
    if (*out == "-") {
      std::cout << text << "\n";
    } else {
      write_text(*out, text);
    }
  }
  return ok_all ? 0 : 1;
}

int cmd_faults(const Argv& argv) {
  const Args a(argv, OptionSpec{});
  if (!a.positional().empty()) {
    throw UsageError("faults takes no arguments");
  }
  for (const char* cat : {"transport", "data", "control"}) {
    std::cout << cat << "\n";
    for (const auto& k : fault_kinds()) {
      if (k.category != cat) {
        continue;
      }
      std::string name = k.name;
      name.append(name.size() < 17 ? 17 - name.size() : 0, ' ');
      std::cout << "  " << name << " " << k.summary << "\n";
      static constexpr std::array<const char*, 6> kType{"float", "int",  "bool",
                                                        "str",   "list", "json"};
      for (const auto& p : k.params) {
        std::string pn = p.name;
        pn.append(pn.size() < 12 ? 12 - pn.size() : 0, ' ');
        std::string t = kType[static_cast<std::size_t>(p.type)];
        t.append(t.size() < 6 ? 6 - t.size() : 0, ' ');
        std::string def = p.required
                              ? "required"
                              : "default " + (p.default_value.is_null() ? std::string("None")
                                                                        : p.default_value.dump());
        def.append(def.size() < 22 ? 22 - def.size() : 0, ' ');
        std::cout << "      " << pn << " " << t << " " << def << " " << p.help << "\n";
      }
    }
  }
  return 0;
}

}  // namespace blackboxrs::cli
