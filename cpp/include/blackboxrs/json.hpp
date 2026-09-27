// JSON values at the evidence boundary, and the numeric rules applied to them.
//
// Flight records are JSON. The payload of a message record is kept as a Json
// value because it is the durable evidence (like a serialized CDR buffer);
// logic never reads it directly, it reads the typed view built by
// decode_payload (event.hpp). This header holds the few rules both sides
// need: how a stored value is read as a number, dotted-path access for fault
// injection, and a byte-stable canonical encoding for digests.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace blackboxrs {

// std::map-backed: object keys iterate in sorted order, so iteration over a
// Json object is deterministic. Never replaced by an unordered variant.
using Json = nlohmann::json;

enum class NumState : std::uint8_t {
  ok,         // a finite number
  nonfinite,  // NaN or +/-Inf (value holds it)
  malformed,  // missing, a bool, a string that is not "NaN"/"Infinity", an object ...
};

// A numeric field as stored. A malformed field is data, not an exception:
// the arbitration model, the monitors and the fault injectors each decide
// what a malformed field means for them.
struct Numeric {
  double value = 0.0;
  NumState state = NumState::malformed;

  [[nodiscard]] constexpr bool finite() const noexcept { return state == NumState::ok; }
  [[nodiscard]] constexpr bool problem() const noexcept { return state != NumState::ok; }
  friend constexpr bool operator==(const Numeric&, const Numeric&) = default;
};

// Python blackboxrs.lab.values.as_number. `v == nullptr` means "missing".
// bool is malformed: a bool in a velocity field is a type error, not 0 or 1.
[[nodiscard]] Numeric as_number(const Json* v) noexcept;

// Integer field the way Python's isinstance(v, int) and not isinstance(v, bool)
// reads it. Floats (even 3.0) are not integers.
[[nodiscard]] bool is_strict_int(const Json* v) noexcept;

// Dotted path into nested objects. nullptr when any component is missing or a
// non-object is traversed (Python values.get_path).
[[nodiscard]] const Json* get_path(const Json& data, std::string_view dotted) noexcept;

// Copy of `data` with `dotted` set to `value`; intermediate non-objects are
// replaced by objects (Python values.set_path).
[[nodiscard]] Json set_path(Json data, std::string_view dotted, Json value);

// Copy of `data` without `dotted`; a missing path is a no-op (Python del_path).
[[nodiscard]] Json del_path(Json data, std::string_view dotted);

// Floats that JSON cannot carry become the strings "NaN", "Infinity",
// "-Infinity", recursively (Python values.jsonable).
[[nodiscard]] Json jsonable(Json v);

// Sorted keys, no whitespace, ASCII only, non-finite floats as strings.
// Byte-identical to Python's json.dumps(jsonable(v), sort_keys=True,
// separators=(",", ":"), ensure_ascii=True) for the values evidence holds.
[[nodiscard]] std::string canonical_json(const Json& v);

// Append the compact JSON encoding of `v` to `out`. Floats use Python's
// repr() (shortest round-trip digits, the same fixed/exponent switch), so C++
// output matches what the Python tools write and hash. Non-finite floats are
// written as the strings "NaN", "Infinity", "-Infinity". Object keys come out
// in the value's own iteration order (sorted for Json).
void write_json(std::string& out, const Json& v);
template <class BasicJson>
void write_json_any(std::string& out, const BasicJson& v);

// Python repr() of a finite double: "0.1", "1e-05", "100.0", "1e+16".
[[nodiscard]] std::string py_float_repr(double v);
void append_py_float(std::string& out, double v);
// JSON string literal with Python's ensure_ascii escaping.
void append_json_string(std::string& out, std::string_view s);

// Python str() of a decoded JSON value, for display text that must match the
// Python tools: strings bare, containers as repr() (single-quoted keys and
// strings, True/False/None, float repr), objects in their stored key order.
template <class BasicJson>
[[nodiscard]] std::string py_str_any(const BasicJson& v, bool top = true);

// Python-compatible float for a value that must round trip through JSON:
// a double that is NaN or infinite becomes the corresponding string.
[[nodiscard]] Json number_or_nonfinite(double v);

template <class BasicJson>
void write_json_any(std::string& out, const BasicJson& v) {
  switch (v.type()) {
    case nlohmann::json::value_t::null:
    case nlohmann::json::value_t::discarded: out += "null"; return;
    case nlohmann::json::value_t::boolean: out += v.template get<bool>() ? "true" : "false"; return;
    case nlohmann::json::value_t::number_integer:
      out += std::to_string(v.template get<std::int64_t>());
      return;
    case nlohmann::json::value_t::number_unsigned:
      out += std::to_string(v.template get<std::uint64_t>());
      return;
    case nlohmann::json::value_t::number_float:
      append_py_float(out, v.template get<double>());
      return;
    case nlohmann::json::value_t::string:
      append_json_string(out, v.template get_ref<const std::string&>());
      return;
    case nlohmann::json::value_t::array: {
      out += '[';
      bool first = true;
      for (const auto& x : v) {
        if (!first) {
          out += ',';
        }
        first = false;
        write_json_any(out, x);
      }
      out += ']';
      return;
    }
    case nlohmann::json::value_t::object: {
      out += '{';
      bool first = true;
      for (auto it = v.begin(); it != v.end(); ++it) {
        if (!first) {
          out += ',';
        }
        first = false;
        append_json_string(out, it.key());
        out += ':';
        write_json_any(out, it.value());
      }
      out += '}';
      return;
    }
    case nlohmann::json::value_t::binary: out += "null"; return;
  }
}

template <class BasicJson>
std::string py_str_any(const BasicJson& v, bool top) {
  switch (v.type()) {
    case nlohmann::json::value_t::null: return "None";
    case nlohmann::json::value_t::boolean: return v.template get<bool>() ? "True" : "False";
    case nlohmann::json::value_t::number_integer:
      return std::to_string(v.template get<std::int64_t>());
    case nlohmann::json::value_t::number_unsigned:
      return std::to_string(v.template get<std::uint64_t>());
    case nlohmann::json::value_t::number_float: {
      const double d = v.template get<double>();
      if (d != d) return "nan";
      if (d == 1.0 / 0.0) return "inf";
      if (d == -1.0 / 0.0) return "-inf";
      return py_float_repr(d);
    }
    case nlohmann::json::value_t::string:
      return top ? v.template get<std::string>() : "'" + v.template get<std::string>() + "'";
    case nlohmann::json::value_t::array: {
      std::string s = "[";
      bool first = true;
      for (const auto& x : v) {
        s += (first ? "" : ", ") + py_str_any(x, false);
        first = false;
      }
      return s + "]";
    }
    case nlohmann::json::value_t::object: {
      std::string s = "{";
      bool first = true;
      for (auto it = v.begin(); it != v.end(); ++it) {
        s += (first ? "'" : ", '") + it.key() + "': " + py_str_any(it.value(), false);
        first = false;
      }
      return s + "}";
    }
    default: return "None";
  }
}

}  // namespace blackboxrs
