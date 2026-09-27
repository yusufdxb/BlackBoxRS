#include "blackboxrs/recorder/system_sampler.hpp"

#include <unistd.h>

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include "blackboxrs/time.hpp"

namespace blackboxrs::recorder {
namespace {

std::optional<std::string> read_first_line(const std::string& path) {
  std::ifstream in(path);
  std::string line;
  if (!in || !std::getline(in, line)) {
    return std::nullopt;
  }
  return line;
}

std::optional<std::int64_t> status_field_kb(const char* key) {
  std::ifstream in("/proc/self/status");
  std::string line;
  const std::string k = std::string(key) + ":";
  while (std::getline(in, line)) {
    if (line.rfind(k, 0) == 0) {
      std::istringstream ss(line.substr(k.size()));
      std::int64_t v = 0;
      if (ss >> v) {
        return v;
      }
    }
  }
  return std::nullopt;
}

}  // namespace

std::optional<double> current_rss_mb() {
  const auto kb = status_field_kb("VmRSS");
  return kb ? std::optional<double>(static_cast<double>(*kb) / 1024.0) : std::nullopt;
}

Json SystemSample::to_json() const {
  auto opt = [](const auto& v) { return v ? Json(*v) : Json(); };
  return {{"cpu_percent", opt(cpu_percent)},
          {"mem_percent", opt(mem_percent)},
          {"recorder",
           {{"cpu_percent", opt(recorder_cpu_percent)},
            {"rss_mb", opt(recorder_rss_mb)},
            {"threads", opt(recorder_threads)}}},
          {"gpu", {{"backend", "none"}}},
          {"thermal_c", thermal_c}};
}

SystemSample SystemSampler::sample() {
  SystemSample s;
  // Host CPU: first line of /proc/stat, "cpu user nice system idle iowait ...".
  if (const auto line = read_first_line("/proc/stat")) {
    std::istringstream ss(*line);
    std::string label;
    ss >> label;
    std::uint64_t v = 0;
    std::uint64_t total = 0;
    std::uint64_t idle = 0;
    for (int i = 0; ss >> v; ++i) {
      total += v;
      if (i == 3 || i == 4) {  // idle + iowait
        idle += v;
      }
    }
    if (last_total_ && total > *last_total_) {
      const auto dt = static_cast<double>(total - *last_total_);
      const auto di = static_cast<double>(idle - *last_idle_);
      s.cpu_percent = 100.0 * (1.0 - di / dt);
    }
    last_total_ = total;
    last_idle_ = idle;
  }
  // Host memory.
  {
    std::ifstream in("/proc/meminfo");
    std::string key;
    std::int64_t val = 0;
    std::string unit;
    std::optional<std::int64_t> total;
    std::optional<std::int64_t> avail;
    while (in >> key >> val >> unit) {
      if (key == "MemTotal:") total = val;
      if (key == "MemAvailable:") avail = val;
    }
    if (total && avail && *total > 0) {
      s.mem_percent = 100.0 * (1.0 - static_cast<double>(*avail) / static_cast<double>(*total));
    }
  }
  // This process: utime + stime (fields 14, 15 of /proc/self/stat).
  if (const auto line = read_first_line("/proc/self/stat")) {
    const auto rparen = line->rfind(')');
    if (rparen != std::string::npos) {
      std::istringstream ss(line->substr(rparen + 2));
      std::string field;
      std::uint64_t utime = 0;
      std::uint64_t stime = 0;
      for (int i = 3; i <= 15 && ss >> field; ++i) {
        if (i == 14) utime = std::stoull(field);
        if (i == 15) stime = std::stoull(field);
      }
      const std::uint64_t ticks = utime + stime;
      const std::int64_t now = count_ns(clock_domain::Mono::now());
      const long hz = ::sysconf(_SC_CLK_TCK);
      if (last_proc_ticks_ && now > *last_proc_ns_ && hz > 0) {
        const double cpu_s =
            static_cast<double>(ticks - *last_proc_ticks_) / static_cast<double>(hz);
        s.recorder_cpu_percent = 100.0 * cpu_s / (static_cast<double>(now - *last_proc_ns_) / 1e9);
      }
      last_proc_ticks_ = ticks;
      last_proc_ns_ = now;
    }
  }
  s.recorder_rss_mb = current_rss_mb();
  if (const auto th = status_field_kb("Threads")) {
    s.recorder_threads = *th;
  }
  // Thermal zones (present on the Jetson; often absent in containers).
  std::error_code ec;
  for (const auto& zone : std::filesystem::directory_iterator("/sys/class/thermal", ec)) {
    const auto name = zone.path().filename().string();
    if (name.rfind("thermal_zone", 0) != 0) {
      continue;
    }
    const auto type = read_first_line((zone.path() / "type").string());
    const auto temp = read_first_line((zone.path() / "temp").string());
    if (type && temp) {
      try {
        s.thermal_c[*type + ":" + name] = std::stod(*temp) / 1000.0;
      } catch (const std::exception&) {
      }
    }
  }
  return s;
}

}  // namespace blackboxrs::recorder
