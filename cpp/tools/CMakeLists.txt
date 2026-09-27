add_executable(blackboxrs blackboxrs.cpp cmd_replay.cpp cmd_misc.cpp cmd_evidence.cpp cmd_benchmark.cpp cmd_config.cpp)
target_link_libraries(blackboxrs PRIVATE blackboxrs::core blackboxrs::bench)
blackboxrs_set_warnings(blackboxrs)
install(TARGETS blackboxrs RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
