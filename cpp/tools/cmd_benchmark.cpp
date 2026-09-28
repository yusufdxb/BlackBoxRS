// blackboxrs benchmark: the same executable on the workstation and on the
// Jetson, with machine-readable output so the two can be compared directly.
//
// Sections:
//   serialize  Record::serialize throughput on GO2-shaped records
//   queue      BoundedQueue under 1, 2 and 4 contending producers
//   record     the full recording pipeline (ingest, decode stand-in,
//              FlightCore, evidence writer with fsync, integrity) at each
//              --scale of the measured GO2 + HELIX load, with drops, callback
//              latency, per-thread CPU, RSS and writer throughput
//   replay     deterministic replay throughput on the golden evidence
//   soak       (--soak SECONDS) long run at --soak-scale, RSS sampled every
//              second, to check that memory does not grow
//
// Nothing here asserts a threshold: numbers are recorded, not judged. A
// Debug build is labelled as such in the output.

#include <malloc.h>
#include <sys/resource.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <random>
#include <sstream>
#include <thread>

#include "blackboxrs/bench/load.hpp"
#include "blackboxrs/build_info.hpp"
#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "blackboxrs/recorder/bounded_queue.hpp"
#include "blackboxrs/recorder/recorder.hpp"
#include "blackboxrs/recorder/system_sampler.hpp"
#include "blackboxrs/replay/engine.hpp"
#include "cli_args.hpp"
#include "commands.hpp"

namespace blackboxrs::cli {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

namespace {

double pct(std::vector<std::int64_t> v, double q) {
  if (v.empty()) {
    return 0.0;
  }
  std::sort(v.begin(), v.end());
  const auto idx = static_cast<std::size_t>(std::ceil(q * static_cast<double>(v.size()))) - 1;
  return static_cast<double>(v[std::min(idx, v.size() - 1)]);
}

std::string read_line(const std::string& path) {
  std::ifstream in(path);
  std::string line;
  std::getline(in, line);
  return line;
}

// utime + stime of one thread, in seconds.
double thread_cpu_s(int tid) {
  if (tid <= 0) {
    return 0.0;
  }
  const std::string line = read_line("/proc/self/task/" + std::to_string(tid) + "/stat");
  const auto rp = line.rfind(')');
  if (rp == std::string::npos) {
    return 0.0;
  }
  std::istringstream ss(line.substr(rp + 2));
  std::string f;
  std::uint64_t ut = 0;
  std::uint64_t st = 0;
  for (int i = 3; i <= 15 && ss >> f; ++i) {
    if (i == 14) ut = std::stoull(f);
    if (i == 15) st = std::stoull(f);
  }
  return static_cast<double>(ut + st) / static_cast<double>(::sysconf(_SC_CLK_TCK));
}

double process_cpu_s() {
  rusage ru{};
  getrusage(RUSAGE_SELF, &ru);
  return static_cast<double>(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) +
         static_cast<double>(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1e6;
}

double peak_rss_mb() {
  rusage ru{};
  getrusage(RUSAGE_SELF, &ru);
  return static_cast<double>(ru.ru_maxrss) / 1024.0;
}

Json environment() {
  std::string model;
  std::ifstream ci("/proc/cpuinfo");
  std::string line;
  while (std::getline(ci, line)) {
    if (line.rfind("model name", 0) == 0 || line.rfind("Model", 0) == 0) {
      model = line.substr(line.find(':') + 2);
      break;
    }
  }
  std::string kernel = read_line("/proc/sys/kernel/osrelease");
  return {
    {"cpu_model", model}, {"logical_cpus", std::thread::hardware_concurrency()}, {"kernel", kernel},
#if defined(__aarch64__)
        {"arch", "aarch64"},
#elif defined(__x86_64__)
        {"arch", "x86_64"},
#else
        {"arch", "other"},
#endif
        {"build_type", build_info::kBuildType}, {"compiler", build_info::kCompiler},
        {"git_sha", build_info::kGitSha}, {"git_dirty", build_info::kGitDirty}, {
      "version", build_info::kVersion
    }
  };
}

recorder::RecorderConfig recorder_config(const fs::path& dir) {
  recorder::RecorderConfig c;
  c.continuous = true;
  c.writer.session_dir = dir;
  c.manifest.session = {{"session_id", "benchmark"}, {"synthetic", true}};
  c.manifest.profile = Json::object();
  c.manifest.config_sha256 = "benchmark";
  c.manifest.writer_build = {{"implementation", "blackboxrs-cpp"},
                             {"version", build_info::kVersion},
                             {"git_sha", build_info::kGitSha},
                             {"build_type", build_info::kBuildType}};
  return c;
}

Json bench_serialize() {
  const auto load = bench::go2_helix_load();
  std::vector<Record> recs;
  for (int i = 0; i < 1000; ++i) {
    const auto& t = load[static_cast<std::size_t>(i) % load.size()];
    Record r =
        make_msg_record(t.topic, Role::go2_state, "x/msg/Y",
                        t.make(static_cast<std::uint64_t>(i), 1'789'000'000'000'000'000LL), true,
                        mono_ns(1'000'000'000LL + i), wall_ns(1'789'000'000'000'000'000LL + i),
                        std::nullopt, source_ns(1'789'000'000'000'000'000LL), std::nullopt);
    r.seq = i;
    recs.push_back(std::move(r));
  }
  const int rounds = 200;
  std::uint64_t bytes = 0;
  const auto t0 = Clock::now();
  for (int k = 0; k < rounds; ++k) {
    for (auto& r : recs) {
      r.serialize();
      bytes += r.line.size();
    }
  }
  const double s = std::chrono::duration<double>(Clock::now() - t0).count();
  const double n = static_cast<double>(rounds) * static_cast<double>(recs.size());
  return {{"records", n},
          {"records_per_sec", n / s},
          {"mb_per_sec", static_cast<double>(bytes) / s / 1e6},
          {"mean_record_bytes", static_cast<double>(bytes) / n}};
}

Json bench_queue() {
  Json out = Json::array();
  for (int producers : {1, 2, 4}) {
    recorder::BoundedQueue<std::uint64_t> q(65'536);
    constexpr std::uint64_t kEach = 500'000;
    std::atomic<bool> done{false};
    std::uint64_t consumed = 0;
    std::atomic<std::uint64_t> rejected{0};
    const auto t0 = Clock::now();
    std::jthread consumer([&] {
      std::vector<std::uint64_t> batch;
      batch.reserve(512);
      while (!done.load() || q.stats().depth != 0) {
        batch.clear();
        consumed += q.pop_batch(batch, 512, Clock::now() + 1ms, {});
      }
    });
    {
      std::vector<std::jthread> ps;
      ps.reserve(static_cast<std::size_t>(producers));
      for (int p = 0; p < producers; ++p) {
        ps.emplace_back([&] {
          for (std::uint64_t i = 0; i < kEach; ++i) {
            std::uint64_t v = i;
            if (q.try_push(v) != recorder::PushResult::ok) {
              rejected.fetch_add(1);
            }
          }
        });
      }
    }
    done.store(true);
    consumer.join();
    const double s = std::chrono::duration<double>(Clock::now() - t0).count();
    const auto total = static_cast<double>(kEach) * producers;
    out.push_back({{"producers", producers},
                   {"offered", total},
                   {"consumed", consumed},
                   {"rejected_full", rejected.load()},
                   {"push_ops_per_sec", total / s},
                   {"high_water", q.stats().high_water}});
  }
  return out;
}

Json bench_record(const Profile& profile, double scale, std::chrono::seconds duration,
                  const fs::path& work) {
  const fs::path dir = work / ("record_x" + std::to_string(static_cast<int>(scale)));
  fs::remove_all(dir);
  const auto load = bench::go2_helix_load();
  const double rss_before = recorder::current_rss_mb().value_or(0.0);
  const double cpu0 = process_cpu_s();
  auto rec = std::make_unique<recorder::Recorder>(profile, recorder_config(dir),
                                                  std::make_unique<bench::JsonPayloadDecoder>());
  std::this_thread::sleep_for(50ms);
  const auto [ptid, wtid] = rec->thread_ids();
  const double pcpu0 = thread_cpu_s(ptid);
  const double wcpu0 = thread_cpu_s(wtid);
  const auto t0 = Clock::now();
  const bench::GeneratorResult g = bench::run_load(*rec, load, scale, duration);
  const double pcpu = thread_cpu_s(ptid) - pcpu0;
  const double wcpu = thread_cpu_s(wtid) - wcpu0;
  const auto stop_t0 = Clock::now();
  rec->stop("benchmark_done");
  const double stop_s = std::chrono::duration<double>(Clock::now() - stop_t0).count();
  const double wall = std::chrono::duration<double>(Clock::now() - t0).count();
  const recorder::RecorderMetrics m = rec->metrics();
  const auto bundles = rec->bundles();
  rec.reset();
  const double cpu = process_cpu_s() - cpu0;
  std::string validation = "no bundle";
  if (!bundles.empty()) {
    const auto v = validate_bundle(bundles.front());
    validation = v.status == ValidationStatus::verified ? "verified" : "invalid";
  }
  double hz = 0.0;
  for (const auto& t : load) {
    if (profile.topic(t.topic) != nullptr) {
      hz += t.hz * scale;
    }
  }
  return {{"scale", scale},
          {"duration_s", g.wall_s},
          {"offered_msgs_per_sec", static_cast<double>(g.offered) / g.wall_s},
          {"expected_msgs_per_sec", hz},
          {"offered", g.offered},
          {"received", m.received},
          {"processed", m.processed},
          {"dropped_ingest", m.dropped_ingest},
          {"dropped_at_shutdown", m.dropped_at_shutdown},
          {"dropped_events", m.dropped_ingest + m.dropped_at_shutdown},
          {"records_written", m.writer.records_written},
          {"bytes_written", m.writer.bytes_written},
          {"write_mb_per_sec", static_cast<double>(m.writer.bytes_written) / g.wall_s / 1e6},
          {"peak_queue_depth", m.ingest.high_water},
          {"queue_capacity", m.ingest.capacity},
          {"writer_max_lag_ms", static_cast<double>(m.writer.max_lag_ns) / 1e6},
          {"push_latency_us",
           {{"samples", g.push_ns.size()},
            {"p50", pct(g.push_ns, 0.50) / 1e3},
            {"p95", pct(g.push_ns, 0.95) / 1e3},
            {"p99", pct(g.push_ns, 0.99) / 1e3},
            {"max", pct(g.push_ns, 1.0) / 1e3}}},
          {"cpu_percent",
           {{"pipeline_thread", 100.0 * pcpu / g.wall_s},
            {"writer_thread", 100.0 * wcpu / g.wall_s},
            {"process_including_load_generator", 100.0 * cpu / wall}}},
          {"rss_mb", {{"before", rss_before}, {"end", recorder::current_rss_mb().value_or(0.0)}}},
          {"stop_s", stop_s},
          {"evidence", validation}};
}

Json bench_replay(const fs::path& repo) {
  Json out = Json::array();
  for (const char* c : {"nominal_motion", "clean_stop", "stale_command__twist_mux_legacy",
                        "teleop_vs_stop__helix_arbiter"}) {
    const auto cs = replay::load_case(
        (repo / "examples/replay_lab/cases" / (std::string(c) + ".json")).string());
    const Evidence ev = load_evidence(cs.evidence, false, cs.evidence);
    const int runs = 20;
    const auto t0 = Clock::now();
    std::int64_t events = 0;
    std::int64_t span = 0;
    for (int i = 0; i < runs; ++i) {
      const Json r = replay::replay(ev, cs.config);
      events += r["replay"]["events_delivered"].get<std::int64_t>() +
                r["replay"]["ticks"].get<std::int64_t>();
      span = r["replay"]["window_end_ns"].get<std::int64_t>();
    }
    const double s = std::chrono::duration<double>(Clock::now() - t0).count();
    out.push_back({{"case", c},
                   {"runs", runs},
                   {"dispatches_per_sec", static_cast<double>(events) / s},
                   {"replay_x_realtime", static_cast<double>(span) / 1e9 * runs / s},
                   {"ms_per_replay", s * 1e3 / runs}});
  }
  return out;
}

Json bench_soak(const Profile& profile, double scale, std::chrono::seconds duration,
                const fs::path& work) {
  const fs::path dir = work / "soak";
  fs::remove_all(dir);
  const auto load = bench::go2_helix_load();
  auto rec = std::make_unique<recorder::Recorder>(profile, recorder_config(dir),
                                                  std::make_unique<bench::JsonPayloadDecoder>());
  // Samples are plain structs in storage reserved up front, so the
  // measuring harness does not itself grow the RSS it measures (a JSON
  // object per sample did, by about 1 MB over 30 min).
  // Besides RSS, each sample records what glibc's allocator holds, so RSS
  // growth can be told apart: live heap (heap_in_use), memory the allocator
  // keeps but no one uses (heap_free), and anonymous vs file-backed pages.
  struct Sample {
    double t_s;
    double rss_mb;
    double rss_anon_mb;
    double rss_file_mb;
    double heap_in_use_mb;
    double heap_free_mb;
    double heap_mmap_mb;
    std::size_t queue_depth;
    std::uint64_t records_written;
    std::uint64_t dropped;
  };
  const auto status_mb = [](const char* key) {
    std::ifstream in("/proc/self/status");
    std::string line;
    const std::string k = std::string(key) + ":";
    while (std::getline(in, line)) {
      if (line.rfind(k, 0) == 0) {
        return std::stod(line.substr(k.size())) / 1024.0;
      }
    }
    return 0.0;
  };
  std::vector<Sample> raw;
  raw.reserve(static_cast<std::size_t>(duration.count()) + 16);
  std::atomic<bool> running{true};
  std::jthread sampler([&] {
    const auto t0 = Clock::now();
    while (running.load()) {
      std::this_thread::sleep_for(1s);
      const auto m = rec->metrics();
      if (raw.size() < raw.capacity()) {
        const struct mallinfo2 mi = ::mallinfo2();
        constexpr double kMb = 1024.0 * 1024.0;
        raw.push_back({std::chrono::duration<double>(Clock::now() - t0).count(),
                       recorder::current_rss_mb().value_or(0.0), status_mb("RssAnon"),
                       status_mb("RssFile"), static_cast<double>(mi.uordblks + mi.hblkhd) / kMb,
                       static_cast<double>(mi.fordblks) / kMb, static_cast<double>(mi.hblkhd) / kMb,
                       m.ingest.depth, m.writer.records_written, m.dropped_ingest});
      }
    }
  });
  const bench::GeneratorResult g = bench::run_load(*rec, load, scale, duration, 0);
  running.store(false);
  sampler.join();
  rec->stop("soak_done");
  const auto m = rec->metrics();
  const auto bundles = rec->bundles();
  rec.reset();
  Json samples = Json::array();
  for (const Sample& x : raw) {
    samples.push_back({{"t_s", x.t_s},
                       {"rss_mb", x.rss_mb},
                       {"rss_anon_mb", x.rss_anon_mb},
                       {"rss_file_mb", x.rss_file_mb},
                       {"heap_in_use_mb", x.heap_in_use_mb},
                       {"heap_free_mb", x.heap_free_mb},
                       {"heap_mmap_mb", x.heap_mmap_mb},
                       {"queue_depth", x.queue_depth},
                       {"records_written", x.records_written},
                       {"dropped", x.dropped}});
  }
  // RSS slope after a warmup of 10 % of the run (least squares, MB per hour).
  std::vector<double> ts;
  std::vector<double> rs;
  for (const auto& s : samples) {
    if (s["t_s"].get<double>() >= 0.1 * static_cast<double>(duration.count())) {
      ts.push_back(s["t_s"].get<double>());
      rs.push_back(s["rss_mb"].get<double>());
    }
  }
  double slope = 0.0;
  if (ts.size() > 2) {
    const double mt = std::accumulate(ts.begin(), ts.end(), 0.0) / static_cast<double>(ts.size());
    const double mr = std::accumulate(rs.begin(), rs.end(), 0.0) / static_cast<double>(rs.size());
    double num = 0.0;
    double den = 0.0;
    for (std::size_t i = 0; i < ts.size(); ++i) {
      num += (ts[i] - mt) * (rs[i] - mr);
      den += (ts[i] - mt) * (ts[i] - mt);
    }
    slope = den > 0 ? num / den * 3600.0 : 0.0;
  }
  const auto [mn, mx] = std::minmax_element(rs.begin(), rs.end());
  std::string validation = "no bundle";
  if (!bundles.empty()) {
    validation = validate_bundle(bundles.front()).status == ValidationStatus::verified ? "verified"
                                                                                       : "invalid";
  }
  return {{"scale", scale},
          {"duration_s", g.wall_s},
          {"offered", g.offered},
          {"dropped_events", m.dropped_ingest + m.dropped_at_shutdown},
          {"records_written", m.writer.records_written},
          {"bytes_written", m.writer.bytes_written},
          {"rss_after_warmup_min_mb", rs.empty() ? 0.0 : *mn},
          {"rss_after_warmup_max_mb", rs.empty() ? 0.0 : *mx},
          {"rss_slope_mb_per_hour", slope},
          {"peak_queue_depth", m.ingest.high_water},
          {"threads_at_end", m.system.value("recorder", Json::object()).value("threads", Json())},
          {"evidence", validation},
          {"samples", samples}};
}

}  // namespace

int cmd_benchmark(const Argv& argv) {
  OptionSpec spec;
  spec.values = {"--profile", "--scale",      "--seconds", "--json", "--work-dir",
                 "--soak",    "--soak-scale", "--only",    "--repo"};
  const Args a(argv, spec);
  const std::string repo = a.value("--repo").value_or(".");
  const std::string profile_path =
      a.value("--profile")
          .value_or((fs::path(repo) / "blackboxrs/flight/profiles/go2_helix.yaml").string());
  const Profile profile = load_profile_file(profile_path);
  std::vector<double> scales;
  {
    std::stringstream ss(a.value("--scale").value_or("1,2,5"));
    std::string item;
    while (std::getline(ss, item, ',')) {
      scales.push_back(std::stod(item));
    }
  }
  const auto seconds = std::chrono::seconds(static_cast<int>(a.number("--seconds").value_or(10.0)));
  const fs::path work =
      a.value("--work-dir").value_or((fs::temp_directory_path() / "blackboxrs_bench").string());
  fs::create_directories(work);
  const std::string only = a.value("--only").value_or("serialize,queue,record,replay");
  auto want = [&](const char* s) { return only.find(s) != std::string::npos; };
  Json out = {
      {"schema", "blackboxrs.benchmark.v1"},
      {"environment", environment()},
      {"profile", {{"path", profile_path}, {"name", profile.name}, {"sha256", profile.sha256}}},
      {"load", Json::array()}};
  for (const auto& t : bench::go2_helix_load()) {
    out["load"].push_back({{"topic", t.topic},
                           {"hz_at_1x", t.hz},
                           {"rate_source", t.rate_source},
                           {"in_profile", profile.topic(t.topic) != nullptr}});
  }
  if (std::string(build_info::kBuildType) != "Release") {
    out["warning"] = "not a Release build: numbers are not representative";
    std::cerr << "warning: " << build_info::kBuildType
              << " build, numbers are not representative\n";
  }
  if (want("serialize")) {
    std::cerr << "serialize ...\n";
    out["serialize"] = bench_serialize();
  }
  if (want("queue")) {
    std::cerr << "queue ...\n";
    out["queue"] = bench_queue();
  }
  if (want("record")) {
    out["record"] = Json::array();
    for (double s : scales) {
      std::cerr << "record x" << s << " for " << seconds.count() << " s ...\n";
      out["record"].push_back(bench_record(profile, s, seconds, work));
    }
  }
  if (want("replay")) {
    std::cerr << "replay ...\n";
    out["replay"] = bench_replay(repo);
  }
  if (const auto soak = a.number("--soak")) {
    const double sc = a.number("--soak-scale").value_or(5.0);
    std::cerr << "soak x" << sc << " for " << *soak << " s ...\n";
    out["soak"] = bench_soak(profile, sc, std::chrono::seconds(static_cast<int>(*soak)), work);
  }
  out["peak_rss_mb"] = peak_rss_mb();
  const std::string text = out.dump(2);
  if (const auto path = a.value("--json")) {
    std::ofstream(*path) << text << "\n";
    std::cerr << "written to " << *path << "\n";
  } else {
    std::cout << text << "\n";
  }
  return 0;
}

}  // namespace blackboxrs::cli

namespace blackboxrs::cli {

// Record the synthetic GO2 + HELIX load through the real pipeline into a
// bundle (continuous mode). For compatibility tests and rehearsals only:
// the bundle says synthetic.
int cmd_synth_record(const Argv& argv) {
  OptionSpec spec;
  spec.values = {"--out", "--seconds", "--scale", "--profile", "--repo"};
  const Args a(argv, spec);
  const auto out = a.value("--out");
  if (!out) {
    throw UsageError("synth-record needs --out DIR");
  }
  const std::string repo = a.value("--repo").value_or(".");
  const std::string profile_path =
      a.value("--profile")
          .value_or((fs::path(repo) / "blackboxrs/flight/profiles/go2_helix.yaml").string());
  const Profile profile = load_profile_file(profile_path);
  recorder::RecorderConfig cfg = recorder_config(*out);
  cfg.manifest.profile = profile_manifest_block(profile, profile_path);
  cfg.manifest.session["recorder"] = "blackboxrs-cpp synth-record";
  auto rec = std::make_unique<recorder::Recorder>(profile, cfg,
                                                  std::make_unique<bench::JsonPayloadDecoder>());
  const auto g = bench::run_load(*rec, bench::go2_helix_load(), a.number("--scale").value_or(1.0),
                                 std::chrono::milliseconds(static_cast<std::int64_t>(
                                     a.number("--seconds").value_or(3.0) * 1000)),
                                 0);
  rec->stop("synth_record_done");
  const auto m = rec->metrics();
  for (const auto& b : rec->bundles()) {
    std::cout << b << "\n";
  }
  std::cerr << "offered " << g.offered << ", processed " << m.processed << ", dropped "
            << m.dropped_ingest + m.dropped_at_shutdown << "\n";
  return rec->bundles().empty() ? 1 : 0;
}

}  // namespace blackboxrs::cli
