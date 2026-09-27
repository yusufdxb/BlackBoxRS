#include "blackboxrs/time.hpp"

#include <cmath>
#include <limits>
#include <stdexcept>

namespace blackboxrs {

std::int64_t seconds_to_ns(double seconds) {
  // nearbyint under the default FE_TONEAREST mode rounds half to even, which
  // is what Python's round() does; the product is the same IEEE double.
  const double ns = std::nearbyint(seconds * 1e9);
  if (!std::isfinite(ns) || ns > static_cast<double>(std::numeric_limits<std::int64_t>::max()) ||
      ns < static_cast<double>(std::numeric_limits<std::int64_t>::min())) {
    throw std::out_of_range("time in seconds does not fit in int64 nanoseconds");
  }
  return static_cast<std::int64_t>(ns);
}

}  // namespace blackboxrs
