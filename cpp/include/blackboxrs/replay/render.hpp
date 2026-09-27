// Plain-text rendering of a replay result (Python lab/render.py).
#pragma once

#include <cstddef>
#include <string>

#include "blackboxrs/json.hpp"

namespace blackboxrs::replay {

[[nodiscard]] std::string timeline_line(const Json& entry);
[[nodiscard]] std::string render_text(const Json& result, bool timeline,
                                      std::size_t max_lines = 200);

}  // namespace blackboxrs::replay
