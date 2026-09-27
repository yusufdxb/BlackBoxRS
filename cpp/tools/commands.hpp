// blackboxrs CLI commands. Each returns the process exit code.
#pragma once

#include <string>
#include <vector>

namespace blackboxrs::cli {

using Argv = std::vector<std::string>;

int cmd_replay(const Argv& argv, bool inject_mode);
int cmd_verify(const Argv& argv);
int cmd_faults(const Argv& argv);
int cmd_inspect(const Argv& argv);
int cmd_validate(const Argv& argv);
int cmd_benchmark(const Argv& argv);
int cmd_synth_record(const Argv& argv);
int cmd_config(const Argv& argv);
int cmd_version(const Argv& argv);

}  // namespace blackboxrs::cli
