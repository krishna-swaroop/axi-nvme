# Project scope and feasibility

Research date: 2026-09-17. This is an engineering proposal, not a compliance or
performance certification. Hardware results quoted below belong to upstream
authors; our executed evidence is in [BASELINE.md](BASELINE.md).

## Recommendation

Build an open, reusable capture/storage IP around a qualified NVMe host engine.
Start by evaluating LiteNVMe and improving its stream/buffer scheduler, rather
than immediately reimplementing enumeration, queues and PCIe transactions.
Keep the NVMe transport behind a fixed adapter so evaluation can reject it
without rewriting capture logic. DUNE/pl-nvme is a serious alternative if a
GPL-based VHDL implementation is preferred. No backend is accepted yet.

The first real milestone is a reproducible simulated write/read path with an
independent SSD model and uninterrupted input under a specified service envelope.
A clean upstream unit-test run is useful baseline evidence, but is insufficient
for adopting a controller in a non-pausable recorder.

## Requirements confirmed by the owner

| Requirement | Scope |
|---|---|
| Sensor link | 20 CML SerDes channels; approximately 40 Gbit/s effective incoming rate |
| Existing receiver | Strips/processes/discards data; outside this project's initial scope |
| Storage input | Approximately 8–10 Gbit/s; source cannot pause |
| Payload | AXI4-Stream write input and read output |
| Runtime | No CPU/Linux/PS DDR involvement in steady-state I/O |
| Configuration | Optional MicroBlaze/PS AXI4-Lite control is acceptable |
| Prototype assets | ZCU102 and additional ZU7EG devkits, FPGA Drive FMC Gen4, Micron 2100AT 1 TB M.2 modules |
| First validation source | PL-generated deterministic data, then real receiver |
| Development | Agents write code and consume machine-readable tool feedback |
| Initial host | macOS; Linux with Vivado/Vitis to be provided later |
| Publication | Public open-source repository and reusable IP |

Exact SSD part number/firmware, FMC revision, retention duration, worst burst
rate, record boundaries and tolerable outage remain unconfirmed. The stated
8–10 Gbit/s must become a maximum-rate/burst contract; averages cannot size FIFOs.

## Open-source options: host versus endpoint matters

| Project | What it actually provides | License observed | Disposition |
|---|---|---|---|
| [LiteNVMe](https://github.com/enjoy-digital/litenvme) | SSD-facing host; LitePCIe RootPort; I/O engine; AXI stream; optional RTL init | BSD-2-Clause | First candidate; source and unmodified tests inspected |
| [DUNE/pl-nvme](https://github.com/DUNE/pl-nvme) | VHDL SSD storage host, dual-drive stream distribution, configuration and simulation modules | GPL-3.0-or-later in source | Strong alternative; evaluate separately before importing |
| [mcrl/NVMe](https://github.com/mcrl/NVMe) | SystemVerilog host on ZU19EG/250-SoC using an OcuLink root path; autonomous bring-up and synthetic/addressable payload handling | BSD-style three-clause top-level notice | Useful second candidate; requires stream integration and per-file provenance review |
| [LitePCIe](https://github.com/enjoy-digital/litepcie) | PCIe transport including RootPort support used by LiteNVMe | BSD-2-Clause | Transport building block, not NVMe itself |
| [verilog-pcie](https://github.com/alexforencich/verilog-pcie) | PCIe TLP, DMA and vendor-interface building blocks | MIT | Components/models, not a turnkey autonomous NVMe host; check upstream maintenance/successor license before adoption |
| [NVMeCHA](https://github.com/yhqiu16/NVMeCHA) | FPGA presents an NVMe controller to a host PC; evaluated with SPDK on KCU105 | GPL-3.0 | Wrong side of the link for directly driving an SSD |
| [OpenExpress](https://www.usenix.org/conference/atc20/presentation/jung) | Research NVMe controller/SSD-side work | Not adopted or license-qualified here | Do not confuse NVMe controller endpoints with hosts |
| [FastPath_MP](https://github.com/beehive-lab/FastPath_MP) | FPGA direct-storage research with C API and modified Linux NVMe driver on Zynq-7000 | Apache-2.0 FPGA/library; GPLv2 modified driver per README | Useful architecture reference; published system retains host software |
| [SNAcc](https://www.esa.informatik.tu-darmstadt.de/assets/publications/materials/2025/2025_H2RC_DV.pdf) | Streaming NVMe access over a host PCIe P2P topology with FPGA/host memory variants | Not source/license-qualified here | Relevant buffer/scheduler research; host-root/IOMMU topology differs from standalone SSD root port |
| Linux, SPDK, bare-metal NVMe drivers | Software hosts and useful protocol/bring-up references | Project dependent | Useful characterization tools, not the required runtime datapath |
| Opsero reference designs | Board wiring and processor-led PCIe/NVMe integration | Review exact files before reuse | Board evidence; not a CPU-free host engine |

No commercial demonstration wrapper is treated as open-source implementation
of its underlying paid IP. Public source visibility alone does not grant a license.

### LiteNVMe findings at ce9c478c3df794685e50bab503ab4a7764b6f983

Upstream [validation](https://github.com/enjoy-digital/litenvme/blob/ce9c478c3df794685e50bab503ab4a7764b6f983/doc/VALIDATION.md)
reports 46 simulation tests and hardware results on Alibaba XCKU3P plus Crucial
CT500P310SSD8, Gen3 x4/256 bits at 125 MHz, approximately 2.69 GB/s reads and
2.74 GB/s writes at 8 KiB/QD32. Those are not results on Micron 2100AT or ZCU102.

Source inspection of [block.py](https://github.com/enjoy-digital/litenvme/blob/ce9c478c3df794685e50bab503ab4a7764b6f983/litenvme/block.py)
shows a stream fill stage followed by one submitted command and completion wait.
Read commands likewise complete into staging before stream draining. The DMA
issues single-beat AXI transactions, waiting for each response. Therefore the
engine's QD32 benchmark cannot be attributed to sustained external stream traffic.
We need overlapping capture, SSD DMA and command completion with separately
owned buffers and a high-throughput memory backend.

The pinned standalone documentation also records a firmware/CSR naming mismatch
as a follow-up, fixed assumptions in RTL initialization, a bounded staging window,
and no general multi-chunk streaming. Runtime length/namespace/PRP bounds need
independent validation rather than reliance on documented preconditions.

The existing block-streamer tests inspect staged payload or preload read data and
model completions. They do not establish a complete real SSD payload round-trip.
The new end-to-end scoreboard must model SSD DMA independently.

### DUNE and mcrl findings

DUNE at `971d95f5c89ad6e9960fae62d51cd82545856706` includes
`src/NvmeStorage.vhd`, AXI-Lite configuration, streams, two storage units and an
explicit block-drop input/accounting interface. It is closer to this application
than an endpoint emulator. Its top-level comments expose fixed device parameters
and a 250 MHz domain. It was source-inspected, not simulated here. GPL is a valid
open-source choice, but importing it changes the project's permissive reuse plan.

mcrl at `789c8caf419fa70a9a1a5041f009e1b17206b8a3` describes hardware-managed
NVMe commands and board-specific AXI/PCIe integration. Its README reports hardware
throughput, but pattern generators and counters are not evidence of arbitrary
camera-payload integrity. No mcrl performance claim has been reproduced here.

## Throughput and capacity

8–10 Gbit/s = 1.0–1.25 GB/s, before our record metadata/padding. Gen3 x4 gives
`4 × 8e9 × 128/130 / 8 = 3.938 GB/s` after line coding, before TLP/DLLP/flow-control
overheads. This is adequate transport headroom, not a promised SSD media rate.
256 bits at 125 MHz gives 4 GB/s raw stream capacity; arbitration and stalls
determine usable bandwidth. We will sweep transfer size and QD against the real
SSD rather than assume the largest value is best.

Micron's [2100AI/AT catalog](https://www.micron.com/products/storage/ssd/automotive-industrial-ssd-storage/2100at-ssd/part-catalog)
lists family headline figures up to 2.0 GB/s read and 1.8 GB/s write. Catalog rows
are not sustained-media or tail-latency guarantees. Resolve the exact 1 TB module
part number/firmware; measure after cache exhaustion, at the intended temperature,
occupancy and wear. If long-term service is below ingest, buffers only delay loss.

| Zero-service interval at 10 Gbit/s | Payload accumulated |
|---|---:|
| 1 ms | 1.25 MB |
| 10 ms | 12.5 MB |
| 100 ms | 125 MB |
| 1 s | 1.25 GB |

A 256 MiB payload ring covers about 214.75 ms with no reserve. With an illustrative
1.75 GB/s service rate, draining a 125 MB backlog while ingest continues at
1.25 GB/s takes 250 ms. A second stall before recovery adds to the backlog.
Reserve memory for outstanding commands, alignment, metadata and controller
latency. `make budget` makes these assumptions explicit and recalculable.

One decimal TB holds at most 800 seconds (13 min 20 s) at 10 Gbit/s before
metadata/reserved capacity. Decide stop-at-end versus overwrite-oldest explicitly;
silent wraparound is forbidden. Multi-drive striping is a later option if one
drive's sustained service or capacity is insufficient.

## Hardware and timing feasibility

AMD's [MPSoC selection guide, EG table page 4](https://docs.amd.com/api/khub/documents/2rKzwvn3YbmuIyp_BKxBGA/content)
lists no PL PCIe integrated block for ZU9EG. Opsero's
[compatibility note 3](https://docs.opsero.com/op063/datasheet/compatibility/)
explicitly applies that limitation to both ZCU102 FMC slots. GTH presence alone
does not supply the PCIe link/controller block.

The owner reports that ZU9EG has a suitable root-port hard IP; this conflicts with
the cited device documentation. Obtain the working project's exact part, XCI/IP
VLNV/configuration and lane constraints before declaring this resolved. We do not
infer the existing Linux/XDMA design's topology without those files.

The owner subsequently confirmed access to ZU7EG devkits. **An available ZU7EG
board is now the preferred first SSD prototype**, pending exact board/part and
accessible x4 lane qualification. AMD's EG table lists PL PCIe blocks for ZU7EG.
Do not assume these kits are ZCU106 (which uses ZU7EV) or reuse its pins blindly.
ZCU102 remains useful for portable logic and PL DDR testing. A full soft PCIe Gen3
root complex on GTH is a substantial separate project, not a board-wrapper tweak.
PS PCIe is a different architecture that violates the strict no-PS transport goal.
No hardware purchase is necessary to proceed with simulation.

AMD [PG213](https://docs.amd.com/r/en-US/pg213-pcie4-ultrascale-plus/Introduction)
is the relevant UltraScale+ integrated-PCIe reference; PG156 applies to UltraScale
Gen3 blocks and must not be substituted blindly. Hard-block locations, GT quads,
clock routing, lane mapping and actual device support are separate qualification gates.

macOS: Icarus/cocotb, Migen simulation, Verilator lint/coverage, Yosys structural
checks and SymbiYosys properties are practical open-source tools. OpenSTA requires
a mapped library/timing model and cannot certify UltraScale+ routing from generic
cells. Real setup/hold, CDC/RDC, GT constraints and post-route timing require Vivado.

## Delivery scope

Version 0.1: one SSD, Gen3 x4 on a suitable device, one namespace, one admin and
one I/O queue pair, configurable QD, PRP-backed aligned block read/write, Flush,
validated Identify/capability parsing, polling completions, autonomous steady-state
scheduler, bounded capture ring, error telemetry and deterministic PL test pattern.
Accept only supported 512/4096-byte LBA formats with no metadata/protection
information; reject others. Configuration can use firmware initially.

Later: full RTL init compatibility, multiple I/O queue pairs, multiple SSDs,
striping, richer telemetry, vendor-neutral transport wrappers and optimized
resource variants. Initial exclusions: filesystem, NVMe-oF, ZNS, virtualization,
arbitrary switches/hotplug, Gen4 tuning, format/sanitize and transparent retry
after ambiguous write failures. These can be separately specified later.

Plan by acceptance gates, not speculative agent-hour estimates. Protocol model,
buffer lifetime, PCIe error recovery and hardware qualification dominate risk;
more generated RTL is not itself evidence of progress.
