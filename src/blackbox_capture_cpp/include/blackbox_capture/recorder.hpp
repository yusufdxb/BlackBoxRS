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

#include <chrono>
#include <memory>
#include <string>

#include "rclcpp/node.hpp"
#include "rclcpp/node_options.hpp"

namespace blackbox_capture
{

class RecorderNode final : public rclcpp::Node
{
public:
  explicit RecorderNode(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());
  ~RecorderNode() override;

  RecorderNode(const RecorderNode &) = delete;
  RecorderNode & operator=(const RecorderNode &) = delete;

  void request_stop() noexcept;
  bool drain_and_stop() noexcept;
  bool drain_and_stop(std::chrono::milliseconds timeout) noexcept;
  std::string status_json() const;

private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace blackbox_capture
