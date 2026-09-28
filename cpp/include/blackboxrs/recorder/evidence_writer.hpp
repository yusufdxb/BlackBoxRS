// Evidence writer: the only thread that touches the evidence disk.
//
// One writer thread serves every bundle of a recording session. The recorder
// pipeline hands it operations (open, append, trigger, close) through a
// bounded queue; the pipeline waits for space rather than dropping, so any
// loss is taken, and counted, at the recorder's ingest queue with the topic
// it belongs to. The wait is bounded (stall_timeout): a writer stuck on a
// dead disk must not freeze the pipeline, so an operation that cannot be
// handed over in time is counted against its bundle as unwritten and the
// bundle closes as write_failed ("writer stalled"). While the writer is
// stalled, later operations are offered without waiting until one gets in.
//
// Per bundle, the writer:
//   * creates <session>/<bundle_id>.partial with manifest.json (status
//     "capturing") and appends records.jsonl, fsyncing at least every
//     fsync_every and at close;
//   * streams every byte through SHA-256 and a CRC-32C chunk table
//     (records.jsonl is never re-read, so a large bundle never stalls it);
//   * appends each completed chunk-table entry to integrity.chunks.tmp in
//     the bundle directory rather than keeping it in memory, so the writer's
//     memory does not grow with the length of a bundle;
//   * at close writes integrity.json (assembled from that file,
//     byte-identical to the in-memory form), removes the spool file, writes
//     the final manifest (each written to a temporary file, fsynced and
//     renamed), fsyncs the bundle directory, renames it to <bundle_id> and
//     fsyncs the session directory.
//
// Memory per open bundle is constant in the number of records. What does
// grow is the manifest's trigger list, about 1 KB per trigger attached to
// the bundle (measured): proportional to trigger events, not to records.
//
// A write error (ENOSPC, EFBIG, EIO) puts the bundle in status
// "write_failed": later records are counted as unwritten, integrity.json says
// complete=false, and the directory keeps its .partial name, so nothing marks
// damaged evidence as final.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <variant>
#include <vector>

#include "blackboxrs/evidence/record.hpp"
#include "blackboxrs/json.hpp"
#include "blackboxrs/recorder/bounded_queue.hpp"
#include "blackboxrs/recorder/flight_core.hpp"

namespace blackboxrs::recorder {

struct WriterOptions {
  std::filesystem::path session_dir;
  std::chrono::milliseconds fsync_every{1000};
  std::size_t chunk_records = 1024;
  std::size_t chunk_bytes = 1U << 20U;
  std::size_t queue_capacity = 65'536;
  std::chrono::milliseconds stall_timeout{2000};
  // Tests only: called on the writer thread before each operation (to
  // simulate a slow or stuck disk). Empty in production.
  std::function<void()> before_op;
};

// Everything the manifest states that does not change during a session.
struct ManifestContext {
  Json session;  // session id, host, recorder build, runtime mode ...
  Json profile;  // name, sha256, text, topics ...
  std::string config_sha256;
  Json writer_build;  // implementation, version, git_sha, build_type
};

struct WriterStats {
  std::uint64_t records_written = 0;
  std::uint64_t records_unwritten = 0;  // accepted but lost to a write error
  std::uint64_t bytes_written = 0;
  std::uint64_t write_errors = 0;
  std::uint64_t bundles_opened = 0;
  std::uint64_t bundles_finalized = 0;
  std::uint64_t bundles_failed = 0;
  std::uint64_t ops_dropped_stalled = 0;  // not handed over within stall_timeout
  std::uint64_t dir_sync_errors = 0;      // fsync of a directory failed after a rename
  std::int64_t last_lag_ns = 0;           // monotonic now minus the record's t_mono, at write
  std::int64_t max_lag_ns = 0;
  QueueStats queue;
};

class EvidenceWriter {
 public:
  EvidenceWriter(WriterOptions options, ManifestContext context);
  ~EvidenceWriter();
  EvidenceWriter(const EvidenceWriter&) = delete;
  EvidenceWriter& operator=(const EvidenceWriter&) = delete;
  EvidenceWriter(EvidenceWriter&&) = delete;
  EvidenceWriter& operator=(EvidenceWriter&&) = delete;

  // Messages lost before reaching the core whose arrival time lies in
  // [from_mono_ns, to_mono_ns].
  using LossBetween = std::function<std::uint64_t(std::int64_t, std::int64_t)>;

  // A FlightCore incident sink backed by this writer. `topic_status` and
  // `loss` are called on the FlightCore thread when the incident opens and
  // closes; the bundle's loss window runs from one second before its
  // pre-trigger window to its close.
  [[nodiscard]] std::unique_ptr<IncidentSink> make_sink(std::function<Json()> topic_status,
                                                        LossBetween loss);

  // Write session.json (atomically) on the writer thread.
  void write_session_file(Json session_state);

  // Drain every queued operation, finalize, join the thread. Idempotent.
  void finish();

  [[nodiscard]] WriterStats stats() const;
  [[nodiscard]] std::vector<std::string> finalized_bundles() const;
  [[nodiscard]] int thread_id() const noexcept { return tid_.load(); }

 private:
  struct OpOpen {
    std::string bundle_id;
    OrderedJson trigger;
    std::vector<RecordPtr> pre;
    Json pre_window;
    Json topic_status;
  };
  struct OpAppend {
    std::string bundle_id;
    RecordPtr record;
  };
  struct OpTrigger {
    std::string bundle_id;
    OrderedJson trigger;
  };
  struct OpClose {
    std::string bundle_id;
    std::string status;
    Json recorder_stats;
    Json topic_status;
  };
  struct OpSession {
    Json state;
  };
  using Op = std::variant<OpOpen, OpAppend, OpTrigger, OpClose, OpSession>;
  class Sink;
  struct Bundle;

  // False when the operation could not be handed over (writer stalled or finished).
  bool enqueue(Op op);
  void run(std::stop_token stop);
  void handle(Op& op);

  WriterOptions options_;
  ManifestContext context_;
  BoundedQueue<Op> queue_;
  std::map<std::string, std::unique_ptr<Bundle>> open_;  // writer thread only
  std::atomic<std::uint64_t> records_written_{0};
  std::atomic<std::uint64_t> records_unwritten_{0};
  std::atomic<std::uint64_t> bytes_written_{0};
  std::atomic<std::uint64_t> write_errors_{0};
  std::atomic<std::uint64_t> bundles_opened_{0};
  std::atomic<std::uint64_t> bundles_finalized_{0};
  std::atomic<std::uint64_t> bundles_failed_{0};
  std::atomic<std::uint64_t> ops_dropped_stalled_{0};
  std::atomic<std::uint64_t> dir_sync_errors_{0};
  std::atomic<bool> stalled_{false};
  // Per bundle: operations that never reached the writer thread. Written by
  // the pipeline when a hand-off fails, read by the writer when it closes
  // the bundle, so the count survives even if the close itself was lost.
  std::mutex stall_mu_;
  std::map<std::string, std::uint64_t> stall_drops_;
  std::atomic<std::int64_t> last_lag_ns_{0};
  std::atomic<std::int64_t> max_lag_ns_{0};
  mutable std::mutex finalized_mu_;
  std::vector<std::string> finalized_;
  std::atomic<bool> finished_{false};
  std::atomic<int> tid_{0};
  std::jthread thread_;  // last member: started after everything it uses exists
};

// Bundle id for an incident: inc_<UTC time of the trigger>_<trigger type>.
[[nodiscard]] std::string bundle_id_for(const Trigger& trigger);

}  // namespace blackboxrs::recorder
