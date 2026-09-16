# axi-nvme

An open-source project for FPGA-resident NVMe storage on AMD UltraScale+
devices, with AXI4-Stream payloads and no CPU, Linux, or PS DDR in the data path.

**Status: research and executable verification bootstrap. No production NVMe
core, board bitstream, or timing closure is delivered yet.**

The first application is a non-pausable camera stream of 8–10 Gbit/s after
processing. A PL test generator will precede camera integration. Configuration
may use a small soft CPU or PS over AXI4-Lite; steady-state command submission,
DMA service, completions, and buffer management must operate autonomously.

## Initial findings

- **Evaluate and extend LiteNVMe first.** Its BSD-2-Clause NVMe host includes
  a LitePCIe root port, hardware I/O engine, stream frontend and optional RTL
  initialization. The existing frontend serializes fill/command/completion;
  its headline hardware benchmark is not our continuous-stream acceptance test.
- **ZCU102 is not a direct target for that backend.** Its ZU9EG has no PL PCIe
  hard block. Opsero explicitly records this limitation for FPGA Drive FMC Gen4.
  ZCU102 can exercise capture/buffering, but direct SSD validation needs a
  suitable device or a separately validated PCIe implementation. See
  [hardware feasibility](boards/zcu102/README.md).
- **Preferred prototype: an available ZU7EG devkit.** The owner also has ZU7EG
  boards, whose devices include PL PCIe. Exact board/part/slot routing still needs
  qualification; see [ZU7EG target record](boards/zu7eg/README.md).
- **Non-pausable ingest needs stall capacity.** 10 Gbit/s is 1.25 GB/s; 100 ms
  with no SSD progress consumes 125 MB. Plan PL-attached DDR, while keeping the
  user-facing payload interface AXI4-Stream. No finite buffer guarantees capture
  through an unbounded outage or indefinitely inadequate SSD write bandwidth.
- macOS can validate portable logic and cycle budgets. UltraScale+ device timing
  signoff requires Vivado and actual constraints on the future Linux workstation.

## Start here

1. [Scope, alternatives and evidence](docs/PROJECT_SCOPE.md)
2. [Architecture and interface contracts](docs/ARCHITECTURE.md)
3. [Agent development workflow](docs/AGENT_WORKFLOW.md)
4. [Ordered implementation work packages](docs/ROADMAP.md)
5. [Validation matrix](docs/VERIFICATION.md)
6. [Initial measured results](docs/BASELINE.md)

## Run the feedback loop

Prerequisites: Python 3.12, Git, GNU Make, Icarus Verilog 12.x (`iverilog`, `vvp`).
On macOS Icarus can be installed with `brew install icarus-verilog`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
make check
make upstream-test          # downloads the exact upstream revision on first use
make budget
```

`make check` runs budget-model tests and an Icarus/cocotb AXI-stream harness
canary, writing JUnit, a waveform, tool versions, and logs to `reports/`.
The canary is a verification fixture, not NVMe RTL. `make upstream-test` runs
the unmodified LiteNVMe suite using pinned dependencies. See the baseline for
what this proves and what remains untested. All failures produce nonzero exits.

## Licensing and boundaries

Original project code and documentation use BSD-2-Clause. Upstream dependencies
retain their own notices; see [third-party inventory](docs/THIRD_PARTY.md).
AMD PCIe/DDR primitives and Vivado remain vendor dependencies, generated locally
from scripts in a later milestone. Open-source host RTL does not mean an
open-source FPGA silicon stack. No vendor-generated IP is committed here.
