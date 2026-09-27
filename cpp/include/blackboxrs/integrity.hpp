// Integrity primitives for evidence files.
//
// SHA-256 comes from OpenSSL's EVP interface (a dependency every ROS 2 Humble
// install already has through Fast DDS), not a hand-rolled implementation.
// CRC-32C (Castagnoli) is small enough to implement here; it is table driven
// and checked against the RFC 3720 test vectors.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <string_view>

namespace blackboxrs {

// Incremental SHA-256. Move-only; owns an OpenSSL EVP_MD_CTX.
class Sha256 {
 public:
  Sha256();
  ~Sha256();
  Sha256(Sha256&&) noexcept;
  Sha256& operator=(Sha256&&) noexcept;
  Sha256(const Sha256&) = delete;
  Sha256& operator=(const Sha256&) = delete;

  void update(std::span<const std::byte> data);
  void update(std::string_view data);
  // Finishes the digest; the object must not be updated afterwards.
  [[nodiscard]] std::array<std::uint8_t, 32> finish();
  [[nodiscard]] std::string finish_hex();

 private:
  struct Ctx;
  std::unique_ptr<Ctx> ctx_;
};

[[nodiscard]] std::string sha256_hex(std::string_view data);

// CRC-32C. `crc32c_extend(crc32c_extend(0, a), b) == crc32c(a + b)`.
[[nodiscard]] std::uint32_t crc32c_extend(std::uint32_t crc, std::string_view data) noexcept;
[[nodiscard]] inline std::uint32_t crc32c(std::string_view data) noexcept {
  return crc32c_extend(0, data);
}

}  // namespace blackboxrs
