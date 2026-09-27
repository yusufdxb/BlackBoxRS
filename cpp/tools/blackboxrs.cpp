// blackboxrs: offline command-line interface of the C++ runtime.
//
// Every command here works without ROS. The live commands (record, monitor,
// preflight) are ROS 2 executables in ros2/blackboxrs_ros.
//
// Exit codes: 0 PASS/ok, 1 FAIL/invalid, 2 usage, 3 INCOMPLETE, 4 DETECTED,
// 5 malformed input (evidence, case, fault, config), 6 internal error.

#include <cstdio>
#include <exception>
#include <iostream>
#include <string>
#include <vector>

#include "cli_args.hpp"
#include "commands.hpp"

namespace {

constexpr const char* kUsage = R"(usage: blackboxrs <command> [options]

commands:
  replay    <case.json | bundle>   deterministic replay; causal timeline and verdict
  inject    <bundle> --fault SPEC  replay with injected faults (same options as replay)
  verify    [cases...]             run golden cases, check expectations and determinism
  faults                           list fault injectors and their parameters
  inspect   <bundle>               summarize a flight bundle
  validate  <bundle>               check evidence integrity (exit 0 valid, 1 invalid)
  benchmark [--profile go2] [--scale 1,2,5] [--json PATH]
                                   ingest / serialization / replay / detector benchmarks
  synth-record --out DIR [--seconds S] [--scale X]
                                   record the synthetic GO2 load through the real
                                   pipeline (compatibility tests; marked synthetic)
  config    <runtime.yaml>         validate a runtime configuration
  version                          build provenance

live commands (ROS 2): ros2 run blackboxrs_ros {recorder|monitor|replay|preflight}
)";

}  // namespace

int main(int argc, char** argv) {
  using namespace blackboxrs::cli;
  std::vector<std::string> args(argv + 1, argv + argc);
  if (args.empty() || args[0] == "-h" || args[0] == "--help" || args[0] == "help") {
    std::fputs(kUsage, args.empty() ? stderr : stdout);
    return args.empty() ? 2 : 0;
  }
  const std::string cmd = args[0];
  const Argv rest(args.begin() + 1, args.end());
  try {
    if (cmd == "replay") return cmd_replay(rest, false);
    if (cmd == "inject") return cmd_replay(rest, true);
    if (cmd == "verify") return cmd_verify(rest);
    if (cmd == "faults") return cmd_faults(rest);
    if (cmd == "inspect") return cmd_inspect(rest);
    if (cmd == "validate") return cmd_validate(rest);
    if (cmd == "benchmark") return cmd_benchmark(rest);
    if (cmd == "synth-record") return cmd_synth_record(rest);
    if (cmd == "config") return cmd_config(rest);
    if (cmd == "version") return cmd_version(rest);
    std::cerr << "unknown command '" << cmd << "'\n" << kUsage;
    return 2;
  } catch (const UsageError& exc) {
    std::cerr << "usage error: " << exc.what() << "\n";
    return 2;
  } catch (const std::exception& exc) {
    // A bug must never read as a verdict.
    std::cerr << "internal error (not a verdict): " << exc.what() << "\n";
    return 6;
  }
}
