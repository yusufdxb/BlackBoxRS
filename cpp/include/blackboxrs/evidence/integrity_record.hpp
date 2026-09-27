// integrity.json: the C++ recorder's end-of-bundle integrity record, and the
// validator that checks a bundle against it.
//
// Written last, just before the bundle directory is renamed from
// <id>.partial to <id>. It states how many records and bytes records.jsonl
// holds, the SHA-256 of the whole file (hashed while writing, never re-read),
// and a chunk table: for every chunk of up to N records or B bytes, its byte
// offset, length, record count, sequence range and CRC-32C. That is enough to
// detect and locate truncation, a torn tail, bit flips and records appended
// after finalization.
//
// Bundles written by the Python recorder have no integrity.json. The
// validator still checks their structure but reports them as unverifiable,
// never as verified.
#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "blackboxrs/json.hpp"

namespace blackboxrs {

inline constexpr const char* kIntegritySchema = "blackboxrs.integrity.v1";

struct ChunkEntry {
  std::uint64_t offset = 0;
  std::uint64_t length = 0;
  std::uint64_t records = 0;
  std::int64_t first_seq = 0;
  std::int64_t last_seq = 0;
  std::uint32_t crc32c = 0;
};

struct IntegrityRecord {
  std::uint64_t records = 0;
  std::uint64_t bytes = 0;
  std::string sha256;
  std::optional<std::int64_t> first_seq;
  std::optional<std::int64_t> last_seq;
  std::optional<std::int64_t> first_t_mono_ns;
  std::optional<std::int64_t> last_t_mono_ns;
  std::vector<ChunkEntry> chunks;
  bool complete = false;  // every accepted record reached the file

  [[nodiscard]] Json to_json() const;
  [[nodiscard]] static IntegrityRecord from_json(const Json& j);
};

enum class ValidationStatus : std::uint8_t {
  verified,      // integrity record present and every check passed
  unverifiable,  // structurally sound, but no integrity record to check against
  invalid,       // a check failed
};

struct ValidationReport {
  ValidationStatus status = ValidationStatus::invalid;
  std::vector<std::string> problems;
  std::vector<std::string> notes;
  std::uint64_t records = 0;
  std::string manifest_status;
  [[nodiscard]] Json to_json() const;
};

// Check a bundle directory. Never throws for a damaged bundle; damage is
// reported in the result.
[[nodiscard]] ValidationReport validate_bundle(const std::filesystem::path& dir);

}  // namespace blackboxrs
