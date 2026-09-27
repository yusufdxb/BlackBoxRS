// The recording pipeline, independent of ROS.
//
//   producers (ROS callbacks)  --try_push-->  IngestQueue (bounded)
//   pipeline thread            decode, records, FlightCore, ticks, samples
//   writer thread              EvidenceWriter: files, integrity, finalize
//
// Producers only stamp clocks and move a payload handle into the ingest
// queue; they never block, allocate nothing in the queue, and never throw.
// A full queue rejects the newest message and counts it against its topic.
// The pipeline thread owns everything else that changes (FlightCore, the
// decoder, per-topic bookkeeping), so none of it needs a lock; other threads
// see the recorder only through atomic counters and snapshots.
//
// Shutdown (stop()):
//   1. close the ingest queue: no new messages are accepted;
//   2. the pipeline drains what is queued until the queue is empty or the
//      drain deadline passes; anything left is counted as dropped at shutdown;
//   3. FlightCore closes the open incident (interrupted in triggered mode,
//      complete in continuous mode, complete_with_loss if messages were lost);
//   4. the writer drains its queue and finalizes every bundle (integrity
//      record, manifest, rename, fsync);
//   5. both threads are joined. Nothing is detached.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <variant>
#include <vector>

#include "blackboxrs/evidence/record.hpp"
#include "blackboxrs/profile.hpp"
#include "blackboxrs/recorder/bounded_queue.hpp"
#include "blackboxrs/recorder/evidence_writer.hpp"
#include "blackboxrs/recorder/flight_core.hpp"
#include "blackboxrs/recorder/system_sampler.hpp"

namespace blackboxrs::recorder {

// A received message payload. The ROS layer wraps the executor's serialized
// message (shared, not copied); tests and benchmarks use their own.
class Payload {
 public:
  Payload() = default;
  virtual ~Payload() = default;
  Payload(const Payload&) = delete;
  Payload& operator=(const Payload&) = delete;
  Payload(Payload&&) = delete;
  Payload& operator=(Payload&&) = delete;
  [[nodiscard]] virtual std::size_t size_bytes() const noexcept = 0;
};
using PayloadPtr = std::shared_ptr<const Payload>;

struct DecodeResult {
  std::optional<Json> data;          // the profile's fields (or the whole message)
  std::vector<std::string> missing;  // profile fields the message does not have
  std::string error;                 // set when data is empty
};

// Turns a payload into record data. Runs on the pipeline thread only.
class MessageDecoder {
 public:
  MessageDecoder() = default;
  virtual ~MessageDecoder() = default;
  MessageDecoder(const MessageDecoder&) = delete;
  MessageDecoder& operator=(const MessageDecoder&) = delete;
  MessageDecoder(MessageDecoder&&) = delete;
  MessageDecoder& operator=(MessageDecoder&&) = delete;
  virtual DecodeResult decode(std::size_t topic_index, const Payload& payload) = 0;
};

struct MessageArrival {
  std::uint32_t topic = 0;  // index into Profile::topics
  MonoTime t_mono{};
  WallTime t_wall{};
  std::optional<RosTime> t_ros;
  std::optional<SourceTime> src;
  std::optional<WallTime> rx;
  PayloadPtr payload;
};

struct GraphSnapshot {
  MonoTime t_mono{};
  WallTime t_wall{};
  std::vector<std::string> nodes;
  std::map<std::string, std::vector<std::string>> publishers;
  Json topics = Json::object();
  // Per profile topic: status, reason, publishers, graph_types, qos, message_lost.
  std::map<std::string, Json> topic_status;
};

struct MarkerRequest {
  MonoTime t_mono{};
  WallTime t_wall{};
  std::string note;
  std::string source;
};

using IngestItem = std::variant<MessageArrival, GraphSnapshot, MarkerRequest>;

struct RecorderConfig {
  std::size_t ingest_capacity = 65'536;
  std::size_t control_reserve = 256;
  std::size_t batch = 512;
  std::chrono::milliseconds drain_deadline{3000};
  std::chrono::milliseconds session_file_every{5000};
  std::int64_t hard_disk_floor_mb = 256;
  bool continuous = false;
  bool sample_system = true;
  WriterOptions writer;  // writer.session_dir is the session directory
  ManifestContext manifest;
};

struct TopicMetrics {
  std::string name;
  std::uint64_t received = 0;        // reached the recorder (callback)
  std::uint64_t dropped_ingest = 0;  // rejected by a full ingest queue
  std::uint64_t decode_errors = 0;
};

struct RecorderMetrics {
  std::string state;  // recording, draining, stopped, failed
  QueueStats ingest;
  WriterStats writer;
  std::uint64_t received = 0;
  std::uint64_t dropped_ingest = 0;
  std::uint64_t dropped_at_shutdown = 0;
  std::uint64_t rejected_after_stop = 0;
  std::uint64_t decoded = 0;
  std::uint64_t decode_errors = 0;
  std::uint64_t decimated = 0;
  std::uint64_t processed = 0;  // records handed to FlightCore
  std::vector<TopicMetrics> topics;
  Json core = Json::object();    // FlightCore statistics (last snapshot)
  Json system = Json::object();  // last system sample
  double uptime_s = 0.0;
  std::string fatal_error;

  [[nodiscard]] Json to_json() const;
};

class Recorder {
 public:
  Recorder(Profile profile, RecorderConfig config, std::unique_ptr<MessageDecoder> decoder);
  ~Recorder();
  Recorder(const Recorder&) = delete;
  Recorder& operator=(const Recorder&) = delete;
  Recorder(Recorder&&) = delete;
  Recorder& operator=(Recorder&&) = delete;

  // Producer side: any thread, never blocks, never throws.
  PushResult on_message(MessageArrival&& m) noexcept;
  void on_graph(GraphSnapshot g) noexcept;
  void mark(std::string note, std::string source) noexcept;

  // Stop, drain, finalize, join (see the file comment). Idempotent.
  void stop(const std::string& reason = "recorder_stopped");

  [[nodiscard]] RecorderMetrics metrics() const;
  [[nodiscard]] const Profile& profile() const noexcept { return profile_; }
  [[nodiscard]] std::optional<std::uint32_t> topic_index(std::string_view name) const noexcept;
  [[nodiscard]] std::vector<std::string> bundles() const { return writer_.finalized_bundles(); }
  // Kernel thread ids of the pipeline and writer threads (0 until started),
  // for per-thread CPU accounting from /proc/self/task/<tid>/stat.
  [[nodiscard]] std::pair<int, int> thread_ids() const noexcept {
    return {pipeline_tid_.load(), writer_.thread_id()};
  }

 private:
  void run(std::stop_token stop);
  void handle(IngestItem& item);
  void handle_message(MessageArrival& m);
  void periodic(bool force);
  [[nodiscard]] Json topic_status_json() const;  // pipeline thread
  [[nodiscard]] std::uint64_t total_dropped() const noexcept;

  const Profile profile_;
  const RecorderConfig config_;
  const std::chrono::steady_clock::time_point started_;
  // Producer-shared state: fixed at construction, atomics only.
  std::vector<std::atomic<std::uint64_t>> received_;
  std::vector<std::atomic<std::uint64_t>> dropped_;
  std::atomic<std::uint64_t> rejected_after_stop_{0};
  BoundedQueue<IngestItem> ingest_;
  EvidenceWriter writer_;
  // Pipeline-thread state.
  std::unique_ptr<MessageDecoder> decoder_;
  std::unique_ptr<FlightCore> core_;
  SystemSampler sampler_;
  std::vector<std::int64_t> store_period_ns_;
  std::vector<std::optional<std::int64_t>> last_stored_;
  std::map<std::string, Json> topic_status_;
  std::vector<std::uint64_t> decode_errors_;
  std::chrono::steady_clock::time_point next_tick_;
  std::chrono::steady_clock::time_point next_sample_;
  std::chrono::steady_clock::time_point next_session_file_;
  // Counters read by other threads.
  std::atomic<std::uint64_t> decoded_{0};
  std::atomic<std::uint64_t> decode_error_total_{0};
  std::atomic<std::uint64_t> decimated_{0};
  std::atomic<std::uint64_t> processed_{0};
  std::atomic<std::uint64_t> dropped_at_shutdown_{0};
  std::atomic<int> state_{0};
  std::atomic<int> pipeline_tid_{0};  // 0 recording, 1 draining, 2 stopped, 3 failed
  mutable std::mutex snapshot_mu_;
  Json core_snapshot_ = Json::object();
  Json system_snapshot_ = Json::object();
  std::string fatal_error_;
  std::string stop_reason_ = "recorder_stopped";
  std::once_flag stop_once_;
  std::jthread pipeline_;  // last member: started after everything it uses exists
};

}  // namespace blackboxrs::recorder
