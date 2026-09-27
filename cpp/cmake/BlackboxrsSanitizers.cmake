# BLACKBOXRS_SANITIZER = address | undefined | thread | "" (none).
# address also enables LeakSanitizer. Applied to every target in the project
# through the directory-level options so tests and tools are instrumented too.
set(BLACKBOXRS_SANITIZER "" CACHE STRING "address, undefined, thread or empty")
set_property(CACHE BLACKBOXRS_SANITIZER PROPERTY STRINGS "" address undefined thread)

if(BLACKBOXRS_SANITIZER STREQUAL "address")
  add_compile_options(-fsanitize=address -fno-omit-frame-pointer -fno-optimize-sibling-calls)
  add_link_options(-fsanitize=address)
elseif(BLACKBOXRS_SANITIZER STREQUAL "undefined")
  add_compile_options(-fsanitize=undefined,float-cast-overflow -fno-sanitize-recover=all
                      -fno-omit-frame-pointer)
  add_link_options(-fsanitize=undefined,float-cast-overflow -fno-sanitize-recover=all)
elseif(BLACKBOXRS_SANITIZER STREQUAL "thread")
  add_compile_options(-fsanitize=thread -fno-omit-frame-pointer)
  add_link_options(-fsanitize=thread)
elseif(NOT BLACKBOXRS_SANITIZER STREQUAL "")
  message(FATAL_ERROR "unknown BLACKBOXRS_SANITIZER '${BLACKBOXRS_SANITIZER}'")
endif()
