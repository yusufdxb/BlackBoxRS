cmake_minimum_required(VERSION 3.22)
project(blackboxrs_ros LANGUAGES CXX)

find_package(ament_cmake REQUIRED)
find_package(blackboxrs_core 0.5 REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rcpputils REQUIRED)
find_package(rmw REQUIRED)
find_package(rosidl_typesupport_cpp REQUIRED)
find_package(rosidl_typesupport_introspection_cpp REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(std_msgs REQUIRED)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_EXTENSIONS OFF)

function(blackboxrs_ros_warnings target)
  target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Wshadow -Wnon-virtual-dtor
                                           -Woverloaded-virtual -Wnull-dereference
                                           -ffp-contract=off)
endfunction()

add_library(blackboxrs_ros_lib STATIC
  src/introspection_decoder.cpp
  src/runtime.cpp
  src/recorder_node.cpp)
target_compile_features(blackboxrs_ros_lib PUBLIC cxx_std_20)
target_include_directories(blackboxrs_ros_lib PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)
target_link_libraries(blackboxrs_ros_lib PUBLIC
  blackboxrs::core
  rclcpp::rclcpp
  rcpputils::rcpputils
  rmw::rmw
  rosidl_typesupport_cpp::rosidl_typesupport_cpp
  rosidl_typesupport_introspection_cpp::rosidl_typesupport_introspection_cpp
  ${diagnostic_msgs_TARGETS}
  ${std_msgs_TARGETS})
blackboxrs_ros_warnings(blackboxrs_ros_lib)

foreach(exe recorder monitor replay preflight)
  add_executable(${exe} src/${exe}_main.cpp)
  target_link_libraries(${exe} PRIVATE blackboxrs_ros_lib)
  blackboxrs_ros_warnings(${exe})
  install(TARGETS ${exe} RUNTIME DESTINATION lib/${PROJECT_NAME})
endforeach()

install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(geometry_msgs REQUIRED)
  find_package(nav_msgs REQUIRED)
  find_package(sensor_msgs REQUIRED)
  set(_repo "${CMAKE_CURRENT_SOURCE_DIR}/../..")
  # Benchmark load tool (telemetry only, localhost-only); built for tests, not installed.
  add_executable(load_publisher test/load_publisher.cpp)
  target_link_libraries(load_publisher blackboxrs_ros_lib)
  ament_add_gtest(test_decoder test/test_decoder.cpp TIMEOUT 120)
  ament_add_gtest(test_passive_nodes test/test_passive_nodes.cpp TIMEOUT 300 SKIP_LINKING_MAIN_LIBRARIES)
  foreach(t test_decoder test_passive_nodes)
    target_link_libraries(${t} blackboxrs_ros_lib ${geometry_msgs_TARGETS} ${nav_msgs_TARGETS}
                          ${sensor_msgs_TARGETS})
    target_compile_definitions(${t} PRIVATE
      BLACKBOXRS_REPO_ROOT="${_repo}"
      BLACKBOXRS_ROS_TEST_DIR="${CMAKE_CURRENT_SOURCE_DIR}/test"
      BLACKBOXRS_ROS_BIN_DIR="${CMAKE_CURRENT_BINARY_DIR}")
  endforeach()
endif()

ament_package()
