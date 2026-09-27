find_package(GTest CONFIG QUIET)
if(NOT GTest_FOUND)
  # ROS 2 installs ship GoogleTest sources in gtest_vendor.
  set(_gtest_src "/usr/src/googletest")
  if(NOT EXISTS "${_gtest_src}/CMakeLists.txt" AND DEFINED ENV{AMENT_PREFIX_PATH})
    set(_gtest_src "/opt/ros/$ENV{ROS_DISTRO}/src/gtest_vendor")
  endif()
  add_subdirectory(${_gtest_src} ${CMAKE_CURRENT_BINARY_DIR}/googletest EXCLUDE_FROM_ALL)
  add_library(GTest::gtest_main ALIAS gtest_main)
endif()
include(GoogleTest)

set(BLACKBOXRS_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../..")

function(blackboxrs_test name)
  add_executable(${name} ${ARGN})
  target_link_libraries(${name} PRIVATE blackboxrs::core GTest::gtest_main)
  target_compile_definitions(${name} PRIVATE
    BLACKBOXRS_REPO_ROOT="${BLACKBOXRS_REPO_ROOT}")
  blackboxrs_set_warnings(${name})
  gtest_discover_tests(${name} DISCOVERY_TIMEOUT 60 PROPERTIES LABELS unit)
endfunction()

blackboxrs_test(test_core_basics test_time.cpp test_json.cpp test_integrity.cpp)
blackboxrs_test(test_evidence test_evidence.cpp test_payload.cpp)
blackboxrs_test(test_replay test_replay.cpp)
blackboxrs_test(test_recorder test_recorder.cpp)
blackboxrs_test(test_helix_parity test_helix_parity.cpp)
blackboxrs_test(test_config test_config.cpp)
