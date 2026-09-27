add_executable(blackboxrs blackboxrs.cpp cmd_replay.cpp cmd_misc.cpp cmd_evidence.cpp)
target_link_libraries(blackboxrs PRIVATE blackboxrs::core)
blackboxrs_set_warnings(blackboxrs)
install(TARGETS blackboxrs RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
