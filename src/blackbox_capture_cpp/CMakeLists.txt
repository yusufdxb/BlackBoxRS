cmake_minimum_required(VERSION 3.16)
project(blackbox_capture_cpp VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(BLACKBOX_CAPTURE_ENABLE_ASAN "Build with AddressSanitizer" OFF)
option(BLACKBOX_CAPTURE_ENABLE_UBSAN "Build with UndefinedBehaviorSanitizer" OFF)
option(BLACKBOX_CAPTURE_BUILD_FUZZERS "Build libFuzzer targets" OFF)

find_package(ament_cmake REQUIRED)
find_package(mcap_vendor REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(std_msgs REQUIRED)
find_package(Threads REQUIRED)

set(BLACKBOX_CAPTURE_WARNING_FLAGS -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)

function(blackbox_capture_harden target)
  target_compile_options(${target} PRIVATE ${BLACKBOX_CAPTURE_WARNING_FLAGS})
  if(BLACKBOX_CAPTURE_ENABLE_ASAN)
    target_compile_options(${target} PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(${target} PRIVATE -fsanitize=address)
  endif()
  if(BLACKBOX_CAPTURE_ENABLE_UBSAN)
    target_compile_options(${target} PRIVATE -fsanitize=undefined -fno-omit-frame-pointer)
    target_link_options(${target} PRIVATE -fsanitize=undefined)
  endif()
endfunction()

add_library(blackbox_capture_core SHARED
  src/segment_writer.cpp
  src/trigger_engine.cpp
)
target_include_directories(blackbox_capture_core
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)
target_link_libraries(blackbox_capture_core mcap_vendor::mcap Threads::Threads)
ament_target_dependencies(blackbox_capture_core rclcpp)
blackbox_capture_harden(blackbox_capture_core)

add_library(blackbox_capture_component SHARED src/recorder.cpp)
target_include_directories(blackbox_capture_component
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)
target_link_libraries(blackbox_capture_component blackbox_capture_core)
ament_target_dependencies(blackbox_capture_component rclcpp rclcpp_components std_msgs)
blackbox_capture_harden(blackbox_capture_component)
rclcpp_components_register_nodes(blackbox_capture_component "blackbox_capture::RecorderNode")

add_executable(blackbox_capture src/recorder_node.cpp)
target_link_libraries(blackbox_capture blackbox_capture_component)
ament_target_dependencies(blackbox_capture rclcpp)
blackbox_capture_harden(blackbox_capture)

add_executable(blackbox_capture_recover src/recovery_cli.cpp)
target_link_libraries(blackbox_capture_recover blackbox_capture_core)
blackbox_capture_harden(blackbox_capture_recover)

install(
  DIRECTORY include/
  DESTINATION include
)
install(
  DIRECTORY config/
  DESTINATION share/${PROJECT_NAME}/config
)
install(
  TARGETS blackbox_capture_core blackbox_capture_component blackbox_capture
    blackbox_capture_recover
  EXPORT export_blackbox_capture_cpp
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME}
  INCLUDES DESTINATION include
)

ament_export_targets(export_blackbox_capture_cpp HAS_LIBRARY_TARGET)
ament_export_dependencies(mcap_vendor rclcpp rclcpp_components std_msgs)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()

  foreach(test_name
      ring_buffer
      payload_arena
      topic_registry
      trigger_engine
      segment_writer
      recorder)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/test/test_${test_name}.cpp")
      ament_add_gtest(test_${test_name} test/test_${test_name}.cpp)
      if(TARGET test_${test_name})
        target_link_libraries(test_${test_name} blackbox_capture_core)
        if(test_name STREQUAL "recorder")
          target_link_libraries(test_${test_name} blackbox_capture_component)
          ament_target_dependencies(test_${test_name} rclcpp)
        endif()
        blackbox_capture_harden(test_${test_name})
      endif()
    endif()
  endforeach()
endif()

if(BLACKBOX_CAPTURE_BUILD_FUZZERS)
  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "BLACKBOX_CAPTURE_BUILD_FUZZERS requires Clang")
  endif()
  foreach(fuzz_name segment_parser event_decoder topic_metadata)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/fuzz/fuzz_${fuzz_name}.cpp")
      add_executable(fuzz_${fuzz_name} fuzz/fuzz_${fuzz_name}.cpp)
      target_link_libraries(fuzz_${fuzz_name} PRIVATE blackbox_capture_core)
      target_compile_options(fuzz_${fuzz_name} PRIVATE -fsanitize=fuzzer,address,undefined)
      target_link_options(fuzz_${fuzz_name} PRIVATE -fsanitize=fuzzer,address,undefined)
    endif()
  endforeach()
endif()

ament_package()
