# Warning flags for BlackBoxRS targets. Applied per target, never globally, so
# third-party headers pulled in as SYSTEM includes are not affected.
function(blackboxrs_set_warnings target)
  set(_common
      -Wall -Wextra -Wpedantic
      -Wconversion -Wsign-conversion
      -Wshadow -Wnon-virtual-dtor -Wold-style-cast -Wcast-align
      -Woverloaded-virtual -Wnull-dereference -Wdouble-promotion
      -Wformat=2 -Wimplicit-fallthrough)
  if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    list(APPEND _common -Wduplicated-cond -Wduplicated-branches -Wlogical-op
         -Wuseless-cast)
  endif()
  target_compile_options(${target} PRIVATE ${_common})
  if(BLACKBOXRS_WERROR)
    target_compile_options(${target} PRIVATE -Werror)
  endif()
endfunction()
