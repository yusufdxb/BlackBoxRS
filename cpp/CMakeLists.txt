cmake_minimum_required(VERSION 3.22)
project(blackboxrs_core VERSION 0.5.0 LANGUAGES CXX)

# BlackBoxRS core runtime: event and time model, evidence I/O, the bounded
# recording pipeline, deterministic replay, fault injection, detectors and
# invariants. No ROS dependency; ros2/blackboxrs_ros adds the rclcpp layer.

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")
include(BlackboxrsWarnings)
include(BlackboxrsSanitizers)
include(GNUInstallDirs)

option(BLACKBOXRS_BUILD_TESTS "Build GoogleTest suites" ON)
option(BLACKBOXRS_BUILD_TOOLS "Build the blackboxrs CLI" ON)
option(BLACKBOXRS_WERROR "Treat warnings as errors" OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)
find_package(nlohmann_json 3.10 REQUIRED)
find_package(yaml-cpp REQUIRED)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)

# Build provenance, regenerated at every build so the SHA is never stale.
set(BLACKBOXRS_BUILD_INFO "${CMAKE_CURRENT_BINARY_DIR}/generated/blackboxrs/build_info.hpp")
add_custom_target(blackboxrs_build_info
  COMMAND ${CMAKE_COMMAND}
          -DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}
          -DOUTPUT=${BLACKBOXRS_BUILD_INFO}
          -DBUILD_TYPE=${CMAKE_BUILD_TYPE}
          "-DCOMPILER=${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}"
          -DVERSION=${PROJECT_VERSION}
          -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/BuildInfo.cmake
  BYPRODUCTS ${BLACKBOXRS_BUILD_INFO}
  COMMENT "BlackBoxRS build info")

add_library(blackboxrs_core
  src/time.cpp
  src/json.cpp
  src/integrity.cpp
  src/profile.cpp
  src/payload.cpp
  src/event.cpp
  src/evidence/bundle.cpp
  src/evidence/record.cpp
  src/recorder/flight_core.cpp
  src/replay/faults.cpp
  src/replay/sut.cpp
  src/replay/monitors.cpp
  src/replay/liveness.cpp
  src/replay/timeline.cpp
  src/replay/engine.cpp
  src/replay/render.cpp
)
add_library(blackboxrs::core ALIAS blackboxrs_core)
add_dependencies(blackboxrs_core blackboxrs_build_info)
target_compile_features(blackboxrs_core PUBLIC cxx_std_20)
set_target_properties(blackboxrs_core PROPERTIES CXX_EXTENSIONS OFF POSITION_INDEPENDENT_CODE ON)
target_include_directories(blackboxrs_core
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/generated>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
target_link_libraries(blackboxrs_core
  PUBLIC nlohmann_json::nlohmann_json Threads::Threads
  PRIVATE yaml-cpp OpenSSL::Crypto)
blackboxrs_set_warnings(blackboxrs_core)

if(BLACKBOXRS_BUILD_TOOLS)
  add_subdirectory(tools)
endif()

if(BLACKBOXRS_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()

install(TARGETS blackboxrs_core EXPORT blackboxrs_coreTargets
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(FILES ${BLACKBOXRS_BUILD_INFO} DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/blackboxrs)
install(EXPORT blackboxrs_coreTargets NAMESPACE blackboxrs::
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/blackboxrs_core)
include(CMakePackageConfigHelpers)
configure_package_config_file(cmake/blackboxrs_coreConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/blackboxrs_coreConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/blackboxrs_core)
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/blackboxrs_coreConfigVersion.cmake
  COMPATIBILITY SameMinorVersion)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/blackboxrs_coreConfig.cmake
              ${CMAKE_CURRENT_BINARY_DIR}/blackboxrs_coreConfigVersion.cmake
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/blackboxrs_core)
