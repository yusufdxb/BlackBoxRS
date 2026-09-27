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
    # -Wuseless-cast is left out on purpose: it flags casts that are no-ops on
    # x86_64 but required on other targets (int64_t -> time_t, long, ...).
    list(APPEND _common -Wduplicated-cond -Wduplicated-branches -Wlogical-op)
  endif()
  target_compile_options(${target} PRIVATE ${_common})
  # Floating point must round exactly as CPython does (each operation
  # rounded on its own) for replays to be byte-identical to the Python
  # reference and across architectures. aarch64 (the Jetson) has fused
  # multiply-add, and GCC may contract a*b+c into one FMA with a single
  # rounding; forbid it.
  target_compile_options(${target} PRIVATE -ffp-contract=off)
  if(BLACKBOXRS_WERROR)
    target_compile_options(${target} PRIVATE -Werror)
  endif()
endfunction()
