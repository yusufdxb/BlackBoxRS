#include "blackboxrs/recorder/evidence_writer.hpp"

#include <fcntl.h>
#include <sys/uio.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cstring>
#include <ctime>
#include <limits>
#include <system_error>
#include <vector>

#include "blackboxrs/evidence/bundle.hpp"
#include "blackboxrs/evidence/integrity_record.hpp"
#include "blackboxrs/integrity.hpp"

namespace blackboxrs::recorder {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

namespace {

// Owning POSIX file descriptor.
class UniqueFd {
 public:
  UniqueFd() = default;
  explicit UniqueFd(int fd) : fd_(fd) {}
  ~UniqueFd() { reset(); }
  UniqueFd(UniqueFd&& o) noexcept : fd_(std::exchange(o.fd_, -1)) {}
  UniqueFd& operator=(UniqueFd&& o) noexcept {
    if (this != &o) {
      reset();
      fd_ = std::exchange(o.fd_, -1);
    }
    return *this;
  }
  UniqueFd(const UniqueFd&) = delete;
  UniqueFd& operator=(const UniqueFd&) = delete;
  [[nodiscard]] int get() const noexcept { return fd_; }
  [[nodiscard]] bool valid() const noexcept { return fd_ >= 0; }
  // Close and report the error close() gives (it can carry a delayed write error).
  std::error_code close() {
    if (fd_ < 0) {
      return {};
    }
    const int rc = ::close(std::exchange(fd_, -1));
    return rc == 0 ? std::error_code{} : std::error_code(errno, std::generic_category());
  }
  void reset() noexcept {
    if (fd_ >= 0) {
      ::close(std::exchange(fd_, -1));
    }
  }

 private:
  int fd_ = -1;
};

std::error_code write_all(int fd, std::string_view data) {
  while (!data.empty()) {
    const ssize_t n = ::write(fd, data.data(), data.size());
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      return {errno, std::generic_category()};
    }
    data.remove_prefix(static_cast<std::size_t>(n));
  }
  return {};
}

// records.jsonl writes: one writev covers at most this many iovecs (two per
// record: its line and the shared newline) and about kMaxWriteBytes; a
// single longer record is written on its own.
constexpr std::size_t kMaxIov = std::min<std::size_t>(IOV_MAX, 1024);
constexpr std::size_t kMaxLinesPerWrite = kMaxIov / 2;
constexpr std::size_t kMaxWriteBytes = 1U << 20U;
// The newline after every record, referenced by every writev instead of
// being copied after each line. writev only reads it.
constexpr std::string_view kNewline = "\n";

void* iov_base(std::string_view s) {
  // iovec has one pointer type for reading and writing; writev only reads.
  return const_cast<char*>(s.data());  // NOLINT(cppcoreguidelines-pro-type-const-cast)
}

std::error_code fsync_fd(int fd) {
  return ::fsync(fd) == 0 ? std::error_code{} : std::error_code(errno, std::generic_category());
}

std::error_code fsync_dir(const fs::path& dir) {
  UniqueFd fd(::open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
  if (!fd.valid()) {
    return {errno, std::generic_category()};
  }
  return fsync_fd(fd.get());
}

// Write `text` to `path` atomically: temporary file, fsync, rename.
std::error_code write_file_atomic(const fs::path& path, const std::string& text) {
  const fs::path tmp = path.string() + ".tmp";
  UniqueFd fd(::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644));
  if (!fd.valid()) {
    return {errno, std::generic_category()};
  }
  if (auto ec = write_all(fd.get(), text); ec) {
    return ec;
  }
  if (auto ec = fsync_fd(fd.get()); ec) {
    return ec;
  }
  if (auto ec = fd.close(); ec) {
    return ec;
  }
  if (::rename(tmp.c_str(), path.c_str()) != 0) {
    return {errno, std::generic_category()};
  }
  return {};
}

// Append `src` (whole file) to the open descriptor `fd`.
std::error_code copy_file_to(const fs::path& src, int fd) {
  UniqueFd in(::open(src.c_str(), O_RDONLY | O_CLOEXEC));
  if (!in.valid()) {
    return {errno, std::generic_category()};
  }
  std::vector<char> buf(1U << 16U);
  while (true) {
    const ssize_t n = ::read(in.get(), buf.data(), buf.size());
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      return {errno, std::generic_category()};
    }
    if (n == 0) {
      return {};
    }
    if (auto ec = write_all(fd, std::string_view(buf.data(), static_cast<std::size_t>(n))); ec) {
      return ec;
    }
  }
}

// integrity.json from `record` (counts, hashes) and the spooled chunk table,
// written atomically like every other bundle file. Byte-identical to
// write_file_atomic(path, record-with-chunks.to_json().dump(2) + "\n").
std::error_code write_integrity_streamed(const fs::path& path, const IntegrityRecord& record,
                                         const fs::path& chunks, std::uint64_t n_chunks) {
  const IntegrityText text = integrity_text_around_chunks(record);
  const fs::path tmp = path.string() + ".tmp";
  UniqueFd fd(::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644));
  if (!fd.valid()) {
    return {errno, std::generic_category()};
  }
  if (auto ec = write_all(fd.get(), text.head); ec) {
    return ec;
  }
  if (n_chunks == 0) {
    if (auto ec = write_all(fd.get(), kIntegrityChunksEmpty); ec) {
      return ec;
    }
  } else {
    if (auto ec = write_all(fd.get(), kIntegrityChunksOpen); ec) {
      return ec;
    }
    if (auto ec = copy_file_to(chunks, fd.get()); ec) {
      return ec;
    }
    if (auto ec = write_all(fd.get(), kIntegrityChunksClose); ec) {
      return ec;
    }
  }
  if (auto ec = write_all(fd.get(), text.tail); ec) {
    return ec;
  }
  if (auto ec = fsync_fd(fd.get()); ec) {
    return ec;
  }
  if (auto ec = fd.close(); ec) {
    return ec;
  }
  if (::rename(tmp.c_str(), path.c_str()) != 0) {
    return {errno, std::generic_category()};
  }
  return {};
}

std::string describe(const std::error_code& ec) {
  const char* name = nullptr;
  switch (ec.value()) {
    case ENOSPC: name = "ENOSPC"; break;
    case EFBIG: name = "EFBIG"; break;
    case EIO: name = "EIO"; break;
    case EDQUOT: name = "EDQUOT"; break;
    case EROFS: name = "EROFS"; break;
    case EACCES: name = "EACCES"; break;
    default: break;
  }
  return (name != nullptr ? std::string(name) + ": " : std::string()) + ec.message();
}

std::int64_t now_mono_ns() {
  return count_ns(clock_domain::Mono::now());
}

// A manifest or a state file as text. Strings from the robot (a hold reason,
// a decode error, an operator note) may hold invalid UTF-8: it is replaced
// by U+FFFD instead of throwing, as the record writer does.
std::string dump_text(const Json& j) {
  return j.dump(2, ' ', false, Json::error_handler_t::replace) + "\n";
}

// OrderedJson to Json. The dump cannot throw (invalid UTF-8 is replaced, a
// non-finite number becomes null), so neither can the parse.
Json plain(const OrderedJson& j) {
  return Json::parse(j.dump(-1, ' ', false, OrderedJson::error_handler_t::replace));
}

// The bundle an operation belongs to (nullptr for session-level operations).
template <class Variant>
const std::string* bundle_of(const Variant& op) {
  return std::visit(
      [](const auto& o) -> const std::string* {
        if constexpr (requires { o.bundle_id; }) {
          return &o.bundle_id;
        } else {
          return nullptr;
        }
      },
      op);
}

// {"kind": "trigger", **trigger} as one JSON line (Python-compatible floats).
std::string trigger_line(const OrderedJson& trigger) {
  OrderedJson r = OrderedJson::object();
  r["kind"] = "trigger";
  for (auto it = trigger.begin(); it != trigger.end(); ++it) {
    r[it.key()] = it.value();
  }
  std::string out;
  write_json_any(out, r);
  return out;
}

}  // namespace

std::string bundle_id_for(const Trigger& trigger) {
  const std::int64_t ns = count_ns(trigger.t_wall);
  const std::time_t sec = static_cast<std::time_t>(ns / 1'000'000'000);
  const auto micros = static_cast<long>((ns % 1'000'000'000) / 1000);
  std::tm tm{};
  gmtime_r(&sec, &tm);
  char buf[64];
  std::snprintf(buf, sizeof buf, "%04d%02d%02dT%02d%02d%02d.%06ldZ", tm.tm_year + 1900,
                tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, micros);
  return "inc_" + std::string(buf) + "_" + trigger.type;
}

// ---------------------------------------------------------------------------
// per-bundle state (writer thread only)
// ---------------------------------------------------------------------------

struct EvidenceWriter::Bundle {
  std::string id;
  fs::path partial_dir;
  fs::path final_dir;
  UniqueFd fd;
  Sha256 sha;
  IntegrityRecord integrity;  // counts and hashes only; its `chunks` stays empty
  ChunkEntry chunk;
  bool chunk_open = false;
  // Completed chunk-table entries, already in their integrity.json text form,
  // are appended to this file instead of kept in memory: the table grows by
  // one entry per chunk for as long as the bundle is open (a continuous
  // capture is open for the whole session). integrity.json is assembled from
  // it at close, and it is removed before the bundle is renamed.
  fs::path chunks_path;
  UniqueFd chunks_fd;
  std::uint64_t chunks_spooled = 0;
  std::uint64_t unwritten = 0;
  std::uint64_t write_errors = 0;
  std::string first_error;
  Clock::time_point last_sync = Clock::now();
  Json triggers = Json::array();
  Json pre_window;
  Json topic_status;

  [[nodiscard]] bool failed() const noexcept { return write_errors != 0; }
};

// ---------------------------------------------------------------------------
// FlightCore sink: runs on the pipeline thread, only enqueues
// ---------------------------------------------------------------------------

class EvidenceWriter::Sink final : public IncidentSink {
 public:
  Sink(EvidenceWriter& w, std::function<Json()> topic_status, LossBetween loss)
      : w_(w), topic_status_(std::move(topic_status)), loss_(std::move(loss)) {}

  std::string open(const Trigger& primary, std::vector<RecordPtr> pre,
                   const PreWindow& window) override {
    id_ = bundle_id_for(primary);
    // Everything that could have belonged in this bundle: its pre-trigger
    // window, the ring's extra second, and whatever arrived while the trigger
    // waited in the ingest queue.
    loss_from_ = count_ns(primary.t_mono) - static_cast<std::int64_t>(window.requested_s * 1e9) -
                 1'000'000'000;
    const Json pw = {{"requested_s", window.requested_s},
                     {"available_s", window.available_s},
                     {"evicted_by_cap_in_window", window.evicted_by_cap_in_window}};
    submit(OpOpen{id_, primary.to_json(), std::move(pre), pw,
                  topic_status_ ? topic_status_() : Json::object()});
    return id_;
  }
  void append(const RecordPtr& record) override { submit(OpAppend{id_, record}); }
  void add_trigger(const Trigger& trigger) override { submit(OpTrigger{id_, trigger.to_json()}); }
  std::optional<std::string> close(const std::string& status, const Json& stats) override {
    const std::uint64_t lost = loss_ ? loss_(loss_from_, now_mono_ns()) : 0;
    Json s = stats;
    s["messages_lost_before_core_during_bundle"] = lost;
    s["writer_ops_dropped_stalled"] = dropped_;
    // A bundle that is otherwise complete but lost messages before the core
    // (ingest queue, DDS, shutdown) says so in its status, so it is never
    // read as complete.
    const std::string final_status =
        status == "complete" && lost != 0 ? "complete_with_loss" : status;
    (void)w_.enqueue(
        OpClose{id_, final_status, s, topic_status_ ? topic_status_() : Json::object()});
    return (w_.options_.session_dir / id_).string();
  }

 private:
  void submit(Op op) {
    if (!w_.enqueue(std::move(op))) {
      ++dropped_;
    }
  }

  EvidenceWriter& w_;
  std::function<Json()> topic_status_;
  LossBetween loss_;
  std::int64_t loss_from_ = 0;
  std::uint64_t dropped_ = 0;
  std::string id_;
};

// ---------------------------------------------------------------------------
// writer
// ---------------------------------------------------------------------------

EvidenceWriter::EvidenceWriter(WriterOptions options, ManifestContext context)
    : options_(std::move(options)),
      context_(std::move(context)),
      queue_(options_.queue_capacity),
      thread_([this](std::stop_token st) { run(std::move(st)); }) {}

EvidenceWriter::~EvidenceWriter() {
  finish();
}

std::unique_ptr<IncidentSink> EvidenceWriter::make_sink(std::function<Json()> topic_status,
                                                        LossBetween loss) {
  return std::make_unique<Sink>(*this, std::move(topic_status), std::move(loss));
}

void EvidenceWriter::write_session_file(Json session_state) {
  enqueue(OpSession{std::move(session_state)});
}

bool EvidenceWriter::enqueue(Op op) {
  // Back-pressure, not loss: the pipeline waits here when the disk is slow,
  // and the ingest queue in front of it counts what it cannot take. The wait
  // is bounded so a stuck writer cannot freeze the pipeline; once stalled,
  // later operations are offered without waiting until one gets through.
  // A close always gets the full wait: it is once per bundle and carries
  // the bundle's final status.
  const bool is_close = std::holds_alternative<OpClose>(op);
  const auto deadline = stalled_.load(std::memory_order_relaxed) && !is_close
                            ? Clock::now()
                            : Clock::now() + options_.stall_timeout;
  const PushResult r = queue_.push_wait_until(op, deadline);
  if (r == PushResult::ok) {
    stalled_.store(false, std::memory_order_relaxed);
    return true;
  }
  if (r == PushResult::full) {
    stalled_.store(true, std::memory_order_relaxed);
    ops_dropped_stalled_.fetch_add(1);
  }
  // Stalled, or after finish(): this operation will never be written.
  records_unwritten_.fetch_add(std::holds_alternative<OpAppend>(op) ? 1U : 0U);
  if (r == PushResult::full) {
    const std::string* id = bundle_of(op);
    if (id != nullptr) {
      std::lock_guard lock(stall_mu_);
      ++stall_drops_[*id];
    }
  }
  return false;
}

void EvidenceWriter::finish() {
  if (finished_.exchange(true)) {
    return;
  }
  queue_.close();  // the thread drains everything queued before it exits
  if (thread_.joinable()) {
    thread_.join();
  }
}

void EvidenceWriter::run(std::stop_token stop) {
  tid_.store(static_cast<int>(::gettid()));
  std::vector<Op> batch;
  batch.reserve(512);
  while (true) {
    batch.clear();
    const std::size_t n =
        queue_.pop_batch(batch, 512, Clock::now() + std::chrono::milliseconds(200), stop);
    // Consecutive appends to one bundle are written together; any other
    // operation, or an append to another bundle, ends the run. Only what was
    // already dequeued is grouped: nothing waits for a batch to fill.
    for (std::size_t i = 0; i < n;) {
      std::size_t j = i + 1;
      if (const auto* a = std::get_if<OpAppend>(&batch[i])) {
        while (j < n && j - i < kMaxLinesPerWrite) {
          const auto* next = std::get_if<OpAppend>(&batch[j]);
          if (next == nullptr || next->bundle_id != a->bundle_id) {
            break;
          }
          ++j;
        }
      }
      process(std::span<Op>(batch).subspan(i, j - i));
      i = j;
    }
    // Periodic fsync of open bundles even when idle.
    for (auto& [id, b] : open_) {
      if (b->fd.valid() && !b->failed() && Clock::now() - b->last_sync >= options_.fsync_every) {
        if (auto ec = fsync_fd(b->fd.get()); ec) {
          ++b->write_errors;
          write_errors_.fetch_add(1);
          b->first_error = b->first_error.empty() ? describe(ec) : b->first_error;
        }
        b->last_sync = Clock::now();
      }
    }
    if (n == 0 && queue_.stats().closed && queue_.stats().depth == 0) {
      break;
    }
  }
  // Anything still open at exit was never closed by the core: finalize it as
  // interrupted rather than leaving a capturing manifest behind.
  std::vector<std::string> ids;
  for (const auto& [id, b] : open_) {
    ids.push_back(id);
  }
  for (const auto& id : ids) {
    Op close = OpClose{id, "interrupted", Json::object(), Json::object()};
    try {
      handle(close);
    } catch (const std::exception&) {
      // The bundle keeps its .partial name: never final, never complete.
      write_errors_.fetch_add(1);
      bundles_failed_.fetch_add(1);
      open_.erase(id);
    }
  }
}

void EvidenceWriter::process(std::span<Op> ops) {
  Bundle* bundle = nullptr;
  std::uint64_t accounted_before = 0;
  const bool appends = std::holds_alternative<OpAppend>(ops.front());
  if (const std::string* id = bundle_of(ops.front()); id != nullptr) {
    if (auto it = open_.find(*id); it != open_.end()) {
      bundle = it->second.get();
      accounted_before = bundle->integrity.records + bundle->unwritten;
    }
  }
  try {
    if (options_.before_op) {
      for (std::size_t k = 0; k < ops.size(); ++k) {
        options_.before_op();
      }
    }
    if (appends) {
      append_run(ops);
    } else {
      handle(ops.front());
    }
  } catch (const std::exception& exc) {
    // A bug or an unexpected library error must not take the evidence
    // thread down silently: count it against its bundle (whatever the
    // operation was) so that bundle can only close as write_failed. Appends
    // the run had not accounted for yet are unwritten.
    write_errors_.fetch_add(1);
    std::uint64_t lost = 0;
    if (appends) {
      const std::uint64_t accounted =
          bundle != nullptr ? bundle->integrity.records + bundle->unwritten - accounted_before : 0;
      lost = ops.size() - std::min<std::uint64_t>(accounted, ops.size());
      records_unwritten_.fetch_add(lost);
    }
    if (const std::string* id = bundle_of(ops.front()); id != nullptr) {
      if (auto it = open_.find(*id); it != open_.end()) {
        ++it->second->write_errors;
        it->second->unwritten += lost;
        if (it->second->first_error.empty()) {
          it->second->first_error = std::string("internal: ") + exc.what();
        }
      }
    }
  }
}

void EvidenceWriter::fail(Bundle& b, const std::string& what, const std::error_code& ec) {
  ++b.write_errors;
  write_errors_.fetch_add(1);
  if (b.first_error.empty()) {
    b.first_error = what + ": " + describe(ec);
  }
}

// One completed chunk to the on-disk chunk table.
void EvidenceWriter::spool_chunk(Bundle& b) {
  b.chunk_open = false;
  if (!b.chunks_fd.valid()) {
    fail(b, "write integrity chunk table", std::error_code(EBADF, std::generic_category()));
    return;
  }
  std::string entry;
  if (b.chunks_spooled != 0) {
    entry = kIntegrityChunksSeparator;
  }
  entry += integrity_chunk_text(b.chunk);
  if (auto ec = write_all(b.chunks_fd.get(), entry); ec) {
    fail(b, "write integrity chunk table", ec);
    return;
  }
  ++b.chunks_spooled;
}

void EvidenceWriter::append_run(std::span<Op> appends) {
  const std::string& id = std::get<OpAppend>(appends.front()).bundle_id;
  const auto it = open_.find(id);
  if (it == open_.end()) {
    records_unwritten_.fetch_add(appends.size());
    return;
  }
  // The records stay owned by their operations until the run is written.
  lines_.clear();
  for (const Op& op : appends) {
    const Record& r = *std::get<OpAppend>(op).record;
    lines_.push_back(Line{r.line, r.seq, count_ns(r.t_mono)});
  }
  write_lines(*it->second, lines_);
}

// One record that is completely in records.jsonl: hash, chunk table, counts.
// Exactly what a write of the line plus its newline accounted for before
// records were written together, in the same order, so chunk boundaries,
// hashes and counts do not depend on how the records were grouped.
void EvidenceWriter::account(Bundle& b, const Line& line, std::int64_t now_ns,
                             std::int64_t& max_lag) {
  const std::size_t len = line.text.size() + kNewline.size();
  b.sha.update(line.text);
  b.sha.update(kNewline);
  if (!b.chunk_open) {
    b.chunk = ChunkEntry{b.integrity.bytes, 0, 0, line.seq, line.seq, 0};
    b.chunk_open = true;
  }
  b.chunk.length += len;
  b.chunk.crc32c = crc32c_extend(crc32c_extend(b.chunk.crc32c, line.text), kNewline);
  ++b.chunk.records;
  b.chunk.last_seq = line.seq;
  if (b.chunk.records >= options_.chunk_records || b.chunk.length >= options_.chunk_bytes) {
    spool_chunk(b);
  }
  b.integrity.bytes += len;
  ++b.integrity.records;
  if (!b.integrity.first_seq) {
    b.integrity.first_seq = line.seq;
  }
  b.integrity.last_seq = line.seq;
  if (line.t_mono) {
    if (!b.integrity.first_t_mono_ns) {
      b.integrity.first_t_mono_ns = line.t_mono;
    }
    b.integrity.last_t_mono_ns = line.t_mono;
    const std::int64_t lag = now_ns - *line.t_mono;
    last_lag_ns_.store(lag, std::memory_order_relaxed);
    max_lag = std::max(max_lag, lag);
  }
}

// Append `lines` to records.jsonl in order, in bounded writev calls. A short
// write resumes at the next unwritten byte, even inside a line. Only lines
// that reached the file completely are accounted for; after a write error
// the line it interrupted and every later one are unwritten, as is anything
// for a bundle that has already failed.
void EvidenceWriter::write_lines(Bundle& b, std::span<const Line> lines) {
  std::array<iovec, kMaxIov> iov{};
  std::size_t next = 0;  // first line not accounted for yet
  while (next < lines.size()) {
    if (!b.fd.valid() || b.failed()) {
      const std::size_t rest = lines.size() - next;
      b.unwritten += rest;
      records_unwritten_.fetch_add(rest);
      return;
    }
    // The group [next, end): bounded by iovecs and (except for its first
    // line) bytes.
    std::size_t end = next;
    std::size_t group_bytes = 0;
    int count = 0;
    while (end < lines.size() && end - next < kMaxLinesPerWrite) {
      const std::size_t len = lines[end].text.size() + kNewline.size();
      if (end != next && group_bytes + len > kMaxWriteBytes) {
        break;
      }
      iov[static_cast<std::size_t>(count++)] = {iov_base(lines[end].text), lines[end].text.size()};
      iov[static_cast<std::size_t>(count++)] = {iov_base(kNewline), kNewline.size()};
      group_bytes += len;
      ++end;
    }
    std::size_t done = 0;  // bytes of the group in the file
    std::error_code ec;
    iovec* cur = iov.data();
    while (count > 0) {
      const ssize_t w = options_.records_writev ? options_.records_writev(b.fd.get(), cur, count)
                                                : ::writev(b.fd.get(), cur, count);
      if (w < 0) {
        if (errno == EINTR) {
          continue;
        }
        ec = std::error_code(errno, std::generic_category());
        break;
      }
      auto left = static_cast<std::size_t>(w);
      done += left;
      while (count > 0 && left >= cur->iov_len) {
        left -= cur->iov_len;
        ++cur;
        --count;
      }
      if (left != 0) {  // stopped inside an iovec: resume at its next byte
        cur->iov_base = static_cast<char*>(cur->iov_base) + left;
        cur->iov_len -= left;
      }
    }
    const std::int64_t now_ns = now_mono_ns();
    std::int64_t max_lag = std::numeric_limits<std::int64_t>::min();
    std::size_t complete_bytes = 0;
    std::size_t k = next;
    for (; k < end; ++k) {
      const std::size_t len = lines[k].text.size() + kNewline.size();
      if (complete_bytes + len > done) {
        break;
      }
      account(b, lines[k], now_ns, max_lag);
      complete_bytes += len;
    }
    records_written_.fetch_add(k - next, std::memory_order_relaxed);
    bytes_written_.fetch_add(complete_bytes, std::memory_order_relaxed);
    std::int64_t prev = max_lag_ns_.load(std::memory_order_relaxed);
    while (max_lag > prev && !max_lag_ns_.compare_exchange_weak(prev, max_lag)) {
    }
    next = k;
    if (ec) {
      fail(b, "write records.jsonl", ec);
      continue;  // the rest are unwritten (top of the loop)
    }
    if (Clock::now() - b.last_sync >= options_.fsync_every) {
      if (auto e2 = fsync_fd(b.fd.get()); e2) {
        fail(b, "fsync records.jsonl", e2);
      }
      b.last_sync = Clock::now();
    }
  }
}

void EvidenceWriter::handle(Op& op) {
  auto manifest = [&](const Bundle& b, const std::string& status, const Json& stats) {
    Json m;
    m["schema"] = kManifestSchema;
    m["bundle_id"] = b.id;
    m["status"] = status;
    m["session"] = context_.session;
    m["profile"] = context_.profile;
    m["triggers"] = b.triggers;
    m["pre_window"] = b.pre_window;
    m["topic_status"] = b.topic_status;
    m["recorder_stats"] = stats;
    Json writer = context_.writer_build;
    writer["records_written"] = b.integrity.records;
    writer["records_unwritten"] = b.unwritten;
    writer["write_errors"] = b.write_errors;
    writer["first_write_error"] = b.first_error.empty() ? Json() : Json(b.first_error);
    m["writer"] = writer;
    m["config_sha256"] = context_.config_sha256;
    m["finalized_wall_ns"] =
        status == "capturing" ? Json() : Json(count_ns(clock_domain::Wall::now()));
    if (status != "capturing") {
      m["integrity"] = {{"file", "integrity.json"},
                        {"records", b.integrity.records},
                        {"sha256", b.integrity.sha256},
                        {"complete", b.integrity.complete}};
    }
    return dump_text(m);
  };

  if (auto* o = std::get_if<OpOpen>(&op)) {
    // Registered first, so an exception below is attributed to this bundle.
    auto [slot, inserted] = open_.emplace(o->bundle_id, std::make_unique<Bundle>());
    if (!inserted) {
      // Same id as a bundle still open: FlightCore opens one at a time, so a
      // bug. Keep the open one; count this one as failed.
      write_errors_.fetch_add(1);
      bundles_failed_.fetch_add(1);
      records_unwritten_.fetch_add(o->pre.size() + 1);
      return;
    }
    Bundle* b = slot->second.get();
    b->id = o->bundle_id;  // FlightCore and the sink know it by this name
    std::string id = o->bundle_id;
    // Two incidents in one microsecond of one type: keep both, never overwrite.
    for (int k = 2; fs::exists(options_.session_dir / id) ||
                    fs::exists(options_.session_dir / (id + ".partial"));
         ++k) {
      id = o->bundle_id + "_" + std::to_string(k);
    }
    b->final_dir = options_.session_dir / id;
    b->partial_dir = options_.session_dir / (id + ".partial");
    b->pre_window = o->pre_window;
    b->topic_status = o->topic_status;
    b->triggers.push_back(plain(o->trigger));
    bundles_opened_.fetch_add(1);
    std::error_code ec;
    fs::create_directories(b->partial_dir, ec);
    if (ec) {
      fail(*b, "create bundle directory", ec);
    } else {
      if (auto e2 = write_file_atomic(b->partial_dir / "manifest.json",
                                      manifest(*b, "capturing", Json::object()));
          e2) {
        fail(*b, "write manifest.json", e2);
      }
      b->fd = UniqueFd(::open((b->partial_dir / "records.jsonl").c_str(),
                              O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644));
      if (!b->fd.valid()) {
        fail(*b, "open records.jsonl", std::error_code(errno, std::generic_category()));
      }
      b->chunks_path = b->partial_dir / "integrity.chunks.tmp";
      b->chunks_fd = UniqueFd(::open(b->chunks_path.c_str(),
                                     O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0644));
      if (!b->chunks_fd.valid()) {
        fail(*b, "open integrity chunk table", std::error_code(errno, std::generic_category()));
      }
    }
    // The pre-trigger records, then the trigger, written together.
    const std::string tline = trigger_line(o->trigger);
    lines_.clear();
    for (const RecordPtr& r : o->pre) {
      lines_.push_back(Line{r->line, r->seq, count_ns(r->t_mono)});
    }
    lines_.push_back(Line{tline, o->trigger.value("seq", std::int64_t{0}), std::nullopt});
    write_lines(*b, lines_);
    if (b->fd.valid() && !b->failed()) {
      if (auto e3 = fsync_fd(b->fd.get()); e3) {
        fail(*b, "fsync records.jsonl", e3);
      }
      b->last_sync = Clock::now();
    }
    return;
  }
  if (std::holds_alternative<OpAppend>(op)) {
    append_run(std::span<Op>(&op, 1));
    return;
  }
  if (auto* t = std::get_if<OpTrigger>(&op)) {
    const auto it = open_.find(t->bundle_id);
    if (it == open_.end()) {
      return;
    }
    it->second->triggers.push_back(plain(t->trigger));
    const std::string tline = trigger_line(t->trigger);
    const Line line{tline, t->trigger.value("seq", std::int64_t{0}), std::nullopt};
    write_lines(*it->second, std::span<const Line>(&line, 1));
    return;
  }
  if (auto* c = std::get_if<OpClose>(&op)) {
    const auto it = open_.find(c->bundle_id);
    if (it == open_.end()) {
      return;
    }
    Bundle& b = *it->second;
    std::uint64_t dropped = 0;
    {
      std::lock_guard lock(stall_mu_);
      if (auto s = stall_drops_.find(c->bundle_id); s != stall_drops_.end()) {
        dropped = s->second;
        stall_drops_.erase(s);
      }
    }
    if (dropped != 0) {
      b.unwritten += dropped;
      ++b.write_errors;
      write_errors_.fetch_add(1);
      if (b.first_error.empty()) {
        b.first_error = "writer stalled: " + std::to_string(dropped) +
                        " operations not handed over within the stall timeout";
      }
    }
    if (!c->topic_status.empty()) {
      b.topic_status = c->topic_status;
    }
    if (b.fd.valid()) {
      if (!b.failed()) {
        if (auto ec = fsync_fd(b.fd.get()); ec) {
          fail(b, "fsync records.jsonl", ec);
        }
      }
      if (auto ec = b.fd.close(); ec) {
        fail(b, "close records.jsonl", ec);
      }
    }
    if (b.chunk_open) {
      spool_chunk(b);
    }
    if (b.chunks_fd.valid()) {
      if (auto ec = b.chunks_fd.close(); ec) {
        fail(b, "close integrity chunk table", ec);
      }
    }
    b.integrity.sha256 = b.sha.finish_hex();
    b.integrity.complete = !b.failed() && b.unwritten == 0;
    std::string status = c->status;
    if (b.failed()) {
      status = "write_failed";
    }
    if (auto ec = write_integrity_streamed(b.partial_dir / "integrity.json", b.integrity,
                                           b.chunks_path, b.chunks_spooled);
        ec) {
      fail(b, "write integrity.json", ec);
      status = "write_failed";
    }
    // The chunk table now lives in integrity.json. A failed bundle keeps the
    // spool file too, as evidence of what was written.
    if (status != "write_failed" && !b.chunks_path.empty()) {
      if (::unlink(b.chunks_path.c_str()) != 0 && errno != ENOENT) {
        fail(b, "remove integrity chunk table", std::error_code(errno, std::generic_category()));
        status = "write_failed";
      }
    }
    if (auto ec = write_file_atomic(b.partial_dir / "manifest.json",
                                    manifest(b, status, c->recorder_stats));
        ec) {
      fail(b, "write manifest.json", ec);
      status = "write_failed";
    }
    // Only evidence that reached the disk intact gets the final name.
    // The bundle directory's entries (integrity.json, manifest.json) are
    // made durable before the rename that declares the bundle final.
    if (status != "write_failed") {
      if (auto ec = fsync_dir(b.partial_dir); ec) {
        fail(b, "fsync bundle directory", ec);
        status = "write_failed";
        (void)write_file_atomic(b.partial_dir / "manifest.json",
                                manifest(b, status, c->recorder_stats));
      }
    }
    if (status != "write_failed") {
      if (::rename(b.partial_dir.c_str(), b.final_dir.c_str()) == 0) {
        // The rename happened; if its directory sync fails the bundle is
        // final but a power loss could still undo the rename. Counted.
        if (auto ec = fsync_dir(options_.session_dir); ec) {
          dir_sync_errors_.fetch_add(1);
          write_errors_.fetch_add(1);
        }
        bundles_finalized_.fetch_add(1);
        std::lock_guard lock(finalized_mu_);
        finalized_.push_back(b.final_dir.string());
      } else {
        fail(b, "rename bundle directory", std::error_code(errno, std::generic_category()));
        bundles_failed_.fetch_add(1);
      }
    } else {
      bundles_failed_.fetch_add(1);
    }
    open_.erase(it);
    return;
  }
  if (auto* s = std::get_if<OpSession>(&op)) {
    std::error_code ec;
    fs::create_directories(options_.session_dir, ec);
    if (!ec) {
      (void)write_file_atomic(options_.session_dir / "session.json", dump_text(s->state));
    }
  }
}

WriterStats EvidenceWriter::stats() const {
  WriterStats s;
  s.records_written = records_written_.load();
  s.records_unwritten = records_unwritten_.load();
  s.bytes_written = bytes_written_.load();
  s.write_errors = write_errors_.load();
  s.bundles_opened = bundles_opened_.load();
  s.bundles_finalized = bundles_finalized_.load();
  s.bundles_failed = bundles_failed_.load();
  s.ops_dropped_stalled = ops_dropped_stalled_.load();
  s.dir_sync_errors = dir_sync_errors_.load();
  s.last_lag_ns = last_lag_ns_.load();
  s.max_lag_ns = max_lag_ns_.load();
  s.queue = queue_.stats();
  return s;
}

std::vector<std::string> EvidenceWriter::finalized_bundles() const {
  std::lock_guard lock(finalized_mu_);
  return finalized_;
}

}  // namespace blackboxrs::recorder
