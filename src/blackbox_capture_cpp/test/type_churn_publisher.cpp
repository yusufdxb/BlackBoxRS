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
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#include <chrono>
#include <cstdint>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/int32.hpp"
#include "std_msgs/msg/string.hpp"

namespace
{

using namespace std::chrono_literals;

template<typename MessageT>
int publish_when_matched(
  const std::shared_ptr<rclcpp::Node> & node, const std::string & topic,
  const MessageT & message)
{
  auto publisher = node->create_publisher<MessageT>(topic, rclcpp::QoS(10).reliable());
  const auto match_deadline = std::chrono::steady_clock::now() + 5s;
  while (rclcpp::ok() && publisher->get_subscription_count() == 0U &&
    std::chrono::steady_clock::now() < match_deadline)
  {
    rclcpp::spin_some(node);
    std::this_thread::sleep_for(5ms);
  }
  if (publisher->get_subscription_count() == 0U) {
    std::cerr << "timed out waiting for a compatible recorder subscription on " << topic << '\n';
    return 2;
  }

  for (std::size_t index = 0U; index < 20U; ++index) {
    publisher->publish(message);
    rclcpp::spin_some(node);
    std::this_thread::sleep_for(10ms);
  }
  return 0;
}

}  // namespace

int main(int argc, char ** argv)
{
  if (argc != 3) {
    std::cerr << "usage: type_churn_publisher <string|int32> <topic>\n";
    return 64;
  }

  const std::string type = argv[1];
  const std::string topic = argv[2];
  rclcpp::init(argc, argv);
  int result = 1;
  try {
    auto node = std::make_shared<rclcpp::Node>("blackbox_type_churn_" + type + "_publisher");
    if (type == "string") {
      std_msgs::msg::String message;
      message.data = "first type";
      result = publish_when_matched(node, topic, message);
    } else if (type == "int32") {
      std_msgs::msg::Int32 message;
      message.data = 42;
      result = publish_when_matched(node, topic, message);
    } else {
      std::cerr << "unsupported publisher type: " << type << '\n';
      result = 64;
    }
  } catch (const std::exception & error) {
    std::cerr << "publisher helper failed: " << error.what() << '\n';
    result = 1;
  }
  rclcpp::shutdown();
  return result;
}
