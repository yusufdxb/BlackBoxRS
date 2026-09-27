#pragma once

#include <filesystem>
#include <fstream>
#include <random>
#include <string>

namespace blackboxrs::testing {

inline std::filesystem::path repo_root() {
  return BLACKBOXRS_REPO_ROOT;
}
inline std::filesystem::path golden_evidence(const std::string& name) {
  return repo_root() / "examples/replay_lab/evidence" / name;
}
inline std::filesystem::path golden_cases() {
  return repo_root() / "examples/replay_lab/cases";
}

// A fresh directory under the system temp dir, removed on destruction.
class TempDir {
 public:
  TempDir() {
    std::random_device rd;
    path_ = std::filesystem::temp_directory_path() /
            ("bbrs_test_" + std::to_string(rd()) + "_" + std::to_string(rd()));
    std::filesystem::create_directories(path_);
  }
  ~TempDir() {
    std::error_code ec;
    std::filesystem::remove_all(path_, ec);
  }
  TempDir(const TempDir&) = delete;
  TempDir& operator=(const TempDir&) = delete;
  [[nodiscard]] const std::filesystem::path& path() const { return path_; }

 private:
  std::filesystem::path path_;
};

inline void copy_dir(const std::filesystem::path& from, const std::filesystem::path& to) {
  std::filesystem::copy(from, to, std::filesystem::copy_options::recursive);
}

inline std::string read_file(const std::filesystem::path& p) {
  std::ifstream in(p, std::ios::binary);
  return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

inline void write_file(const std::filesystem::path& p, const std::string& s) {
  std::ofstream out(p, std::ios::binary | std::ios::trunc);
  out << s;
}

}  // namespace blackboxrs::testing
