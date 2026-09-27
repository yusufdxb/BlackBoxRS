#include "blackboxrs/json.hpp"

#include <charconv>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <vector>

namespace blackboxrs {
namespace {

std::vector<std::string_view> split_path(std::string_view dotted) {
  std::vector<std::string_view> parts;
  std::size_t start = 0;
  while (true) {
    const std::size_t dot = dotted.find('.', start);
    if (dot == std::string_view::npos) {
      parts.push_back(dotted.substr(start));
      return parts;
    }
    parts.push_back(dotted.substr(start, dot - start));
    start = dot + 1;
  }
}

}  // namespace

Numeric as_number(const Json* v) noexcept {
  if (v == nullptr || v->is_boolean() || v->is_null()) {
    return {0.0, NumState::malformed};
  }
  if (v->is_number()) {
    const double d = v->get<double>();
    return {d, std::isfinite(d) ? NumState::ok : NumState::nonfinite};
  }
  if (v->is_string()) {
    const auto& s = v->get_ref<const std::string&>();
    if (s == "NaN") {
      return {std::numeric_limits<double>::quiet_NaN(), NumState::nonfinite};
    }
    if (s == "Infinity") {
      return {std::numeric_limits<double>::infinity(), NumState::nonfinite};
    }
    if (s == "-Infinity") {
      return {-std::numeric_limits<double>::infinity(), NumState::nonfinite};
    }
  }
  return {0.0, NumState::malformed};
}

bool is_strict_int(const Json* v) noexcept {
  return v != nullptr && (v->is_number_integer() || v->is_number_unsigned());
}

const Json* get_path(const Json& data, std::string_view dotted) noexcept {
  const Json* cur = &data;
  for (std::string_view part : split_path(dotted)) {
    if (!cur->is_object()) {
      return nullptr;
    }
    const auto it = cur->find(part);
    if (it == cur->end()) {
      return nullptr;
    }
    cur = &*it;
  }
  return cur;
}

Json set_path(Json data, std::string_view dotted, Json value) {
  const auto parts = split_path(dotted);
  if (!data.is_object()) {
    data = Json::object();
  }
  Json* cur = &data;
  for (std::size_t i = 0; i + 1 < parts.size(); ++i) {
    Json& next = (*cur)[std::string(parts[i])];
    if (!next.is_object()) {
      next = Json::object();
    }
    cur = &next;
  }
  (*cur)[std::string(parts.back())] = std::move(value);
  return data;
}

Json del_path(Json data, std::string_view dotted) {
  const auto parts = split_path(dotted);
  Json* cur = &data;
  for (std::size_t i = 0; i + 1 < parts.size(); ++i) {
    if (!cur->is_object()) {
      return data;
    }
    const auto it = cur->find(parts[i]);
    if (it == cur->end() || !it->is_object()) {
      return data;
    }
    cur = &*it;
  }
  if (cur->is_object()) {
    cur->erase(std::string(parts.back()));
  }
  return data;
}

Json number_or_nonfinite(double v) {
  if (std::isnan(v)) {
    return "NaN";
  }
  if (std::isinf(v)) {
    return v > 0 ? "Infinity" : "-Infinity";
  }
  return v;
}

Json jsonable(Json v) {
  if (v.is_number_float()) {
    return number_or_nonfinite(v.get<double>());
  }
  if (v.is_object() || v.is_array()) {
    for (auto& item : v) {
      item = jsonable(std::move(item));
    }
  }
  return v;
}

std::string canonical_json(const Json& v) {
  // std::map-backed objects already iterate in sorted key order; the writer
  // turns non-finite floats into their string names itself.
  std::string out;
  write_json(out, v);
  return out;
}

void write_json(std::string& out, const Json& v) {
  write_json_any(out, v);
}

void append_py_float(std::string& out, double v) {
  if (std::isnan(v)) {
    out += "\"NaN\"";
    return;
  }
  if (std::isinf(v)) {
    out += v > 0 ? "\"Infinity\"" : "\"-Infinity\"";
    return;
  }
  out += py_float_repr(v);
}

std::string py_float_repr(double v) {
  // Shortest round-trip digits, then CPython's float_repr layout
  // (Python/pystrtod.c, format_float_short with mode 'r'): exponent form when
  // the decimal point position is <= -4 or > 16, a trailing ".0" otherwise.
  char buf[64];
  const auto res = std::to_chars(buf, buf + sizeof buf, v, std::chars_format::scientific);
  if (res.ec != std::errc()) {
    throw std::runtime_error("float formatting failed");
  }
  const std::string_view sci(buf, static_cast<std::size_t>(res.ptr - buf));
  const bool neg = sci.front() == '-';
  const std::size_t e_pos = sci.find('e');
  std::string digits;
  for (char c : sci.substr(neg ? 1 : 0, e_pos - (neg ? 1 : 0))) {
    if (c != '.') {
      digits.push_back(c);
    }
  }
  const int exp10 = std::atoi(std::string(sci.substr(e_pos + 1)).c_str());
  if (digits == "0") {
    return neg ? "-0.0" : "0.0";
  }
  const int decpt = exp10 + 1;
  const int ndigits = static_cast<int>(digits.size());
  std::string out = neg ? "-" : "";
  if (decpt <= -4 || decpt > 16) {
    out += digits.substr(0, 1);
    if (ndigits > 1) {
      out += '.';
      out += digits.substr(1);
    }
    const int e = decpt - 1;
    out += e < 0 ? "e-" : "e+";
    const int ae = e < 0 ? -e : e;
    if (ae < 10) {
      out += '0';
    }
    out += std::to_string(ae);
  } else if (decpt <= 0) {
    out += "0.";
    out.append(static_cast<std::size_t>(-decpt), '0');
    out += digits;
  } else if (decpt >= ndigits) {
    out += digits;
    out.append(static_cast<std::size_t>(decpt - ndigits), '0');
    out += ".0";
  } else {
    out += digits.substr(0, static_cast<std::size_t>(decpt));
    out += '.';
    out += digits.substr(static_cast<std::size_t>(decpt));
  }
  return out;
}

void append_json_string(std::string& out, std::string_view s) {
  static constexpr char kHex[] = "0123456789abcdef";
  auto u_escape = [&](unsigned cp) {
    out += "\\u";
    out += kHex[(cp >> 12U) & 0xFU];
    out += kHex[(cp >> 8U) & 0xFU];
    out += kHex[(cp >> 4U) & 0xFU];
    out += kHex[cp & 0xFU];
  };
  out += '"';
  for (std::size_t i = 0; i < s.size();) {
    const auto c = static_cast<unsigned char>(s[i]);
    if (c < 0x80U) {
      switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        default:
          if (c < 0x20U || c == 0x7FU) {
            u_escape(c);
          } else {
            out += static_cast<char>(c);
          }
      }
      ++i;
      continue;
    }
    // Decode one UTF-8 sequence; invalid bytes become U+FFFD, as Python's
    // decoder with errors="replace" would have produced when reading.
    unsigned cp = 0xFFFDU;
    std::size_t len = 1;
    if ((c & 0xE0U) == 0xC0U && i + 1 < s.size()) {
      cp = ((c & 0x1FU) << 6U) | (static_cast<unsigned char>(s[i + 1]) & 0x3FU);
      len = 2;
    } else if ((c & 0xF0U) == 0xE0U && i + 2 < s.size()) {
      cp = ((c & 0x0FU) << 12U) | ((static_cast<unsigned char>(s[i + 1]) & 0x3FU) << 6U) |
           (static_cast<unsigned char>(s[i + 2]) & 0x3FU);
      len = 3;
    } else if ((c & 0xF8U) == 0xF0U && i + 3 < s.size()) {
      cp = ((c & 0x07U) << 18U) | ((static_cast<unsigned char>(s[i + 1]) & 0x3FU) << 12U) |
           ((static_cast<unsigned char>(s[i + 2]) & 0x3FU) << 6U) |
           (static_cast<unsigned char>(s[i + 3]) & 0x3FU);
      len = 4;
    }
    i += len;
    if (cp >= 0x10000U) {
      cp -= 0x10000U;
      u_escape(0xD800U + (cp >> 10U));
      u_escape(0xDC00U + (cp & 0x3FFU));
    } else {
      u_escape(cp);
    }
  }
  out += '"';
}

}  // namespace blackboxrs
