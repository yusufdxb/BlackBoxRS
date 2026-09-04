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

#pragma once

#include <cerrno>
#include <string>
#include <utility>

namespace blackbox_capture
{

enum class CaptureStatusCode
{
  kOk = 0,
  kInvalidArgument,
  kNotOpen,
  kAlreadyOpen,
  kIoError,
  kNoSpace,
  kMcapError,
  kCorruptData,
  kCapacityExceeded,
  kInvariantViolation,
};

struct CaptureStatus
{
  CaptureStatusCode code{CaptureStatusCode::kOk};
  int system_errno{0};
  std::string message{};

  [[nodiscard]] bool ok() const noexcept {return code == CaptureStatusCode::kOk;}
  explicit operator bool() const noexcept {return ok();}

  static CaptureStatus success() {return {};}

  static CaptureStatus failure(
    CaptureStatusCode status_code, std::string detail,
    int error_number = 0)
  {
    return CaptureStatus{status_code, error_number, std::move(detail)};
  }

  static CaptureStatus from_errno(std::string detail, int error_number = errno)
  {
    const auto status_code = error_number == ENOSPC ? CaptureStatusCode::kNoSpace :
      CaptureStatusCode::kIoError;
    return failure(status_code, std::move(detail), error_number);
  }
};

}  // namespace blackbox_capture
