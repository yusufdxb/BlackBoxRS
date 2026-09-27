// Minimal command-line parsing for the blackboxrs CLI. Options are declared
// up front; anything undeclared is a usage error (exit 2), never ignored.
#pragma once

#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace blackboxrs::cli {

class UsageError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct OptionSpec {
  std::set<std::string> flags;     // --name, no value
  std::set<std::string> values;    // --name VALUE (last one wins)
  std::set<std::string> repeated;  // --name VALUE, repeatable
};

class Args {
 public:
  Args(const std::vector<std::string>& argv, const OptionSpec& spec) {
    for (std::size_t i = 0; i < argv.size(); ++i) {
      const std::string& a = argv[i];
      if (a.rfind("--", 0) != 0 || a == "--" || a == "-") {
        positional_.push_back(a);
        continue;
      }
      std::string name = a;
      std::optional<std::string> inline_value;
      if (const auto eq = a.find('='); eq != std::string::npos) {
        name = a.substr(0, eq);
        inline_value = a.substr(eq + 1);
      }
      if (spec.flags.count(name) != 0U) {
        if (inline_value) {
          throw UsageError(name + " takes no value");
        }
        flags_.insert(name);
        continue;
      }
      const bool single = spec.values.count(name) != 0U;
      const bool multi = spec.repeated.count(name) != 0U;
      if (!single && !multi) {
        throw UsageError("unknown option " + name);
      }
      std::string value;
      if (inline_value) {
        value = *inline_value;
      } else {
        if (i + 1 >= argv.size()) {
          throw UsageError(name + " needs a value");
        }
        value = argv[++i];
      }
      if (single) {
        values_[name] = value;
      } else {
        repeated_[name].push_back(value);
      }
    }
  }

  [[nodiscard]] bool flag(const std::string& n) const { return flags_.count(n) != 0U; }
  [[nodiscard]] std::optional<std::string> value(const std::string& n) const {
    const auto it = values_.find(n);
    return it == values_.end() ? std::nullopt : std::optional<std::string>(it->second);
  }
  [[nodiscard]] std::vector<std::string> all(const std::string& n) const {
    const auto it = repeated_.find(n);
    return it == repeated_.end() ? std::vector<std::string>{} : it->second;
  }
  [[nodiscard]] const std::vector<std::string>& positional() const { return positional_; }

  [[nodiscard]] std::optional<double> number(const std::string& n) const {
    const auto v = value(n);
    if (!v) {
      return std::nullopt;
    }
    try {
      std::size_t pos = 0;
      const double d = std::stod(*v, &pos);
      if (pos != v->size()) {
        throw std::invalid_argument("trailing characters");
      }
      return d;
    } catch (const std::exception&) {
      throw UsageError(n + " expects a number, got '" + *v + "'");
    }
  }

 private:
  std::vector<std::string> positional_;
  std::set<std::string> flags_;
  std::map<std::string, std::string> values_;
  std::map<std::string, std::vector<std::string>> repeated_;
};

}  // namespace blackboxrs::cli
