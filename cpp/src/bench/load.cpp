#include "blackboxrs/bench/load.hpp"

#include <algorithm>
#include <cmath>
#include <queue>
#include <thread>

namespace blackboxrs::bench {
namespace {

using Clock = std::chrono::steady_clock;

Json stamp(std::int64_t ns) {
  return {{"sec", ns / 1'000'000'000}, {"nanosec", ns % 1'000'000'000}};
}

Json twist(double vx, double wz) {
  return {{"linear", {{"x", vx}, {"y", 0.0}, {"z", 0.0}}},
          {"angular", {{"x", 0.0}, {"y", 0.0}, {"z", wz}}}};
}

}  // namespace

recorder::DecodeResult JsonPayloadDecoder::decode(std::size_t, const recorder::Payload& p) {
  recorder::DecodeResult r;
  r.data.emplace(static_cast<const JsonPayload&>(p).json());
  return r;
}

std::vector<TopicLoad> go2_helix_load() {
  std::vector<TopicLoad> load;
  load.push_back({"/lowstate", 500.0, "measured on GO2 (field notes 2026-09-01/02)", 1020,
                  [](std::uint64_t n, std::int64_t) {
                    const auto i = static_cast<std::int64_t>(n);
                    return Json{{"tick", 1000 + i},
                                {"power_v", 28.5 - 0.0001 * static_cast<double>(n % 1000)},
                                {"power_a", 3.2},
                                {"bms_state", {{"soc", 87}, {"current", -3200}}},
                                {"foot_force", {21, 19, 23, 20}},
                                {"temperature_ntc1", 41}};
                  }});
  load.push_back({"/sportmodestate", 295.0, "measured on GO2 (field notes 2026-09-01/02)", 260,
                  [](std::uint64_t n, std::int64_t wall) {
                    return Json{{"stamp", stamp(wall)},
                                {"error_code", 0},
                                {"mode", 1},
                                {"progress", 0.0},
                                {"gait_type", 1},
                                {"position", {0.001 * static_cast<double>(n), 0.0, 0.31}},
                                {"body_height", 0.31},
                                {"velocity", {0.15, 0.0, 0.0}},
                                {"yaw_speed", 0.0}};
                  }});
  load.push_back(
      {"/utlidar/robot_odom", 151.0, "measured on GO2 (field notes 2026-09-01/02)", 720,
       [](std::uint64_t n, std::int64_t wall) {
         return Json{
             {"header", {{"stamp", stamp(wall)}, {"frame_id", "odom"}}},
             {"child_frame_id", "base_link"},
             {"pose",
              {{"pose",
                {{"position", {{"x", 0.001 * static_cast<double>(n)}, {"y", 0.0}, {"z", 0.0}}},
                 {"orientation", {{"x", 0.0}, {"y", 0.0}, {"z", 0.0}, {"w", 1.0}}}}}}},
             {"twist",
              {{"twist",
                {{"linear", {{"x", 0.15}, {"y", 0.0}, {"z", 0.0}}},
                 {"angular", {{"x", 0.0}, {"y", 0.0}, {"z", 0.0}}}}}}}};
       }});
  load.push_back({"/helix/arbiter/status", 50.0, "HELIX arbiter.yaml rate_hz", 180,
                  [](std::uint64_t n, std::int64_t wall) {
                    return Json{{"reason", "SOURCE"},
                                {"selected_source", "nav"},
                                {"hold_active", false},
                                {"hold_fault_id", ""},
                                {"out_linear_x", 0.15},
                                {"out_linear_y", 0.0},
                                {"out_angular_z", 0.0},
                                {"seq", static_cast<std::int64_t>(n) + 1},
                                {"stamp", static_cast<double>(wall) / 1e9}};
                  }});
  load.push_back({"/helix/hold", 20.0, "HELIX recovery hold publisher (20 Hz)", 120,
                  [](std::uint64_t n, std::int64_t wall) {
                    return Json{{"hold", false},
                                {"fault_id", ""},
                                {"reason", ""},
                                {"epoch", 1},
                                {"seq", static_cast<std::int64_t>(n) + 1},
                                {"stamp", static_cast<double>(wall) / 1e9},
                                {"asserted_stamp", 0.0}};
                  }});
  load.push_back({"/nav/cmd_vel", 20.0, "navigation source as in the Replay Lab evidence", 48,
                  [](std::uint64_t, std::int64_t) { return twist(0.15, 0.0); }});
  return load;
}

double total_rate_hz(const std::vector<TopicLoad>& load) {
  double hz = 0.0;
  for (const auto& t : load) {
    hz += t.hz;
  }
  return hz;
}

GeneratorResult run_load(recorder::Recorder& rec, const std::vector<TopicLoad>& load, double scale,
                         std::chrono::nanoseconds duration, std::size_t latency_sample_every) {
  struct Next {
    Clock::time_point at;
    std::size_t topic;
    bool operator>(const Next& o) const { return at > o.at; }
  };
  std::vector<std::uint32_t> index(load.size(), UINT32_MAX);
  std::vector<std::chrono::nanoseconds> period(load.size());
  std::vector<std::uint64_t> sent(load.size(), 0);
  std::priority_queue<Next, std::vector<Next>, std::greater<>> q;
  const auto start = Clock::now();
  for (std::size_t i = 0; i < load.size(); ++i) {
    const auto idx = rec.topic_index(load[i].topic);
    if (!idx || !(load[i].hz * scale > 0)) {
      continue;
    }
    index[i] = *idx;
    period[i] = std::chrono::nanoseconds(static_cast<std::int64_t>(1e9 / (load[i].hz * scale)));
    q.push({start + std::chrono::nanoseconds(static_cast<std::int64_t>(i) * 97'000), i});
  }
  GeneratorResult res;
  const auto end = start + duration;
  std::uint64_t n = 0;
  while (!q.empty()) {
    Next next = q.top();
    q.pop();
    if (next.at >= end) {
      continue;
    }
    if (next.at > Clock::now()) {
      std::this_thread::sleep_until(next.at);
    }
    const std::size_t i = next.topic;
    recorder::MessageArrival m;
    m.topic = index[i];
    m.t_mono = clock_domain::Mono::now();
    m.t_wall = clock_domain::Wall::now();
    m.src = source_ns(count_ns(m.t_wall) - 400'000);
    m.rx = m.t_wall;
    m.payload =
        std::make_shared<JsonPayload>(load[i].make(sent[i], count_ns(m.t_wall)), load[i].bytes);
    ++sent[i];
    const bool sample = latency_sample_every != 0 && n % latency_sample_every == 0;
    const auto t0 = sample ? Clock::now() : Clock::time_point{};
    const auto r = rec.on_message(std::move(m));
    if (sample) {
      res.push_ns.push_back(
          std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - t0).count());
    }
    ++res.offered;
    (r == recorder::PushResult::ok ? res.accepted : res.rejected) += 1;
    ++n;
    next.at += period[i];
    q.push(next);
  }
  res.wall_s = std::chrono::duration<double>(Clock::now() - start).count();
  return res;
}

}  // namespace blackboxrs::bench
