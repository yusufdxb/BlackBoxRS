// blackboxrs CLI commands. Each returns the process exit code.
#pragma once

#include <string>
#include <vector>

namespace blackboxrs::cli {

using Argv = std::vector<std::string>;

int cmd_replay(const Argv& args, bool inject_mode);
int cmd_verify(const Argv& args);
int cmd_faults(const Argv& args);
int cmd_inspect(const Argv& args);
int cmd_validate(const Argv& args);
int cmd_benchmark(const Argv& args);
int cmd_config(const Argv& args);
int cmd_version(const Argv& args);

}  // namespace blackboxrs::cli
