#include "blackboxrs/integrity.hpp"

#include <openssl/evp.h>

#include <stdexcept>

namespace blackboxrs {

struct Sha256::Ctx {
  EVP_MD_CTX* md = nullptr;
  bool finished = false;
  Ctx() : md(EVP_MD_CTX_new()) {
    if (md == nullptr || EVP_DigestInit_ex(md, EVP_sha256(), nullptr) != 1) {
      EVP_MD_CTX_free(md);
      throw std::runtime_error("OpenSSL: cannot initialise SHA-256");
    }
  }
  ~Ctx() { EVP_MD_CTX_free(md); }
  Ctx(const Ctx&) = delete;
  Ctx& operator=(const Ctx&) = delete;
  Ctx(Ctx&&) = delete;
  Ctx& operator=(Ctx&&) = delete;
};

Sha256::Sha256() : ctx_(std::make_unique<Ctx>()) {}
Sha256::~Sha256() = default;
Sha256::Sha256(Sha256&&) noexcept = default;
Sha256& Sha256::operator=(Sha256&&) noexcept = default;

void Sha256::update(std::span<const std::byte> data) {
  if (ctx_->finished) {
    throw std::logic_error("Sha256::update after finish");
  }
  if (!data.empty() && EVP_DigestUpdate(ctx_->md, data.data(), data.size()) != 1) {
    throw std::runtime_error("OpenSSL: SHA-256 update failed");
  }
}

void Sha256::update(std::string_view data) {
  update(std::as_bytes(std::span(data)));
}

std::array<std::uint8_t, 32> Sha256::finish() {
  if (ctx_->finished) {
    throw std::logic_error("Sha256::finish called twice");
  }
  std::array<std::uint8_t, 32> out{};
  unsigned int len = 0;
  if (EVP_DigestFinal_ex(ctx_->md, out.data(), &len) != 1 || len != out.size()) {
    throw std::runtime_error("OpenSSL: SHA-256 final failed");
  }
  ctx_->finished = true;
  return out;
}

std::string Sha256::finish_hex() {
  static constexpr char kHex[] = "0123456789abcdef";
  const auto digest = finish();
  std::string hex;
  hex.reserve(64);
  for (std::uint8_t b : digest) {
    hex.push_back(kHex[b >> 4U]);
    hex.push_back(kHex[b & 0x0FU]);
  }
  return hex;
}

std::string sha256_hex(std::string_view data) {
  Sha256 h;
  h.update(data);
  return h.finish_hex();
}

namespace {

constexpr std::array<std::uint32_t, 256> make_crc32c_table() {
  constexpr std::uint32_t kPoly = 0x82F63B78U;  // reflected Castagnoli polynomial
  std::array<std::uint32_t, 256> table{};
  for (std::uint32_t i = 0; i < 256; ++i) {
    std::uint32_t c = i;
    for (int k = 0; k < 8; ++k) {
      c = (c & 1U) != 0 ? (c >> 1U) ^ kPoly : c >> 1U;
    }
    table[i] = c;
  }
  return table;
}

constexpr auto kCrc32cTable = make_crc32c_table();

}  // namespace

std::uint32_t crc32c_extend(std::uint32_t crc, std::string_view data) noexcept {
  std::uint32_t c = ~crc;
  for (char ch : data) {
    c = kCrc32cTable[(c ^ static_cast<std::uint8_t>(ch)) & 0xFFU] ^ (c >> 8U);
  }
  return ~c;
}

}  // namespace blackboxrs
