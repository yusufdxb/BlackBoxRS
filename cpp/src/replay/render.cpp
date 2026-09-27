#include "blackboxrs/replay/render.hpp"

#include <cstdio>
#include <map>

#include "blackboxrs/replay/monitors.hpp"

namespace blackboxrs::replay {
namespace {

std::string pad(std::string s, std::size_t width) {
  if (s.size() < width) {
    s.append(width - s.size(), ' ');
  }
  return s;
}

std::string rpad(std::string s, std::size_t width) {
  if (s.size() < width) {
    s.insert(0, width - s.size(), ' ');
  }
  return s;
}

std::string join(const Json& arr, const char* sep) {
  std::string s;
  for (const auto& v : arr) {
    s += (s.empty() ? "" : sep) + (v.is_string() ? v.get<std::string>() : v.dump());
  }
  return s;
}

std::string param_text(const Json& params) {
  std::string s;
  for (const auto& [k, v] : params.items()) {
    if (v.is_null()) {
      continue;
    }
    std::string val;
    if (v.is_string()) {
      val = v.get<std::string>();
    } else if (v.is_boolean()) {
      val = v.get<bool>() ? "True" : "False";
    } else {
      val = v.dump();
    }
    s += (s.empty() ? "" : ", ") + k + "=" + val;
  }
  return s;
}

}  // namespace

std::string timeline_line(const Json& e) {
  static const std::map<std::string, std::string> kLayer{
      {"fault", "FAULT"},     {"input", "INPUT"},   {"detector", "DETECT"},
      {"decision", "DECIDE"}, {"output", "OUTPUT"}, {"invariant", "INVARIANT"}};
  std::string cause;
  if (e.contains("caused_by") && !e["caused_by"].empty()) {
    cause = "  <- " + join(e["caused_by"], ", ");
  }
  if (e.contains("related_faults") && !e["related_faults"].empty()) {
    cause += "  [related fault: " + join(e["related_faults"], ", ") + "]";
  }
  const std::string ident = e.contains("id") ? e["id"].get<std::string>() + " " : "";
  const std::string t =
      fmt_signed_fixed(static_cast<double>(e["t_ns"].get<std::int64_t>()) / 1e9, 3) + "s";
  const std::string layer = e["layer"].get<std::string>();
  const auto it = kLayer.find(layer);
  return ident + rpad(t, 9) + "  " + pad(it != kLayer.end() ? it->second : layer, 9) + " " +
         e["text"].get<std::string>() + cause;
}

std::string render_text(const Json& res, bool timeline, std::size_t max_lines) {
  const Json& ev = res["evidence"];
  const Json& sut = res["config"]["sut"];
  const Json& v = res["verdict"];
  std::string out;
  auto line = [&](const std::string& s) { out += s + "\n"; };
  line("Replay Lab (C++)  run " + res["run_id"].get<std::string>().substr(0, 12));
  line("evidence  " + ev["source"].get<std::string>() + "  (" +
       std::to_string(ev["records"].get<std::int64_t>()) + " records, digest " +
       ev["digest"].get<std::string>().substr(0, 12) +
       (ev["synthetic"].get<bool>() ? ", SYNTHETIC" : "") +
       (ev["partial"].get<bool>() ? ", PARTIAL" : "") + ")");
  if (sut["mode"] == "reference") {
    line("sut       reference model: " + sut["preset"].get<std::string>() +
         (sut["freshness_clock"] != "receipt"
              ? " (freshness clock: " + sut["freshness_clock"].get<std::string>() + ")"
              : ""));
  } else {
    line("sut       observed output: " + (sut["output_source"].is_null()
                                              ? std::string("NOT RECORDED")
                                              : sut["output_source"].get<std::string>()));
  }
  const Json& rp = res["replay"];
  line("replay    " + std::to_string(rp["events_delivered"].get<std::int64_t>()) + " events, " +
       std::to_string(rp["ticks"].get<std::int64_t>()) + " ticks, " +
       fmt_g(static_cast<double>(rp["window_start_ns"].get<std::int64_t>()) / 1e9) + ".." +
       fmt_fixed(static_cast<double>(rp["window_end_ns"].get<std::int64_t>()) / 1e9, 3) + " s");
  if (!rp["suppressed_recorded_outputs"].empty()) {
    std::int64_t n = 0;
    for (const auto& [k, c] : rp["suppressed_recorded_outputs"].items()) {
      n += c.get<std::int64_t>();
    }
    line("          " + std::to_string(n) +
         " recorded arbiter-output records replaced by the model");
  }
  for (const auto& note : rp["notes"]) {
    line("note      " + note.get<std::string>());
  }
  for (const auto& f : res["injections"]) {
    line("fault     " + f["id"].get<std::string>() + " " + f["kind"].get<std::string>() + "(" +
         param_text(f["params"]) + ") -> " +
         std::to_string(f["events_affected"].get<std::int64_t>()) + " event(s)");
  }
  line("");
  line("invariants");
  for (const auto& [name, inv] : res["invariants"].items()) {
    std::string extra;
    const std::string st = inv["status"].get<std::string>();
    if (st == "FAIL") {
      extra = "  first at " +
              fmt_signed_fixed(
                  static_cast<double>(inv["first_violation_t_ns"].get<std::int64_t>()) / 1e9, 3) +
              "s, " + std::to_string(inv["violating_ticks"].get<std::int64_t>()) +
              " violating tick(s)";
    } else if (st == "INCOMPLETE") {
      extra = "  (" + inv["incomplete_reason"].get<std::string>() + ")";
    }
    line("  " + pad(st, 13) + " " + name + extra);
  }
  line("");
  line("findings");
  std::size_t infos = 0;
  bool any = false;
  for (const auto& f : res["findings"]) {
    if (f["severity"] == "info") {
      ++infos;
      continue;
    }
    any = true;
    line("  " + f["id"].get<std::string>() + " " + fmt_signed_fixed(f["t_s"].get<double>(), 3) +
         "s " + pad(f["severity"].get<std::string>(), 8) + " " + f["kind"].get<std::string>() +
         ": " + f["message"].get<std::string>());
  }
  if (!any) {
    line("  none above info");
  }
  if (infos != 0U) {
    line("  (+" + std::to_string(infos) + " info finding(s); see --json)");
  }
  if (timeline) {
    line("");
    line("causal timeline");
    const Json& tl = res["timeline"];
    for (std::size_t i = 0; i < tl.size() && i < max_lines; ++i) {
      line("  " + timeline_line(tl[i]));
    }
    if (tl.size() > max_lines) {
      line("  ... " + std::to_string(tl.size() - max_lines) + " more entries (see --json)");
    }
  }
  line("");
  line("VERDICT   " + v["result"].get<std::string>() + ": " + join(v["reasons"], "; "));
  if (!v["invariants_not_exercised"].empty()) {
    line("          not exercised: " + join(v["invariants_not_exercised"], ", "));
  }
  out.pop_back();
  return out;
}

}  // namespace blackboxrs::replay
