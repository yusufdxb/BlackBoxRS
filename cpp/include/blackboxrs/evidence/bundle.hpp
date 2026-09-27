// Flight-bundle evidence: reading, strict validation, digests.
//
// Layout (shared with the Python flight recorder, docs/FLIGHT_RECORDER.md):
//   <bundle>/manifest.json   provenance, profile (with its YAML text), status
//   <bundle>/records.jsonl   one JSON record per line, recorder ingest order
//   <bundle>/integrity.json  written by the C++ recorder only (integrity.hpp)
// A capture in progress lives in <bundle>.partial and is renamed on a clean
// close.
//
// load_bundle() is tolerant (it reports damage), load_evidence() is strict
// (it refuses damage unless allow_partial, as Replay Lab does).
#pragma once

#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#include "blackboxrs/json.hpp"
#include "blackboxrs/profile.hpp"
#include "blackboxrs/time.hpp"

namespace blackboxrs {

class EvidenceError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

inline constexpr const char* kManifestSchema = "blackboxrs.flight.manifest.v1";

struct ReadInfo {
  std::string path;
  bool partial_dir = false;
  std::int64_t torn_lines = 0;
  bool manifest_missing = false;
};

struct BundleRead {
  Json manifest;
  std::vector<Json> records;  // in file order
  ReadInfo info;
};

// Python flight.bundle.load_bundle: tolerate a missing manifest and torn
// lines, and report them.
[[nodiscard]] BundleRead load_bundle(const std::filesystem::path& dir);

// Record kinds that are inputs to a replay; trigger, health and clock_jump
// records are outputs the recorder derived live.
[[nodiscard]] bool is_input_kind(std::string_view kind) noexcept;

struct Evidence {
  std::string source;
  Json manifest;
  std::vector<Json> records;  // validated, sorted by seq
  Profile profile;
  std::string digest;  // sha256 of {"profile_sha256", "records"} canonical JSON
  bool synthetic = false;
  bool partial = false;
  std::vector<std::string> problems;
  ReadInfo info;
  MonoTime t0{};  // earliest t_mono_ns among input records
};

// Python lab.evidence.load_evidence.
[[nodiscard]] Evidence load_evidence(const std::filesystem::path& dir, bool allow_partial,
                                     const std::string& label = {});

// Validate and sort records (Python lab.evidence.validate_records).
[[nodiscard]] std::vector<Json> validate_records(std::vector<Json> records);

}  // namespace blackboxrs
