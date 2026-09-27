#include <gtest/gtest.h>

#include <string>

#include "blackboxrs/integrity.hpp"

namespace blackboxrs {
namespace {

TEST(Sha256, NistVectors) {
  EXPECT_EQ(sha256_hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  EXPECT_EQ(sha256_hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  EXPECT_EQ(sha256_hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
}

TEST(Sha256, IncrementalEqualsOneShot) {
  const std::string data(100'000, 'x');
  Sha256 h;
  for (std::size_t i = 0; i < data.size(); i += 777) {
    h.update(std::string_view(data).substr(i, 777));
  }
  EXPECT_EQ(h.finish_hex(), sha256_hex(data));
}

TEST(Sha256, FinishTwiceIsAnError) {
  Sha256 h;
  (void)h.finish();
  EXPECT_THROW((void)h.finish(), std::logic_error);
  EXPECT_THROW(h.update("x"), std::logic_error);
}

TEST(Crc32c, Rfc3720Vectors) {
  EXPECT_EQ(crc32c(std::string(32, '\0')), 0x8A9136AAU);
  EXPECT_EQ(crc32c(std::string(32, '\xff')), 0x62A8AB43U);
  std::string inc;
  for (int i = 0; i < 32; ++i) {
    inc.push_back(static_cast<char>(i));
  }
  EXPECT_EQ(crc32c(inc), 0x46DD794EU);
  EXPECT_EQ(crc32c("123456789"), 0xE3069283U);
}

TEST(Crc32c, ExtendIsConcatenation) {
  EXPECT_EQ(crc32c_extend(crc32c("hello "), "world"), crc32c("hello world"));
}

}  // namespace
}  // namespace blackboxrs
