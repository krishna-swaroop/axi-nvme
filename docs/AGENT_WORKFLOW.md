# Agent development workflow

The agent writes RTL, tests, build scripts and constraints; runs tools; inspects
failures/waveforms; and revises the implementation. The owner supplies hardware,
physical connections and requirements, rather than source code. Tests and
reviewable artifacts are the feedback mechanism, not chat assertions.

## Current runnable layer

Python 3.12 virtual environment, pinned `requirements.lock`, Icarus 12.x,
cocotb 2.0.1, pytest, and the pinned LiteNVMe/Migen evaluation. `make check` is
offline once dependencies are installed. `make upstream-test` fetches only its
recorded commit and refuses modified tracked source. CI runs both and retains
reports even on failure. This bootstrap has no production core or timing claim.

## Toolchain layers to add as RTL arrives

| Layer | Tool | Machine-readable feedback and limit |
|---|---|---|
| Fast syntax/lint | Verilator | Warnings/errors; warnings require explicit disposition |
| Protocol simulation | cocotb + Icarus/Verilator, pytest | JUnit, transaction trace, failing seed, FST/VCD, scoreboard |
| AXI/PCIe stimulus | cocotbext-axi / cocotbext-pcie | Bus transactions and link-interface models; not a complete NVMe SSD model |
| Generated upstream logic | Migen simulation plus generated-Verilog differential checks | Do not assume Python simulation and generated RTL behave identically |
| Safety/liveness properties | Yosys + SymbiYosys + SMT solver | Proof/counterexample, explicit fairness and bounded-liveness assumptions |
| Structural synthesis | Yosys | Inferred memories, latches, widths, resource/depth trends; not device signoff |
| Local timing planning | Cycle/throughput model and pipeline budgets | Rate headroom, occupancy and stall profiles; not post-route Fmax |
| FPGA synthesis/implementation | Vivado batch Tcl on Linux | Checkpoint, DRC, utilization, CDC, setup/hold and unconstrained-path reports |
| Real SSD acceptance | PL generator/checker + scripted register access | Payload hashes, byte counters, latency histogram, link/error telemetry |

[OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) distributes open
tools for macOS including Apple Silicon. Choose an immutable release and checksum
when introducing that layer; do not silently track nightly. It is not installed
by this bootstrap. [cocotb simulator support](https://docs.cocotb.org/en/stable/simulator_support.html)
must be checked when changing simulators. Vendor encrypted models are a separate
Vivado-supported simulation lane; don't assume they work in open simulators.

## Per-task execution contract

1. Select a ROADMAP package whose prerequisites pass. Write its file allowlist,
   behavioral contract, failure cases and exact acceptance commands into the PR.
2. Add the independent oracle/assertions first. Where possible demonstrate the
   failure against current logic or a deliberately broken fixture.
3. Implement a bounded module change. Run focused tests, then required regression.
4. On failure retain seed, command, tool version, trace and waveform. Agent reads
   the failing transaction and relevant signals, fixes the cause and reruns the
   same case before randomized expansion. A timeout is a failure.
5. Produce an evidence manifest: Git SHA + dirty state, dependency hashes, tool
   versions, test counts, seed/config, artifact paths, limits and outstanding risks.
6. A separate reviewer checks source and the oracle, particularly tag/phase/epoch
   handling, reset, buffer reuse and clock crossings. It must challenge the happy
   path and may request new tests. Independent agents can fill this role when
   implementation work is explicitly dispatched.
7. Merge only the exact tested revision with green required CI and review complete.
   A passing canary is never substituted for a feature's acceptance test.

If several agents are later used, assign disjoint modules and fixed interface
contracts. Integration owns ABI changes. Do not let multiple agents independently
invent queue formats, CSR maps or reset semantics.

## Public CI and future workstation

Public PR jobs have read-only repository permissions, no hardware/network secrets,
bounded runtime and SHA-pinned Actions. Dependency upgrades are reviewed and
rerun on both macOS and Linux. This bootstrap uses Ubuntu's Icarus package and
records its version; a later reproducible image must pin the simulator binary
and checksum as well as Python packages.

For Linux/Vivado use a dedicated unprivileged account and an allowlisted build
wrapper. Transfer a reviewed immutable Git SHA and manifest, generate vendor IP
locally, run Tcl noninteractively and return reports/checksums. Serialize JTAG
access with a device lock. Set timeouts and preserve failed build logs. Start
manually from trusted commits; do not attach a self-hosted workstation directly
to arbitrary public `pull_request` code or use `pull_request_target` to execute it.

The hardware wrapper must check board ID, FPGA part, SSD serial/namespace and
an explicitly provisioned scratch LBA range before writing. Hardware lifecycle
actions such as power cycling need a controllable relay or a physical operator;
agents cannot manufacture that access. All test/build/control software can be
agent-authored. Vitis is needed only for optional firmware workflows, not for
portable RTL simulation or an eventual CPU-less host.

## Release evidence

Publish portable sources, constraints, deterministic IP-generation Tcl, lockfiles,
test models, usage examples, bitstream build recipe, supported SSD/device matrix,
measured throughput and tail-latency conditions. Vendor-generated files are built
locally under their terms. A release must distinguish simulated, synthesized,
post-route and hardware-tested configurations.
