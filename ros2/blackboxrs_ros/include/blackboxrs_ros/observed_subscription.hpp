// A subscription that takes messages serialized, with their DDS timestamps.
//
// rclcpp::GenericSubscription hands its callback the serialized message but
// not the rmw message info, and the recorder needs both DDS times: the
// publisher's source timestamp and the middleware's reception timestamp
// (taken before the executor queue, so it excludes callback batching). This
// subclass overrides the serialized-message hook to get both. The payload is
// the executor's buffer, shared into the recorder's queue, never copied here.
#pragma once

#include <functional>
#include <memory>
#include <string>

#include <rclcpp/generic_subscription.hpp>
#include <rclcpp/rclcpp.hpp>

namespace blackboxrs_ros {

class ObservedSubscription final : public rclcpp::GenericSubscription {
 public:
  using Callback =
      std::function<void(std::shared_ptr<rclcpp::SerializedMessage>, const rmw_message_info_t&)>;

  template <typename AllocatorT = std::allocator<void>>
  ObservedSubscription(rclcpp::node_interfaces::NodeBaseInterface* node_base,
                       std::shared_ptr<rcpputils::SharedLibrary> ts_lib, const std::string& topic,
                       const std::string& type, const rclcpp::QoS& qos, Callback callback,
                       const rclcpp::SubscriptionOptionsWithAllocator<AllocatorT>& options)
      : rclcpp::GenericSubscription(
            node_base, std::move(ts_lib), topic, type, qos,
            [](std::shared_ptr<rclcpp::SerializedMessage>) {}, options),
        callback_(std::move(callback)) {}

  void handle_serialized_message(const std::shared_ptr<rclcpp::SerializedMessage>& msg,
                                 const rclcpp::MessageInfo& info) override {
    callback_(msg, info.get_rmw_message_info());
  }

 private:
  Callback callback_;
};

// Create and register one on `node` (what rclcpp::create_generic_subscription
// does for the stock class).
template <typename AllocatorT = std::allocator<void>>
std::shared_ptr<ObservedSubscription> create_observed_subscription(
    rclcpp::Node& node, const std::string& topic, const std::string& type, const rclcpp::QoS& qos,
    ObservedSubscription::Callback callback,
    const rclcpp::SubscriptionOptionsWithAllocator<AllocatorT>& options = {}) {
  auto ts_lib = rclcpp::get_typesupport_library(type, "rosidl_typesupport_cpp");
  auto topics = node.get_node_topics_interface();
  auto sub =
      std::make_shared<ObservedSubscription>(topics->get_node_base_interface(), std::move(ts_lib),
                                             topic, type, qos, std::move(callback), options);
  topics->add_subscription(sub, options.callback_group);
  return sub;
}

}  // namespace blackboxrs_ros
