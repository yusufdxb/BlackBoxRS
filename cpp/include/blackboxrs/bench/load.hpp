// Synthetic GO2 + HELIX message load for benchmarks and compatibility tests.
//
// GENERATED data: every bundle recorded from it says synthetic. The rates are
// not invented targets:
//   * GO2 topics use the rates measured on a live GO2 EDU with the Orin NX
//     payload (docs/go2_field_notes.md on the capture-plane branch, and the
//     2026-09-18 payload run): /lowstate 500 Hz, /sportmodestate 295 Hz,
//     /utlidar/robot_odom 151 Hz;
//   * HELIX topics use HELIX's own configuration (arbiter.yaml rate_hz 50, the
//     recovery hold publisher at 20 Hz), not a measurement;
//   * a navigation command source at 20 Hz, as in the Replay Lab evidence.
// "1x" is that set. Scaling multiplies every rate.
//
// Each payload is built when it is sent (the generator's cost is on the
// producer thread; per-thread CPU figures for the recorder exclude it).
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "blackboxrs/json.hpp"
#include "blackboxrs/recorder/recorder.hpp"

namespace blackboxrs::bench {

struct TopicLoad {
  std::string topic;
  double hz = 0.0;
  std::string rate_source;  // where the rate comes from
  std::size_t bytes = 0;    // approximate CDR size of the message type
  // Payload of the n-th message, stamped at `wall_ns`: sequence numbers
  // increase and stamps move forward, as a real publisher's would.
  std::function<Json(std::uint64_t n, std::int64_t wall_ns)> make;
};

// A payload that already is decoded JSON (what the recorder's decoder
// would produce from CDR); `bytes` approximates the serialized size.
class JsonPayload final : public recorder::Payload {
 public:
  JsonPayload(Json j, std::size_t bytes) : json_(std::move(j)), bytes_(bytes) {}
  [[nodiscard]] std::size_t size_bytes() const noexcept override { return bytes_; }
  [[nodiscard]] const Json& json() const noexcept { return json_; }

 private:
  Json json_;
  std::size_t bytes_;
};

// Decoder for JsonPayload: returns a copy of the JSON (the copy is the cost
// that stands in for CDR decoding in ROS-free benchmarks).
class JsonPayloadDecoder final : public recorder::MessageDecoder {
 public:
  recorder::DecodeResult decode(std::size_t topic, const recorder::Payload& p) override;
};

// The go2_helix topic set at 1x.
[[nodiscard]] std::vector<TopicLoad> go2_helix_load();
[[nodiscard]] double total_rate_hz(const std::vector<TopicLoad>& load);

struct GeneratorResult {
  std::uint64_t offered = 0;
  std::uint64_t accepted = 0;
  std::uint64_t rejected = 0;
  std::vector<std::int64_t> push_ns;  // duration of each on_message call (sampled)
  double wall_s = 0.0;
};

// Offer `load` scaled by `scale` to `rec` for `duration`, pacing each topic on
// an absolute schedule so a late wakeup is caught up, not drifted. Topics the
// recorder's profile does not list are skipped. Single producer thread.
[[nodiscard]] GeneratorResult run_load(recorder::Recorder& rec, const std::vector<TopicLoad>& load,
                                       double scale, std::chrono::nanoseconds duration,
                                       std::size_t latency_sample_every = 16);

}  // namespace blackboxrs::bench
