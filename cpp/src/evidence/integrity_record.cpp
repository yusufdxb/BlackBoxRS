#include "blackboxrs/evidence/integrity_record.hpp"

#include <fstream>
#include <sstream>

#include "blackboxrs/integrity.hpp"

namespace blackboxrs {
namespace fs = std::filesystem;

Json IntegrityRecord::to_json() const {
  Json chunk_list = Json::array();
  for (const auto& c : chunks) {
    chunk_list.push_back({{"offset", c.offset},
                          {"length", c.length},
                          {"records", c.records},
                          {"first_seq", c.first_seq},
                          {"last_seq", c.last_seq},
                          {"crc32c", c.crc32c}});
  }
  auto opt = [](const std::optional<std::int64_t>& v) { return v ? Json(*v) : Json(); };
  return {{"schema", kIntegritySchema},
          {"records", records},
          {"bytes", bytes},
          {"sha256", sha256},
          {"first_seq", opt(first_seq)},
          {"last_seq", opt(last_seq)},
          {"first_t_mono_ns", opt(first_t_mono_ns)},
          {"last_t_mono_ns", opt(last_t_mono_ns)},
          {"chunks", chunk_list},
          {"complete", complete}};
}

IntegrityRecord IntegrityRecord::from_json(const Json& j) {
  if (!j.is_object() || j.value("schema", std::string()) != kIntegritySchema) {
    throw std::runtime_error("not a " + std::string(kIntegritySchema) + " document");
  }
  IntegrityRecord r;
  r.records = j.at("records").get<std::uint64_t>();
  r.bytes = j.at("bytes").get<std::uint64_t>();
  r.sha256 = j.at("sha256").get<std::string>();
  auto opt = [&](const char* k) -> std::optional<std::int64_t> {
    return j.contains(k) && j[k].is_number_integer() ? std::optional(j[k].get<std::int64_t>())
                                                     : std::nullopt;
  };
  r.first_seq = opt("first_seq");
  r.last_seq = opt("last_seq");
  r.first_t_mono_ns = opt("first_t_mono_ns");
  r.last_t_mono_ns = opt("last_t_mono_ns");
  for (const auto& c : j.at("chunks")) {
    r.chunks.push_back({c.at("offset").get<std::uint64_t>(), c.at("length").get<std::uint64_t>(),
                        c.at("records").get<std::uint64_t>(), c.at("first_seq").get<std::int64_t>(),
                        c.at("last_seq").get<std::int64_t>(), c.at("crc32c").get<std::uint32_t>()});
  }
  r.complete = j.value("complete", false);
  return r;
}

Json ValidationReport::to_json() const {
  const char* s = status == ValidationStatus::verified       ? "verified"
                  : status == ValidationStatus::unverifiable ? "unverifiable"
                                                             : "invalid";
  return {{"status", s},
          {"problems", problems},
          {"notes", notes},
          {"records", records},
          {"manifest_status", manifest_status}};
}

namespace {

std::optional<std::string> slurp(const fs::path& p) {
  std::ifstream in(p, std::ios::binary);
  if (!in) {
    return std::nullopt;
  }
  std::stringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

}  // namespace

ValidationReport validate_bundle(const fs::path& dir) {
  ValidationReport rep;
  auto problem = [&](std::string p) { rep.problems.push_back(std::move(p)); };
  if (!fs::is_directory(dir)) {
    problem(dir.string() + ": not a directory");
    return rep;
  }
  if (dir.filename().string().ends_with(".partial")) {
    problem("bundle was never finalized (directory still named .partial)");
  }
  // manifest
  const auto manifest_text = slurp(dir / "manifest.json");
  Json manifest;
  if (!manifest_text) {
    problem("manifest.json missing");
  } else {
    manifest = Json::parse(*manifest_text, nullptr, false);
    if (manifest.is_discarded() || !manifest.is_object()) {
      problem("manifest.json is not valid JSON");
      manifest = Json::object();
    }
  }
  rep.manifest_status = manifest.value("status", std::string("unknown"));
  if (rep.manifest_status != "complete") {
    problem("manifest status is '" + rep.manifest_status + "', not 'complete'");
  }
  // records: structure
  const auto records_text = slurp(dir / "records.jsonl");
  if (!records_text) {
    problem("records.jsonl missing");
    return rep;
  }
  const std::string& data = *records_text;
  std::optional<std::int64_t> prev_seq;
  std::size_t pos = 0;
  std::uint64_t line_no = 0;
  bool torn_tail = !data.empty() && data.back() != '\n';
  while (pos < data.size()) {
    std::size_t nl = data.find('\n', pos);
    const bool last = nl == std::string::npos;
    if (last) {
      nl = data.size();
    }
    const std::string_view line(data.data() + pos, nl - pos);
    pos = nl + 1;
    ++line_no;
    if (line.empty()) {
      continue;
    }
    const Json rec = Json::parse(line, nullptr, false);
    if (rec.is_discarded() || !rec.is_object()) {
      problem("records.jsonl line " + std::to_string(line_no) + " is not a JSON object" +
              (last ? " (torn final line)" : ""));
      continue;
    }
    ++rep.records;
    if (!rec.contains("seq") || !rec["seq"].is_number_integer()) {
      problem("records.jsonl line " + std::to_string(line_no) + " has no integer seq");
      continue;
    }
    const auto seq = rec["seq"].get<std::int64_t>();
    if (prev_seq && seq <= *prev_seq) {
      problem("records.jsonl line " + std::to_string(line_no) + ": seq " + std::to_string(seq) +
              " does not increase");
    }
    prev_seq = seq;
  }
  if (torn_tail) {
    problem("records.jsonl does not end with a newline (torn final write)");
  }
  // integrity record
  const auto integrity_text = slurp(dir / "integrity.json");
  if (!integrity_text) {
    rep.notes.emplace_back(
        "no integrity.json (bundle not written by the C++ recorder): structure checked, content "
        "not verifiable");
    rep.status = rep.problems.empty() ? ValidationStatus::unverifiable : ValidationStatus::invalid;
    return rep;
  }
  IntegrityRecord ir;
  try {
    ir = IntegrityRecord::from_json(Json::parse(*integrity_text));
  } catch (const std::exception& exc) {
    problem(std::string("integrity.json unreadable: ") + exc.what());
    rep.status = ValidationStatus::invalid;
    return rep;
  }
  if (!ir.complete) {
    problem("integrity record says not every accepted record reached the file");
  }
  if (ir.bytes != data.size()) {
    problem("records.jsonl is " + std::to_string(data.size()) + " bytes, integrity record says " +
            std::to_string(ir.bytes) +
            (data.size() < ir.bytes ? " (truncated)" : " (data appended after finalization)"));
  }
  if (ir.records != rep.records) {
    problem("records.jsonl holds " + std::to_string(rep.records) +
            " records, integrity record says " + std::to_string(ir.records));
  }
  if (sha256_hex(data) != ir.sha256) {
    problem("records.jsonl SHA-256 does not match the integrity record");
  }
  std::uint64_t expect_offset = 0;
  for (std::size_t i = 0; i < ir.chunks.size(); ++i) {
    const ChunkEntry& c = ir.chunks[i];
    if (c.offset != expect_offset) {
      problem("chunk " + std::to_string(i) + " does not start where the previous one ended");
    }
    expect_offset = c.offset + c.length;
    if (c.offset + c.length > data.size()) {
      problem("chunk " + std::to_string(i) + " (seq " + std::to_string(c.first_seq) + ".." +
              std::to_string(c.last_seq) + ") extends past the end of records.jsonl");
      continue;
    }
    const std::string_view bytes(data.data() + c.offset, c.length);
    if (crc32c(bytes) != c.crc32c) {
      problem("chunk " + std::to_string(i) + " (seq " + std::to_string(c.first_seq) + ".." +
              std::to_string(c.last_seq) + ", bytes " + std::to_string(c.offset) + "+" +
              std::to_string(c.length) + ") fails its CRC-32C: corrupted");
    }
  }
  if (expect_offset != ir.bytes) {
    problem("chunk table covers " + std::to_string(expect_offset) + " bytes of " +
            std::to_string(ir.bytes));
  }
  rep.status = rep.problems.empty() ? ValidationStatus::verified : ValidationStatus::invalid;
  return rep;
}

}  // namespace blackboxrs
