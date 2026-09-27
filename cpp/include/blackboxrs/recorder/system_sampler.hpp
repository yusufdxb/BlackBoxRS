// Host and recorder-process load from /proc (Linux only; no external tools).
//
// CPU percentages are computed from the difference between two samples, so
// the first sample reports none. Each call reads a handful of small /proc
// files (a few tens of microseconds); it is called at the profile's
// system_sample_hz (2 Hz by default) from the recorder pipeline.
#pragma once

#include <cstdint>
#include <optional>

#include "blackboxrs/json.hpp"

namespace blackboxrs::recorder {

struct SystemSample {
  std::optional<double> cpu_percent;  // whole host, all cores = 100
  std::optional<double> mem_percent;
  std::optional<double> recorder_cpu_percent;  // this process, one core = 100
  std::optional<double> recorder_rss_mb;
  std::optional<std::int64_t> recorder_threads;
  Json thermal_c = Json::object();  // zone name -> degrees C

  [[nodiscard]] Json to_json() const;
};

class SystemSampler {
 public:
  [[nodiscard]] SystemSample sample();

 private:
  std::optional<std::uint64_t> last_total_;
  std::optional<std::uint64_t> last_idle_;
  std::optional<std::uint64_t> last_proc_ticks_;
  std::optional<std::int64_t> last_proc_ns_;
};

// Current resident set size of this process in MiB (VmRSS).
[[nodiscard]] std::optional<double> current_rss_mb();

}  // namespace blackboxrs::recorder
