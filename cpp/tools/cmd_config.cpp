#include <iostream>

#include "blackboxrs/runtime_config.hpp"
#include "cli_args.hpp"
#include "commands.hpp"

namespace blackboxrs::cli {

int cmd_config(const Argv& argv) {
  OptionSpec spec;
  spec.flags = {"--json"};
  const Args a(argv, spec);
  if (a.positional().size() != 1) {
    throw UsageError("config takes one runtime configuration file");
  }
  try {
    const RuntimeConfig c = load_runtime_config(a.positional()[0]);
    if (a.flag("--json")) {
      std::cout << c.to_json().dump(2) << "\n";
    } else {
      std::cout << "VALID  " << c.path << "\n"
                << "  sha256   " << c.sha256 << "\n"
                << "  profile  " << c.profile.name << " (" << c.profile.topics.size() << " topics) "
                << c.profile_path << "\n"
                << "  capture  "
                << (c.capture_mode == CaptureMode::continuous ? "continuous" : "triggered")
                << " into " << c.evidence_dir << "\n";
    }
    return 0;
  } catch (const ConfigError& exc) {
    std::cerr << "INVALID  " << exc.what() << "\n";
    return 5;
  }
}

}  // namespace blackboxrs::cli
