// Copyright 2026 Yusuf Guenena
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
// THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#include "blackbox_capture/segment_writer.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <limits>
#include <new>
#include <optional>
#include <sstream>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <utility>

#include <mcap/crc32.hpp>
#include <mcap/reader.hpp>
#include <mcap/writer.hpp>

namespace blackbox_capture
{
namespace
{

constexpr std::string_view kControlTopic = "/blackboxrs/events";
constexpr std::string_view kControlSchemaName = "blackboxrs.capture_event.v1";
constexpr std::string_view kControlSchema =
  R"json({"$schema":"https://json-schema.org/draft/2020-12/schema","title":"blackboxrs.capture_event.v1","type":"object","required":["schema_version","kind","monotonic_ns","ros_time_ns","sequence","topic_id","flags","payload"]})json";

class CheckedPosixWritable final : public mcap::IWritable
{
public:
  explicit CheckedPosixWritable(WriterFailureInjection injection)
  : injection_(injection) {}

  ~CheckedPosixWritable() override {close_fd();}

  CaptureStatus open(const std::filesystem::path & path)
  {
    if (fd_ >= 0) {
      return CaptureStatus::failure(
        CaptureStatusCode::kAlreadyOpen,
        "checked MCAP sink is already open");
    }
    fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0640);
    if (fd_ < 0) {
      return CaptureStatus::from_errno("failed to open partial MCAP segment");
    }
    return CaptureStatus::success();
  }

  void handleWrite(const std::byte * data, uint64_t size) override
  {
    logical_size_ += size;
    if (faulted_ || size == 0U) {
      return;
    }
    if (injection_.delay_per_write.count() > 0) {
      std::this_thread::sleep_for(injection_.delay_per_write);
    }

    uint64_t offset = 0U;
    while (offset < size) {
      if (physical_size_ >= injection_.fail_after_bytes) {
        set_fault(
          injection_.failure_errno == 0 ? EIO : injection_.failure_errno,
          "injected writer failure");
        return;
      }
      uint64_t amount = size - offset;
      if (injection_.max_bytes_per_syscall != 0U) {
        amount = std::min<uint64_t>(amount, injection_.max_bytes_per_syscall);
      }
      amount = std::min<uint64_t>(amount, injection_.fail_after_bytes - physical_size_);
      if (amount == 0U) {
        set_fault(
          injection_.failure_errno == 0 ? EIO : injection_.failure_errno,
          "injected writer failure");
        return;
      }

      const ssize_t written = ::write(fd_, data + offset, static_cast<std::size_t>(amount));
      if (written < 0) {
        if (errno == EINTR) {
          continue;
        }
        set_fault(errno, "MCAP write failed");
        return;
      }
      if (written == 0) {
        set_fault(EIO, "MCAP write returned zero bytes");
        return;
      }
      offset += static_cast<uint64_t>(written);
      physical_size_ += static_cast<uint64_t>(written);
    }
  }

  void end() override {}
  [[nodiscard]] uint64_t size() const override {return logical_size_;}

  CaptureStatus sync()
  {
    if (faulted_) {
      return status_;
    }
    if (injection_.fail_sync) {
      set_fault(
        injection_.failure_errno == 0 ? EIO : injection_.failure_errno,
        "injected segment sync failure");
      return status_;
    }
    if (fd_ < 0 || ::fsync(fd_) != 0) {
      set_fault(errno == 0 ? EBADF : errno, "segment fsync failed");
      return status_;
    }
    return CaptureStatus::success();
  }

  CaptureStatus close_fd()
  {
    if (fd_ < 0) {
      return faulted_ ? status_ : CaptureStatus::success();
    }
    const int descriptor = fd_;
    fd_ = -1;
    if (::close(descriptor) != 0 && !faulted_) {
      set_fault(errno, "segment close failed");
    }
    return faulted_ ? status_ : CaptureStatus::success();
  }

  [[nodiscard]] bool faulted() const noexcept {return faulted_;}
  [[nodiscard]] const CaptureStatus & status() const noexcept {return status_;}
  [[nodiscard]] uint64_t physical_size() const noexcept {return physical_size_;}

private:
  void set_fault(int error_number, const char * message)
  {
    if (!faulted_) {
      faulted_ = true;
      status_ = CaptureStatus::from_errno(message, error_number);
    }
  }

  WriterFailureInjection injection_{};
  int fd_{-1};
  uint64_t logical_size_{0};
  uint64_t physical_size_{0};
  bool faulted_{false};
  CaptureStatus status_{};
};

CaptureStatus sync_directory(const std::filesystem::path & directory)
{
  const int descriptor = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (descriptor < 0) {
    return CaptureStatus::from_errno("failed to open segment directory for fsync");
  }
  const int result = ::fsync(descriptor);
  const int saved_errno = errno;
  ::close(descriptor);
  if (result != 0) {
    return CaptureStatus::from_errno("segment directory fsync failed", saved_errno);
  }
  return CaptureStatus::success();
}

CaptureStatus checked_rename(
  const std::filesystem::path & source,
  const std::filesystem::path & destination, bool inject_failure)
{
  if (inject_failure) {
    return CaptureStatus::failure(CaptureStatusCode::kIoError, "injected rename failure", EIO);
  }
  if (::rename(source.c_str(), destination.c_str()) != 0) {
    return CaptureStatus::from_errno("atomic segment rename failed");
  }
  return CaptureStatus::success();
}

void append_json_escaped(std::string & output, std::string_view value)
{
  output.push_back('"');
  constexpr char kHex[] = "0123456789abcdef";
  for (const char raw_character : value) {
    const auto character = static_cast<unsigned char>(raw_character);
    switch (character) {
      case '"':
        output += "\\\"";
        break;
      case '\\':
        output += "\\\\";
        break;
      case '\b':
        output += "\\b";
        break;
      case '\f':
        output += "\\f";
        break;
      case '\n':
        output += "\\n";
        break;
      case '\r':
        output += "\\r";
        break;
      case '\t':
        output += "\\t";
        break;
      default:
        if (character < 0x20U) {
          output += "\\u00";
          output.push_back(kHex[(character >> 4U) & 0x0FU]);
          output.push_back(kHex[character & 0x0FU]);
        } else {
          output.push_back(static_cast<char>(character));
        }
        break;
    }
  }
  output.push_back('"');
}

template<typename Integer>
void append_integer(std::string & output, Integer value)
{
  std::array<char, 32> buffer{};
  const auto converted = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  if (converted.ec == std::errc{}) {
    output.append(buffer.data(), converted.ptr);
  }
}

std::string_view event_kind(uint32_t flags)
{
  if (has_flag(flags, EventFlag::kDropEvent)) {
    return "drop";
  }
  if (has_flag(flags, EventFlag::kTriggerEvent)) {
    return "trigger";
  }
  if (has_flag(flags, EventFlag::kClockEvent)) {
    return "clock";
  }
  if (has_flag(flags, EventFlag::kGraphEvent)) {
    return "graph";
  }
  if (has_flag(flags, EventFlag::kStatusEvent)) {
    return "status";
  }
  if (has_flag(flags, EventFlag::kProcessEvent)) {
    return "process";
  }
  return "control";
}

bool looks_like_json_object(const std::byte * data, std::size_t size)
{
  if (data == nullptr || size < 2U) {
    return false;
  }
  std::size_t first = 0U;
  while (first < size && std::isspace(std::to_integer<unsigned char>(data[first]))) {
    ++first;
  }
  std::size_t last = size;
  while (last > first && std::isspace(std::to_integer<unsigned char>(data[last - 1U]))) {
    --last;
  }
  return last > first + 1U && data[first] == std::byte{static_cast<unsigned char>('{')} &&
         data[last - 1U] == std::byte{static_cast<unsigned char>('}')};
}

class Sha256
{
public:
  void update(const uint8_t * data, std::size_t size)
  {
    for (std::size_t index = 0; index < size; ++index) {
      buffer_[buffer_size_++] = data[index];
      if (buffer_size_ == buffer_.size()) {
        transform();
        bit_count_ += 512U;
        buffer_size_ = 0U;
      }
    }
  }

  std::array<uint8_t, 32> finish()
  {
    bit_count_ += static_cast<uint64_t>(buffer_size_) * 8U;
    buffer_[buffer_size_++] = 0x80U;
    if (buffer_size_ > 56U) {
      while (buffer_size_ < 64U) {
        buffer_[buffer_size_++] = 0U;
      }
      transform();
      buffer_size_ = 0U;
    }
    while (buffer_size_ < 56U) {
      buffer_[buffer_size_++] = 0U;
    }
    for (int shift = 56; shift >= 0; shift -= 8) {
      buffer_[buffer_size_++] = static_cast<uint8_t>(bit_count_ >> shift);
    }
    transform();

    std::array<uint8_t, 32> digest{};
    for (std::size_t index = 0; index < state_.size(); ++index) {
      digest[index * 4U] = static_cast<uint8_t>(state_[index] >> 24U);
      digest[index * 4U + 1U] = static_cast<uint8_t>(state_[index] >> 16U);
      digest[index * 4U + 2U] = static_cast<uint8_t>(state_[index] >> 8U);
      digest[index * 4U + 3U] = static_cast<uint8_t>(state_[index]);
    }
    return digest;
  }

private:
  static constexpr std::array<uint32_t, 64> kRoundConstants{
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U,
    0x923f82a4U, 0xab1c5ed5U, 0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
    0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U, 0xe49b69c1U, 0xefbe4786U,
    0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
    0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U,
    0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
    0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U, 0xa2bfe8a1U, 0xa81a664bU,
    0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
    0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU,
    0x5b9cca4fU, 0x682e6ff3U, 0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U};

  static uint32_t rotate_right(uint32_t value, uint32_t amount)
  {
    return (value >> amount) | (value << (32U - amount));
  }

  void transform()
  {
    std::array<uint32_t, 64> words{};
    for (std::size_t index = 0; index < 16U; ++index) {
      words[index] = (static_cast<uint32_t>(buffer_[index * 4U]) << 24U) |
        (static_cast<uint32_t>(buffer_[index * 4U + 1U]) << 16U) |
        (static_cast<uint32_t>(buffer_[index * 4U + 2U]) << 8U) |
        static_cast<uint32_t>(buffer_[index * 4U + 3U]);
    }
    for (std::size_t index = 16U; index < words.size(); ++index) {
      const uint32_t s0 = rotate_right(words[index - 15U], 7U) ^
        rotate_right(words[index - 15U], 18U) ^ (words[index - 15U] >> 3U);
      const uint32_t s1 = rotate_right(words[index - 2U], 17U) ^
        rotate_right(words[index - 2U], 19U) ^ (words[index - 2U] >> 10U);
      words[index] = words[index - 16U] + s0 + words[index - 7U] + s1;
    }

    uint32_t a = state_[0];
    uint32_t b = state_[1];
    uint32_t c = state_[2];
    uint32_t d = state_[3];
    uint32_t e = state_[4];
    uint32_t f = state_[5];
    uint32_t g = state_[6];
    uint32_t h = state_[7];
    for (std::size_t index = 0; index < words.size(); ++index) {
      const uint32_t sigma1 = rotate_right(e, 6U) ^ rotate_right(e, 11U) ^ rotate_right(e, 25U);
      const uint32_t choice = (e & f) ^ (~e & g);
      const uint32_t temp1 = h + sigma1 + choice + kRoundConstants[index] + words[index];
      const uint32_t sigma0 = rotate_right(a, 2U) ^ rotate_right(a, 13U) ^ rotate_right(a, 22U);
      const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
      const uint32_t temp2 = sigma0 + majority;
      h = g;
      g = f;
      f = e;
      e = d + temp1;
      d = c;
      c = b;
      b = a;
      a = temp1 + temp2;
    }
    state_[0] += a;
    state_[1] += b;
    state_[2] += c;
    state_[3] += d;
    state_[4] += e;
    state_[5] += f;
    state_[6] += g;
    state_[7] += h;
  }

  std::array<uint8_t, 64> buffer_{};
  std::size_t buffer_size_{0};
  uint64_t bit_count_{0};
  std::array<uint32_t, 8> state_{0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
    0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U};
};

CaptureStatus sha256_file(const std::filesystem::path & path, std::string & output)
{
  std::ifstream stream(path, std::ios::binary);
  if (!stream) {
    return CaptureStatus::from_errno("failed to open segment for SHA-256", errno);
  }
  Sha256 sha;
  std::array<char, 64U * 1024U> buffer{};
  while (stream) {
    stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const std::streamsize count = stream.gcount();
    if (count > 0) {
      sha.update(reinterpret_cast<const uint8_t *>(buffer.data()), static_cast<std::size_t>(count));
    }
  }
  if (!stream.eof()) {
    return CaptureStatus::failure(
      CaptureStatusCode::kIoError,
      "failed while computing segment SHA-256");
  }
  const auto digest = sha.finish();
  constexpr char kHex[] = "0123456789abcdef";
  output.clear();
  output.reserve(64U);
  for (const uint8_t byte : digest) {
    output.push_back(kHex[byte >> 4U]);
    output.push_back(kHex[byte & 0x0FU]);
  }
  return CaptureStatus::success();
}

CaptureStatus write_atomic_text(const std::filesystem::path & destination, std::string_view data)
{
  const std::filesystem::path parent = destination.parent_path().empty() ?
    std::filesystem::path{"."} : destination.parent_path();
  std::string pattern =
    (parent / ("." + destination.filename().string() + ".tmp.XXXXXX")).string();
  std::vector<char> temporary_storage(pattern.begin(), pattern.end());
  temporary_storage.push_back('\0');
  const int descriptor = ::mkostemp(temporary_storage.data(), O_CLOEXEC);
  if (descriptor < 0) {
    return CaptureStatus::from_errno("failed to create temporary metadata file");
  }
  const std::filesystem::path temporary(temporary_storage.data());
  if (::fchmod(descriptor, 0640) != 0) {
    const int saved_errno = errno;
    ::close(descriptor);
    ::unlink(temporary.c_str());
    return CaptureStatus::from_errno("failed to set metadata file permissions", saved_errno);
  }
  std::size_t offset = 0U;
  while (offset < data.size()) {
    const ssize_t written = ::write(descriptor, data.data() + offset, data.size() - offset);
    if (written < 0 && errno == EINTR) {
      continue;
    }
    if (written <= 0) {
      const int saved_errno = written < 0 ? errno : EIO;
      ::close(descriptor);
      ::unlink(temporary.c_str());
      return CaptureStatus::from_errno("failed to write metadata file", saved_errno);
    }
    offset += static_cast<std::size_t>(written);
  }
  if (::fsync(descriptor) != 0) {
    const int saved_errno = errno;
    ::close(descriptor);
    ::unlink(temporary.c_str());
    return CaptureStatus::from_errno("metadata fsync failed", saved_errno);
  }
  if (::close(descriptor) != 0) {
    const int saved_errno = errno;
    ::unlink(temporary.c_str());
    return CaptureStatus::from_errno("metadata close failed", saved_errno);
  }
  if (::rename(temporary.c_str(), destination.c_str()) != 0) {
    const int saved_errno = errno;
    ::unlink(temporary.c_str());
    return CaptureStatus::from_errno("metadata rename failed", saved_errno);
  }
  return sync_directory(parent);
}

class ScopedFileLock
{
public:
  CaptureStatus acquire(const std::filesystem::path & path)
  {
    descriptor_ = ::open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0640);
    if (descriptor_ < 0) {
      return CaptureStatus::from_errno("failed to open recovery lock");
    }
    if (::flock(descriptor_, LOCK_EX | LOCK_NB) != 0) {
      const int saved_errno = errno;
      ::close(descriptor_);
      descriptor_ = -1;
      return CaptureStatus::from_errno("failed to acquire recovery lock", saved_errno);
    }
    return CaptureStatus::success();
  }

  ~ScopedFileLock()
  {
    if (descriptor_ >= 0) {
      (void)::flock(descriptor_, LOCK_UN);
      (void)::close(descriptor_);
    }
  }

private:
  int descriptor_{-1};
};

class ScopedPathCleanup
{
public:
  explicit ScopedPathCleanup(std::filesystem::path path)
  : path_(std::move(path)) {}

  ~ScopedPathCleanup()
  {
    if (active_) {
      (void)::unlink(path_.c_str());
    }
  }

  void release() noexcept {active_ = false;}

private:
  std::filesystem::path path_;
  bool active_{true};
};

std::string indexed_name(uint64_t index, std::string_view suffix)
{
  std::ostringstream output;
  output << std::setw(16) << std::setfill('0') << index << suffix;
  return output.str();
}

uint64_t structurally_truncated_tail(const std::filesystem::path & path, uint64_t file_size)
{
  constexpr uint64_t kMagicSize = sizeof(mcap::Magic);
  constexpr uint64_t kRecordHeaderSize = 9U;
  if (file_size < kMagicSize) {
    return file_size;
  }
  std::ifstream stream(path, std::ios::binary);
  if (!stream) {
    return file_size;
  }
  std::array<uint8_t, kMagicSize> magic{};
  stream.read(reinterpret_cast<char *>(magic.data()), static_cast<std::streamsize>(magic.size()));
  if (!stream || !std::equal(magic.begin(), magic.end(), std::begin(mcap::Magic))) {
    return file_size;
  }

  uint64_t offset = kMagicSize;
  std::array<uint8_t, kRecordHeaderSize> header{};
  while (offset < file_size) {
    const uint64_t remaining = file_size - offset;
    if (remaining == kMagicSize) {
      stream.seekg(static_cast<std::streamoff>(offset));
      stream.read(
        reinterpret_cast<char *>(magic.data()), static_cast<std::streamsize>(magic.size()));
      return stream && std::equal(magic.begin(), magic.end(), std::begin(mcap::Magic)) ? 0U :
             remaining;
    }
    if (remaining < kRecordHeaderSize) {
      return remaining;
    }
    stream.seekg(static_cast<std::streamoff>(offset));
    stream.read(
      reinterpret_cast<char *>(header.data()), static_cast<std::streamsize>(header.size()));
    if (!stream || header[0] < static_cast<uint8_t>(mcap::OpCode::Header) ||
      header[0] > static_cast<uint8_t>(mcap::OpCode::DataEnd))
    {
      return remaining;
    }
    uint64_t record_size = 0U;
    for (uint32_t byte = 0U; byte < 8U; ++byte) {
      record_size |= static_cast<uint64_t>(header[byte + 1U]) << (byte * 8U);
    }
    if (record_size > remaining - kRecordHeaderSize) {
      return remaining;
    }
    offset += kRecordHeaderSize + record_size;
  }
  return 0U;
}

CaptureStatus status_from_current_exception() noexcept
{
  try {
    throw;
  } catch (const std::bad_alloc &) {
    return CaptureStatus::failure(CaptureStatusCode::kCapacityExceeded, "bad_alloc");
  } catch (const std::filesystem::filesystem_error & error) {
    return CaptureStatus::failure(CaptureStatusCode::kIoError, "filesystem", error.code().value());
  } catch (const std::exception &) {
    return CaptureStatus::failure(CaptureStatusCode::kInvariantViolation, "exception");
  } catch (...) {
    return CaptureStatus::failure(CaptureStatusCode::kInvariantViolation, "unknown");
  }
}

}  // namespace

class SegmentWriter::Impl
{
public:
  explicit Impl(SegmentWriterOptions writer_options)
  : options(std::move(writer_options)) {}

  CaptureStatus validate_options() const
  {
    if (options.output_directory.empty() || options.session_id.empty()) {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "output directory and session ID are required");
    }
    if (options.max_segment_bytes == 0U || options.max_segment_events == 0U ||
      options.chunk_size_bytes == 0U || options.max_payload_bytes == 0U ||
      options.max_topics == 0U || options.max_topic_metadata_bytes == 0U ||
      options.max_closed_segment_records == 0U)
    {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "writer capacity limits must be greater than zero");
    }
    if (options.chunk_size_bytes > options.max_segment_bytes ||
      options.max_payload_bytes > options.max_segment_bytes ||
      options.max_topics > std::numeric_limits<mcap::ChannelId>::max() - 1U)
    {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "writer limits are mutually inconsistent");
    }
    return CaptureStatus::success();
  }

  CaptureStatus initialize()
  {
    CaptureStatus status = validate_options();
    if (!status) {
      return set_status(std::move(status));
    }
    definitions.reserve(options.max_topics);
    bindings.reserve(options.max_topics);
    scratch.resize(options.max_payload_bytes);
    control_scratch.reserve(static_cast<std::size_t>(options.max_payload_bytes) + 1024U);
    closed.reserve(options.max_closed_segment_records);

    session_directory = options.output_directory / ("capture_" + options.session_id);
    segments_directory = session_directory / "segments";
    std::error_code error;
    std::filesystem::create_directories(segments_directory, error);
    if (error) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError,
          "failed to create capture segment directory: " +
          error.message(),
          error.value()));
    }
    const std::filesystem::path session_path = session_directory / "session.json";
    error.clear();
    const bool session_exists = std::filesystem::exists(session_path, error);
    if (error) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError,
          "failed to inspect capture session metadata: " + error.message(),
          error.value()));
    }
    if (!session_exists) {
      std::string session_json =
        "{\"schema\":\"blackboxrs.capture_session.v1\",\"schema_version\":"
        "\"blackboxrs.capture_session.v1\",\"session_id\":";
      append_json_escaped(session_json, options.session_id);
      session_json +=
        ",\"capture_backend\":\"cpp\",\"segments_directory\":\"segments\"," +
        std::string("\"monotonic_anchor_ns\":") +
        std::to_string(options.session_monotonic_anchor_ns) +
        ",\"system_time_anchor_ns\":" +
        std::to_string(options.session_system_anchor_ns) + "}\n";
      status = write_atomic_text(session_path, session_json);
      if (!status) {
        return set_status(std::move(status));
      }
    }
    return start_segment();
  }

  CaptureStatus start_segment()
  {
    current = {};
    current.segment_index = next_segment_index;
    current.path = segments_directory / indexed_name(next_segment_index, ".partial.mcap");
    const auto final_path = segments_directory / indexed_name(next_segment_index, ".mcap");
    std::error_code error;
    const bool partial_exists = std::filesystem::exists(current.path, error);
    if (error) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError, "failed to inspect partial segment path", error.value()));
    }
    const bool final_exists = std::filesystem::exists(final_path, error);
    if (error) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError, "failed to inspect final segment path", error.value()));
    }
    if (partial_exists || final_exists) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError,
          "refusing to overwrite an existing segment"));
    }

    sink = std::make_unique<CheckedPosixWritable>(options.failure_injection);
    CaptureStatus status = sink->open(current.path);
    if (!status) {
      return set_status(std::move(status));
    }

    mcap::McapWriterOptions writer_options("ros2");
    writer_options.library = "blackbox_capture_cpp/1";
    writer_options.compression = mcap::Compression::None;
    writer_options.chunkSize = options.chunk_size_bytes;
    writer_options.noChunkCRC = false;
    writer_options.enableDataCRC = true;
    writer_options.noSummaryCRC = false;
    writer_options.noChunking = false;
    // Online indexes grow with the number of messages and chunks in a segment,
    // outside the recorder's configured capture-memory budget. Native capture
    // reads short segments sequentially, so retain independently checksummed
    // chunks while omitting indexes and the optional summary from the hot path.
    writer_options.noMessageIndex = true;
    writer_options.noChunkIndex = true;
    writer_options.noSummary = true;
    writer.open(*sink, writer_options);
    if (sink->faulted()) {
      writer.terminate();
      return set_status(sink->status());
    }

    mcap::Schema control_schema(kControlSchemaName, "jsonschema", kControlSchema);
    writer.addSchema(control_schema);
    mcap::Channel control_channel(kControlTopic, "json", control_schema.id,
      {{"blackboxrs.schema_version", "1"},
        {"blackboxrs.time_contract",
          "log_time_monotonic_publish_time_ros"}});
    writer.addChannel(control_channel);
    control_channel_id = control_channel.id;

    bindings.clear();
    for (const TopicDefinition & definition : definitions) {
      status = bind_topic(definition);
      if (!status) {
        writer.terminate();
        sink->close_fd();
        return set_status(std::move(status));
      }
    }
    open = true;
    faulted = false;
    last_status = CaptureStatus::success();
    estimated_data_bytes = 0U;
    return last_status;
  }

  CaptureStatus bind_topic(const TopicDefinition & definition)
  {
    mcap::Schema schema(definition.type, "ros2msg", std::string_view{});
    writer.addSchema(schema);
    mcap::KeyValueMap metadata{{"blackboxrs.topic_id", std::to_string(definition.topic_id)},
      {"blackboxrs.ros_type", definition.type},
      {"blackboxrs.serialization_format",
        definition.serialization_format},
      {"blackboxrs.time_contract",
        "log_time_monotonic_publish_time_ros"}};
    if (!definition.qos_metadata.empty()) {
      metadata.emplace("blackboxrs.qos", definition.qos_metadata);
    }
    mcap::Channel channel(definition.topic, definition.serialization_format, schema.id, metadata);
    writer.addChannel(channel);
    if (sink->faulted()) {
      return sink->status();
    }
    bindings.push_back(Binding{definition.topic_id, channel.id});
    return CaptureStatus::success();
  }

  CaptureStatus add_topic(const TopicDefinition & definition)
  {
    if (definition.topic_id == 0U || definition.topic.empty() || definition.type.empty() ||
      definition.serialization_format.empty())
    {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kInvalidArgument,
          "topic definition is incomplete"));
    }
    for (const TopicDefinition & existing : definitions) {
      if (existing.topic_id == definition.topic_id) {
        if (existing.topic == definition.topic && existing.type == definition.type &&
          existing.serialization_format == definition.serialization_format &&
          existing.qos_metadata == definition.qos_metadata)
        {
          return CaptureStatus::success();
        }
        return set_status(
          CaptureStatus::failure(
            CaptureStatusCode::kInvalidArgument,
            "topic ID was redefined"));
      }
    }
    if (definitions.size() >= options.max_topics) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kCapacityExceeded,
          "writer topic capacity exhausted"));
    }
    const std::size_t bytes = definition.topic.size() + definition.type.size() +
      definition.serialization_format.size() +
      definition.qos_metadata.size();
    if (bytes > options.max_topic_metadata_bytes - topic_metadata_bytes) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kCapacityExceeded,
          "writer topic metadata capacity exhausted"));
    }
    topic_metadata_bytes += bytes;
    definitions.push_back(definition);
    if (open) {
      CaptureStatus status = bind_topic(definitions.back());
      if (!status) {
        return set_status(std::move(status));
      }
    }
    return CaptureStatus::success();
  }

  std::optional<mcap::ChannelId> channel_for(uint32_t topic_id) const
  {
    for (const Binding & binding : bindings) {
      if (binding.topic_id == topic_id) {
        return binding.channel_id;
      }
    }
    return std::nullopt;
  }

  CaptureStatus materialize(const Event & event, const PayloadArena & arena)
  {
    if (event.header.payload_size != event.payload.size) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "event header and payload handle sizes disagree");
    }
    if (event.header.payload_size > options.max_payload_bytes) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCapacityExceeded,
        "event payload exceeds writer scratch capacity");
    }
    if (!arena.copy_out(event.payload, scratch.data(), scratch.size())) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "payload arena chain failed validation");
    }
    return CaptureStatus::success();
  }

  void make_control_json(const Event & event)
  {
    control_scratch.clear();
    if (looks_like_json_object(scratch.data(), event.header.payload_size)) {
      const std::string_view raw(reinterpret_cast<const char *>(scratch.data()),
        event.header.payload_size);
      if (raw.find("\"schema_version\":\"blackboxrs.capture_event.v1\"") !=
        std::string_view::npos)
      {
        control_scratch.assign(raw.data(), raw.size());
        return;
      }
    }
    control_scratch += "{\"schema_version\":\"blackboxrs.capture_event.v1\",\"kind\":";
    append_json_escaped(control_scratch, event_kind(event.header.flags));
    control_scratch += ",\"monotonic_ns\":";
    append_integer(control_scratch, event.header.monotonic_ns);
    control_scratch += ",\"ros_time_ns\":";
    if (has_flag(event.header.flags, EventFlag::kRosTimeValid)) {
      append_integer(control_scratch, event.header.ros_time_ns);
    } else {
      control_scratch += "null";
    }
    control_scratch += ",\"sequence\":";
    append_integer(control_scratch, event.header.sequence);
    control_scratch += ",\"topic_id\":";
    append_integer(control_scratch, event.header.topic_id);
    control_scratch += ",\"flags\":";
    append_integer(control_scratch, event.header.flags);
    control_scratch += ",\"payload\":";

    bool payload_written = false;
    if (has_flag(event.header.flags, EventFlag::kTriggerEvent) &&
      event.header.payload_size == sizeof(TriggerEvent))
    {
      TriggerEvent trigger{};
      std::memcpy(&trigger, scratch.data(), sizeof(trigger));
      control_scratch += "{\"code\":";
      append_integer(control_scratch, static_cast<uint16_t>(trigger.code));
      control_scratch += ",\"severity\":";
      append_integer(control_scratch, static_cast<uint16_t>(trigger.severity));
      control_scratch += ",\"topic_id\":";
      append_integer(control_scratch, trigger.topic_id);
      control_scratch += ",\"first_seen_ns\":";
      append_integer(control_scratch, trigger.first_seen_ns);
      control_scratch += ",\"confirmed_ns\":";
      append_integer(control_scratch, trigger.confirmed_ns);
      control_scratch += ",\"value\":" + std::to_string(trigger.value);
      control_scratch += ",\"threshold\":" + std::to_string(trigger.threshold) + "}";
      payload_written = true;
    }
    if (!payload_written && has_flag(event.header.flags, EventFlag::kDropEvent) &&
      event.header.payload_size == sizeof(DropEvent))
    {
      DropEvent drop{};
      std::memcpy(&drop, scratch.data(), sizeof(drop));
      control_scratch += "{\"reason\":";
      append_integer(control_scratch, static_cast<uint16_t>(drop.reason));
      control_scratch += ",\"topic_id\":";
      append_integer(control_scratch, drop.topic_id);
      control_scratch += ",\"count\":";
      append_integer(control_scratch, drop.count);
      control_scratch += ",\"bytes\":";
      append_integer(control_scratch, drop.bytes);
      control_scratch += ",\"first_monotonic_ns\":";
      append_integer(control_scratch, drop.first_monotonic_ns);
      control_scratch += ",\"last_monotonic_ns\":";
      append_integer(control_scratch, drop.last_monotonic_ns);
      control_scratch += ",\"first_sequence\":";
      append_integer(control_scratch, drop.first_sequence);
      control_scratch += ",\"last_sequence\":";
      append_integer(control_scratch, drop.last_sequence);
      control_scratch += "}";
      payload_written = true;
    }
    if (!payload_written && looks_like_json_object(scratch.data(), event.header.payload_size)) {
      control_scratch.append(
        reinterpret_cast<const char *>(scratch.data()),
        event.header.payload_size);
      payload_written = true;
    }
    if (!payload_written) {
      control_scratch += "{\"encoding\":\"opaque\",\"size\":";
      append_integer(control_scratch, event.header.payload_size);
      control_scratch += "}";
    }
    control_scratch.push_back('}');
  }

  CaptureStatus write(const Event & event, const PayloadArena & arena)
  {
    if (!open) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kNotOpen,
          "segment writer is not open"));
    }
    if (faulted || sink->faulted()) {
      return set_status(sink->faulted() ? sink->status() : last_status);
    }
    if (current.event_count != 0U &&
      (current.event_count >= options.max_segment_events ||
      estimated_data_bytes + event.header.payload_size + 512U > options.max_segment_bytes))
    {
      CaptureStatus status = finish_segment(true);
      if (!status) {
        return status;
      }
      ++next_segment_index;
      status = start_segment();
      if (!status) {
        return status;
      }
    }

    CaptureStatus status = materialize(event, arena);
    if (!status) {
      return set_status(std::move(status));
    }

    mcap::Message message{};
    message.sequence = static_cast<uint32_t>(event.header.sequence);
    message.logTime = event.header.monotonic_ns;
    message.publishTime = has_flag(event.header.flags, EventFlag::kRosTimeValid) &&
      event.header.ros_time_ns >= 0 ?
      static_cast<uint64_t>(event.header.ros_time_ns) :
      0U;

    if (has_flag(event.header.flags, EventFlag::kSerializedMessage)) {
      const auto channel = channel_for(event.header.topic_id);
      if (!channel) {
        return set_status(
          CaptureStatus::failure(
            CaptureStatusCode::kInvalidArgument,
            "serialized event references unknown topic ID"));
      }
      message.channelId = *channel;
      message.data = scratch.data();
      message.dataSize = event.header.payload_size;
    } else {
      make_control_json(event);
      message.channelId = control_channel_id;
      message.data = reinterpret_cast<const std::byte *>(control_scratch.data());
      message.dataSize = control_scratch.size();
    }

    const mcap::Status mcap_status = writer.write(message);
    if (!mcap_status.ok()) {
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kMcapError,
          "MCAP message write failed: " +
          mcap_status.message));
    }
    if (sink->faulted()) {
      return set_status(sink->status());
    }

    if (current.event_count == 0U) {
      current.first_sequence = event.header.sequence;
      current.first_monotonic_ns = event.header.monotonic_ns;
    }
    current.last_sequence = event.header.sequence;
    current.last_monotonic_ns = event.header.monotonic_ns;
    ++current.event_count;
    estimated_data_bytes += event.header.payload_size + 64U;
    current.file_bytes = sink->physical_size();
    return CaptureStatus::success();
  }

  CaptureStatus do_flush()
  {
    if (!open) {
      return CaptureStatus::failure(CaptureStatusCode::kNotOpen, "segment writer is not open");
    }
    writer.closeLastChunk();
    if (sink->faulted()) {
      return set_status(sink->status());
    }
    CaptureStatus status = sink->sync();
    if (!status) {
      return set_status(std::move(status));
    }
    current.file_bytes = sink->physical_size();
    return CaptureStatus::success();
  }

  std::string sidecar_json(const SegmentInfo & info) const
  {
    std::string output =
      "{\"schema\":\"blackboxrs.capture_segment.v1\",\"schema_version\":"
      "\"blackboxrs.capture_segment.v1\",\"session_id\":";
    append_json_escaped(output, options.session_id);
    output += ",\"segment_index\":";
    append_integer(output, info.segment_index);
    output += ",\"path\":";
    append_json_escaped(output, info.path.filename().string());
    output += ",\"clean\":";
    output += info.clean ? "true" : "false";
    output += ",\"recovered\":";
    output += info.recovered ? "true" : "false";
    output += ",\"first_sequence\":";
    append_integer(output, info.first_sequence);
    output += ",\"last_sequence\":";
    append_integer(output, info.last_sequence);
    output += ",\"accounting_scope\":\"session_cumulative\"";
    output += ",\"received\":";
    append_integer(output, counters.received);
    output += ",\"admitted\":";
    append_integer(output, counters.admitted);
    output += ",\"committed\":";
    append_integer(output, counters.committed);
    output += ",\"dropped\":";
    append_integer(output, counters.dropped);
    output += ",\"bytes_captured\":";
    append_integer(output, counters.bytes_captured);
    output += ",\"bytes_dropped\":";
    append_integer(output, counters.bytes_dropped);
    output += ",\"peak_queue_utilization\":";
    append_integer(output, counters.peak_queue_utilization);
    output += ",\"storage_errors\":";
    append_integer(output, counters.storage_errors);
    output += ",\"clock_anomalies\":";
    append_integer(output, counters.clock_anomalies);
    output += ",\"monotonic_start_ns\":";
    append_integer(output, info.first_monotonic_ns);
    output += ",\"monotonic_end_ns\":";
    append_integer(output, info.last_monotonic_ns);
    output += ",\"event_count\":";
    append_integer(output, info.event_count);
    output += ",\"file_bytes\":";
    append_integer(output, info.file_bytes);
    output += ",\"sha256\":";
    append_json_escaped(output, info.sha256);
    output += "}\n";
    return output;
  }

  CaptureStatus finish_segment(bool clean)
  {
    if (!open) {
      return CaptureStatus::failure(CaptureStatusCode::kNotOpen, "segment writer is not open");
    }

    if (faulted || sink->faulted()) {
      writer.terminate();
      sink->close_fd();
      open = false;
      current.clean = false;
      current.file_bytes = sink->physical_size();
      return set_status(sink->faulted() ? sink->status() : last_status);
    }

    writer.close();
    if (sink->faulted()) {
      sink->close_fd();
      open = false;
      current.clean = false;
      current.file_bytes = sink->physical_size();
      return set_status(sink->status());
    }
    if (options.sync_on_rotation) {
      CaptureStatus status = sink->sync();
      if (!status) {
        sink->close_fd();
        open = false;
        return set_status(std::move(status));
      }
    }
    CaptureStatus status = sink->close_fd();
    if (!status) {
      open = false;
      return set_status(std::move(status));
    }

    const std::filesystem::path partial_path = current.path;
    const std::filesystem::path final_path =
      segments_directory / indexed_name(current.segment_index, ".mcap");
    status = checked_rename(partial_path, final_path, options.failure_injection.fail_rename);
    if (!status) {
      open = false;
      return set_status(std::move(status));
    }
    status = sync_directory(segments_directory);
    if (!status) {
      open = false;
      return set_status(std::move(status));
    }

    current.path = final_path;
    current.clean = clean;
    std::error_code size_error;
    current.file_bytes = std::filesystem::file_size(final_path, size_error);
    if (size_error) {
      open = false;
      return set_status(
        CaptureStatus::failure(
          CaptureStatusCode::kIoError, "failed to stat finalized segment", size_error.value()));
    }
    status = sha256_file(final_path, current.sha256);
    if (!status) {
      open = false;
      return set_status(std::move(status));
    }
    const std::filesystem::path sidecar_path =
      segments_directory / indexed_name(current.segment_index, ".json");
    status = write_atomic_text(sidecar_path, sidecar_json(current));
    if (!status) {
      open = false;
      return set_status(std::move(status));
    }

    if (closed.size() == options.max_closed_segment_records) {
      std::move(closed.begin() + 1, closed.end(), closed.begin());
      closed.back() = current;
    } else {
      closed.push_back(current);
    }
    open = false;
    sink.reset();
    return CaptureStatus::success();
  }

  CaptureStatus rotate_segment()
  {
    if (open && current.event_count == 0U) {
      return CaptureStatus::success();
    }
    CaptureStatus status = finish_segment(true);
    if (!status) {
      return status;
    }
    ++next_segment_index;
    return start_segment();
  }

  CaptureStatus close_writer()
  {
    if (!open) {
      return last_status.ok() ? CaptureStatus::success() : last_status;
    }
    if (current.event_count == 0U) {
      writer.terminate();
      CaptureStatus status = sink->close_fd();
      if (!status.ok()) {
        open = false;
        return set_status(std::move(status));
      }
      if (::unlink(current.path.c_str()) != 0 && errno != ENOENT) {
        open = false;
        return set_status(
          CaptureStatus::from_errno("failed to remove empty partial segment"));
      }
      status = sync_directory(segments_directory);
      open = false;
      sink.reset();
      return status.ok() ? CaptureStatus::success() : set_status(std::move(status));
    }
    return finish_segment(true);
  }

  CaptureStatus set_status(CaptureStatus status)
  {
    last_status = std::move(status);
    if (!last_status.ok()) {
      faulted = true;
    }
    return last_status;
  }

  struct Binding
  {
    uint32_t topic_id;
    mcap::ChannelId channel_id;
  };

  SegmentWriterOptions options;
  std::filesystem::path session_directory;
  std::filesystem::path segments_directory;
  std::vector<TopicDefinition> definitions;
  std::vector<Binding> bindings;
  std::vector<std::byte> scratch;
  std::string control_scratch;
  std::vector<SegmentInfo> closed;
  std::unique_ptr<CheckedPosixWritable> sink;
  mcap::McapWriter writer;
  mcap::ChannelId control_channel_id{0};
  SegmentCounters counters{};
  SegmentInfo current{};
  CaptureStatus last_status{};
  std::size_t topic_metadata_bytes{0};
  uint64_t next_segment_index{0};
  uint64_t estimated_data_bytes{0};
  bool open{false};
  bool faulted{false};
};

SegmentWriter::SegmentWriter(SegmentWriterOptions options)
: impl_(std::make_unique<Impl>(std::move(options))) {}

SegmentWriter::~SegmentWriter()
{
  if (impl_ && impl_->open) {
    try {
      (void)impl_->close_writer();
    } catch (...) {
      impl_->writer.terminate();
      if (impl_->sink) {
        (void)impl_->sink->close_fd();
      }
    }
  }
}

CaptureStatus SegmentWriter::open()
{
  try {
    if (impl_->open) {
      return CaptureStatus::failure(
        CaptureStatusCode::kAlreadyOpen,
        "segment writer is already open");
    }
    return impl_->initialize();
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

CaptureStatus SegmentWriter::register_topic(const TopicDefinition & definition)
{
  try {
    return impl_->add_topic(definition);
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

CaptureStatus SegmentWriter::write_event(const Event & event, const PayloadArena & arena)
{
  try {
    return impl_->write(event, arena);
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

CaptureStatus SegmentWriter::flush()
{
  try {
    return impl_->do_flush();
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

CaptureStatus SegmentWriter::rotate()
{
  try {
    return impl_->rotate_segment();
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

CaptureStatus SegmentWriter::close()
{
  try {
    return impl_->close_writer();
  } catch (...) {
    return impl_->set_status(status_from_current_exception());
  }
}

void SegmentWriter::set_segment_counters(const SegmentCounters & counters) noexcept
{
  impl_->counters = counters;
}

bool SegmentWriter::is_open() const noexcept {return impl_->open;}

bool SegmentWriter::faulted() const noexcept {return impl_->faulted;}

const CaptureStatus & SegmentWriter::last_status() const noexcept {return impl_->last_status;}

const SegmentInfo & SegmentWriter::current_segment() const noexcept {return impl_->current;}

const std::vector<SegmentInfo> & SegmentWriter::closed_segments() const noexcept
{
  return impl_->closed;
}

CaptureStatus SegmentWriter::recover_partial(
  const std::filesystem::path & input,
  const std::filesystem::path & output,
  RecoveryResult & result)
{
  result = {};
  try {
    const std::filesystem::path output_parent = output.parent_path().empty() ?
      std::filesystem::path{"."} : output.parent_path();
    const std::filesystem::path partial_output = output.string() + ".partial";
    const std::filesystem::path recovery_sidecar = output.string() + ".recovery.json";
    std::error_code path_error;
    const std::filesystem::path canonical_input =
      std::filesystem::weakly_canonical(input, path_error);
    if (path_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to resolve recovery input: " + path_error.message(),
        path_error.value());
    }
    const std::filesystem::path canonical_output =
      std::filesystem::weakly_canonical(output, path_error);
    if (path_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to resolve recovery output: " + path_error.message(),
        path_error.value());
    }
    const std::filesystem::path canonical_partial =
      std::filesystem::weakly_canonical(partial_output, path_error);
    if (path_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to resolve partial recovery output: " + path_error.message(),
        path_error.value());
    }
    const std::filesystem::path canonical_sidecar =
      std::filesystem::weakly_canonical(recovery_sidecar, path_error);
    if (path_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to resolve recovery sidecar: " + path_error.message(),
        path_error.value());
    }
    if (canonical_input == canonical_output || canonical_input == canonical_partial ||
      canonical_input == canonical_sidecar)
    {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "recovery input aliases an output path");
    }
    ScopedFileLock recovery_lock;
    CaptureStatus status = recovery_lock.acquire(output.string() + ".recovery.lock");
    if (!status) {
      return status;
    }

    std::error_code file_error;
    const uint64_t input_size = std::filesystem::file_size(input, file_error);
    if (file_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to stat partial MCAP: " + file_error.message(),
        file_error.value());
    }

    const bool output_exists = std::filesystem::exists(output, file_error);
    if (file_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to inspect recovery output: " + file_error.message(),
        file_error.value());
    }
    const bool partial_exists = std::filesystem::exists(partial_output, file_error);
    if (file_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to inspect partial recovery output: " + file_error.message(),
        file_error.value());
    }
    if (output_exists) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "refusing to overwrite an existing recovery output");
    }
    const bool stale_sidecar_exists = std::filesystem::exists(recovery_sidecar, file_error);
    if (file_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to inspect recovery sidecar: " + file_error.message(),
        file_error.value());
    }
    auto aliases_input = [&](const std::filesystem::path & candidate, bool exists) {
        if (!exists) {
          return false;
        }
        std::error_code equivalent_error;
        const bool equivalent = std::filesystem::equivalent(input, candidate, equivalent_error);
        return !equivalent_error && equivalent;
      };
    if (aliases_input(partial_output, partial_exists) ||
      aliases_input(recovery_sidecar, stale_sidecar_exists))
    {
      return CaptureStatus::failure(
        CaptureStatusCode::kInvalidArgument,
        "recovery input aliases an output inode");
    }
    if (partial_exists && ::unlink(partial_output.c_str()) != 0) {
      return CaptureStatus::from_errno("failed to remove stale partial recovery output");
    }
    if (stale_sidecar_exists && ::unlink(recovery_sidecar.c_str()) != 0) {
      return CaptureStatus::from_errno("failed to remove stale recovery sidecar");
    }
    if (partial_exists || stale_sidecar_exists) {
      status = sync_directory(output_parent);
      if (!status) {
        return status;
      }
    }

    mcap::McapReader reader;
    mcap::Status reader_status = reader.open(input.string());
    if (!reader_status.ok()) {
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        "failed to open partial MCAP: " + reader_status.message);
    }

    constexpr uint64_t kMaximumRecoveryChunkBytes = 64U * 1024U * 1024U;
    bool unsafe_chunk_size = false;
    std::optional<mcap::ByteOffset> corrupt_chunk_offset;
    std::optional<mcap::ByteOffset> clean_footer_offset;
    bool trailing_magic_present = false;
    std::string chunk_validation_message;
    if (mcap::IReadable * source = reader.dataSource()) {
      if (input_size >= sizeof(mcap::Magic)) {
        std::byte * tail = nullptr;
        const uint64_t bytes_read = source->read(
          &tail, input_size - sizeof(mcap::Magic), sizeof(mcap::Magic));
        trailing_magic_present = bytes_read == sizeof(mcap::Magic) && tail != nullptr &&
          std::memcmp(tail, mcap::Magic, sizeof(mcap::Magic)) == 0;
      }
      mcap::TypedRecordReader validator(*source, sizeof(mcap::Magic), input_size);
      validator.onChunk = [&](const mcap::Chunk & chunk, mcap::ByteOffset offset) {
          if (chunk.compressedSize > input_size ||
            chunk.uncompressedSize > kMaximumRecoveryChunkBytes ||
            chunk.uncompressedSize > std::numeric_limits<std::size_t>::max())
          {
            unsafe_chunk_size = true;
            chunk_validation_message = "partial MCAP contains invalid chunk sizing";
            return;
          }
          if (chunk.compression != "") {
            return;
          }
          if (chunk.uncompressedSize != chunk.compressedSize) {
            unsafe_chunk_size = true;
            chunk_validation_message = "partial MCAP contains invalid uncompressed chunk sizing";
            return;
          }
          if (chunk.uncompressedCrc == 0U) {
            return;
          }
          const uint32_t computed = mcap::internal::crc32Final(
            mcap::internal::crc32Update(
              mcap::internal::CRC32_INIT, chunk.records,
              static_cast<std::size_t>(chunk.uncompressedSize)));
          if (computed != chunk.uncompressedCrc) {
            corrupt_chunk_offset = offset;
            chunk_validation_message = "partial MCAP contains a chunk CRC mismatch";
          }
        };
      validator.onFooter = [&](const mcap::Footer &, mcap::ByteOffset offset) {
          clean_footer_offset = offset;
        };
      while (!unsafe_chunk_size && !corrupt_chunk_offset.has_value() && validator.next()) {
      }
    }
    if (unsafe_chunk_size) {
      reader.close();
      return CaptureStatus::failure(
        CaptureStatusCode::kCorruptData,
        chunk_validation_message);
    }

    CheckedPosixWritable sink({});
    status = sink.open(partial_output);
    if (!status) {
      reader.close();
      return status;
    }
    ScopedPathCleanup partial_cleanup(partial_output);
    mcap::McapWriter writer;
    mcap::McapWriterOptions options("ros2");
    options.library = "blackbox_capture_cpp/recovery-1";
    options.compression = mcap::Compression::None;
    options.enableDataCRC = true;
    options.noChunkCRC = false;
    options.noSummaryCRC = false;
    options.noMessageIndex = true;
    options.noChunkIndex = true;
    options.noSummary = true;
    writer.open(sink, options);

    std::unordered_map<mcap::SchemaId, mcap::SchemaId> schemas;
    std::unordered_map<mcap::ChannelId, mcap::ChannelId> channels;
    schemas.reserve(256U);
    channels.reserve(256U);
    bool problem_seen = false;
    std::string problem_message;
    const auto on_problem = [&](const mcap::Status & problem) {
        problem_seen = true;
        if (problem_message.empty()) {
          problem_message = problem.message;
        }
      };

    constexpr std::size_t kRecoveryMetadataLimit = 4096U;
    constexpr uint64_t kRecoveryMetadataBytesLimit = 16U * 1024U * 1024U;
    constexpr uint64_t kRecoveryMessageBytesLimit = 64U * 1024U * 1024U;
    uint64_t recovery_metadata_bytes = 0U;
    auto reserve_metadata_bytes = [&](std::size_t bytes) {
        const uint64_t amount = static_cast<uint64_t>(bytes);
        if (amount > kRecoveryMetadataBytesLimit - recovery_metadata_bytes) {
          return false;
        }
        recovery_metadata_bytes += amount;
        return true;
      };
    const mcap::ByteOffset recovery_end = corrupt_chunk_offset.value_or(input_size);
    mcap::LinearMessageView recoverable_messages(
      reader, sizeof(mcap::Magic), recovery_end, 0U, mcap::MaxTime, on_problem);
    for (const mcap::MessageView & view : recoverable_messages) {
      if (view.channel == nullptr || view.message.dataSize > kRecoveryMessageBytesLimit) {
        problem_seen = true;
        problem_message = "recovery message size or channel is invalid";
        break;
      }
      mcap::SchemaId schema_id = 0U;
      if (view.schema) {
        const auto found_schema = schemas.find(view.schema->id);
        if (found_schema == schemas.end()) {
          if (schemas.size() >= kRecoveryMetadataLimit) {
            problem_seen = true;
            problem_message = "recovery schema capacity exceeded";
            break;
          }
          if (!reserve_metadata_bytes(view.schema->name.size()) ||
            !reserve_metadata_bytes(view.schema->encoding.size()) ||
            !reserve_metadata_bytes(view.schema->data.size()))
          {
            problem_seen = true;
            problem_message = "recovery schema metadata byte capacity exceeded";
            break;
          }
          mcap::Schema schema = *view.schema;
          const mcap::SchemaId old_id = schema.id;
          writer.addSchema(schema);
          schemas.emplace(old_id, schema.id);
          schema_id = schema.id;
        } else {
          schema_id = found_schema->second;
        }
      }

      mcap::ChannelId channel_id = 0U;
      const auto found_channel = channels.find(view.channel->id);
      if (found_channel == channels.end()) {
        if (channels.size() >= kRecoveryMetadataLimit) {
          problem_seen = true;
          problem_message = "recovery channel capacity exceeded";
          break;
        }
        bool metadata_fits = reserve_metadata_bytes(view.channel->topic.size()) &&
          reserve_metadata_bytes(view.channel->messageEncoding.size());
        for (const auto & [key, value] : view.channel->metadata) {
          metadata_fits = metadata_fits && reserve_metadata_bytes(key.size()) &&
            reserve_metadata_bytes(value.size());
        }
        if (!metadata_fits) {
          problem_seen = true;
          problem_message = "recovery channel metadata byte capacity exceeded";
          break;
        }
        mcap::Channel channel = *view.channel;
        const mcap::ChannelId old_id = channel.id;
        channel.schemaId = schema_id;
        writer.addChannel(channel);
        channels.emplace(old_id, channel.id);
        channel_id = channel.id;
      } else {
        channel_id = found_channel->second;
      }

      mcap::Message message = view.message;
      message.channelId = channel_id;
      const mcap::Status write_status = writer.write(message);
      if (!write_status.ok() || sink.faulted()) {
        writer.terminate();
        sink.close_fd();
        reader.close();
        return sink.faulted() ?
               sink.status() :
               CaptureStatus::failure(
          CaptureStatusCode::kMcapError,
          "failed to write recovered message: " +
          write_status.message);
      }
      ++result.recovered_messages;
      result.last_recovered_sequence_low32 = view.message.sequence;
    }
    constexpr uint64_t kRecordHeaderBytes = 1U + sizeof(uint64_t);
    constexpr uint64_t kFooterPayloadBytes =
      sizeof(uint64_t) + sizeof(uint64_t) + sizeof(uint32_t);
    const uint64_t discarded_tail_bytes = corrupt_chunk_offset.has_value() ?
      input_size - *corrupt_chunk_offset : structurally_truncated_tail(input, input_size);
    const bool footer_is_adjacent_to_magic = clean_footer_offset.has_value() &&
      *clean_footer_offset <= input_size &&
      kRecordHeaderBytes + kFooterPayloadBytes + sizeof(mcap::Magic) <=
      input_size - *clean_footer_offset &&
      *clean_footer_offset + kRecordHeaderBytes + kFooterPayloadBytes ==
      input_size - sizeof(mcap::Magic);
    result.input_was_clean = !corrupt_chunk_offset.has_value() &&
      footer_is_adjacent_to_magic && trailing_magic_present &&
      discarded_tail_bytes == 0U && !problem_seen;
    result.unwritten_tail_loss_unknown = !result.input_was_clean;
    reader.close();
    writer.close();
    if (sink.faulted()) {
      sink.close_fd();
      return sink.status();
    }
    status = sink.sync();
    if (!status) {
      sink.close_fd();
      return status;
    }
    status = sink.close_fd();
    if (!status) {
      return status;
    }

    result.corruption_reason = corrupt_chunk_offset.has_value() ? chunk_validation_message :
      (problem_seen ? problem_message :
      (result.input_was_clean ? "" :
      (discarded_tail_bytes > 0U ? "trailing or structurally invalid bytes" :
      "missing clean footer")));
    result.discarded_tail_bytes = discarded_tail_bytes;

    std::string digest;
    status = sha256_file(partial_output, digest);
    if (!status) {
      return status;
    }
    const uint64_t output_size = std::filesystem::file_size(partial_output, file_error);
    if (file_error) {
      return CaptureStatus::failure(
        CaptureStatusCode::kIoError,
        "failed to stat recovered MCAP: " + file_error.message(),
        file_error.value());
    }
    std::string recovery_json =
      "{\"schema_version\":\"blackboxrs.capture_recovery.v1\",\"input\":";
    append_json_escaped(recovery_json, input.filename().string());
    recovery_json += ",\"output\":";
    append_json_escaped(recovery_json, output.filename().string());
    recovery_json += ",\"input_was_clean\":";
    recovery_json += result.input_was_clean ? "true" : "false";
    recovery_json += ",\"unwritten_tail_loss_unknown\":";
    recovery_json += result.unwritten_tail_loss_unknown ? "true" : "false";
    recovery_json += ",\"recovered_messages\":" +
      std::to_string(result.recovered_messages);
    recovery_json += ",\"last_recovered_sequence_low32\":";
    if (result.last_recovered_sequence_low32) {
      recovery_json += std::to_string(*result.last_recovered_sequence_low32);
    } else {
      recovery_json += "null";
    }
    recovery_json += ",\"discarded_tail_bytes\":" +
      std::to_string(result.discarded_tail_bytes);
    recovery_json += ",\"corruption_reason\":";
    append_json_escaped(recovery_json, result.corruption_reason);
    recovery_json += ",\"file_bytes\":" + std::to_string(output_size);
    recovery_json += ",\"sha256\":";
    append_json_escaped(recovery_json, digest);
    recovery_json += "}\n";
    status = write_atomic_text(recovery_sidecar, recovery_json);
    if (!status) {
      return status;
    }
    ScopedPathCleanup sidecar_cleanup(recovery_sidecar);
    status = checked_rename(partial_output, output, false);
    if (!status) {
      return status;
    }
    partial_cleanup.release();
    sidecar_cleanup.release();
    status = sync_directory(output_parent);
    if (!status) {
      return status;
    }
    result.recovered = true;
    result.output_path = output;
    return CaptureStatus::success();
  } catch (...) {
    return status_from_current_exception();
  }
}

}  // namespace blackbox_capture
