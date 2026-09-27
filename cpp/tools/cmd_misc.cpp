#include <iostream>

#include "blackboxrs/build_info.hpp"
#include "cli_args.hpp"
#include "commands.hpp"

namespace blackboxrs::cli {

int cmd_version(const Argv& argv) {
  const Args a(argv, OptionSpec{});
  std::cout << "blackboxrs " << build_info::kVersion << "\n"
            << "git_sha " << build_info::kGitSha << (build_info::kGitDirty ? " (dirty)" : "")
            << "\n"
            << "build_type " << build_info::kBuildType << "\n"
            << "compiler " << build_info::kCompiler << "\n";
  return 0;
}


}  // namespace blackboxrs::cli
