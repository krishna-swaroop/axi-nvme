# Ordered work packages

Each row is one bounded task/PR, not an instruction to implement all rows at once.
The bootstrap is complete only when the reproducible checks and publication are
verified. Actual host implementation starts with W01, after this scoping pass.

| ID | Depends on | Ownership / allowed paths | Deliverable and acceptance | Stop condition |
|---|---|---|---|---|
| W00 | — | `docs/`, `scripts/`, `tests/fixtures/`, `tests/cocotb/`, configuration and CI | Research, locks, public repo; check runner emits failure status, JUnit and waveform | Do not call the fixture an NVMe core |
| W01 | W00 | `tests/qualification/`, `scripts/qualify_*`, `docs/qualification/` | LiteNVMe gate: generated RTL checks, real stream cycle accounting, documented init/PRP/error gaps; retain adopt/reject decision | Missing transport features or unsupported license needs design review |
| W02 | W01 | `docs/contracts/`, `config/`, `tests/models/` | Freeze stream, descriptor, CSR, epoch and buffer contracts; independent Python NVMe storage oracle with byte-exact model tests | No guessed SSD capabilities or record format |
| W03 | W02 | `rtl/stream/`, `tests/stream/`, `formal/stream/` | Capture framing, width conversion, TLAST/TKEEP, async FIFO; stable-under-stall and reset tests; overflow accounting | No asynchronous multi-bit sampling or propagated camera pause |
| W04 | W02 | `rtl/buffer/`, `tests/buffer/`, `formal/buffer/` | Buffer allocator/ownership and BRAM model; randomized completion order, wrap, full/empty, reset epoch proofs | Any possible submitted-buffer overwrite |
| W05 | W01,W02 | `gateware/`, `rtl/transport/`, `tests/transport/` | Qualified host-memory/TLP adapter; split reads/completions, byte enables, tags, range checks and malformed/error traffic | Transport cannot service legal SSD DMA patterns |
| W06 | W04,W05 | `rtl/nvme/`, `gateware/`, `tests/nvme/` | Admin/config adapter, checked capability discovery, I/O queues, PRPs, Flush; QD/phase/wrap/error tests | Hardcoded device facts presented as universal support |
| W07 | W03,W04,W06 | `rtl/scheduler/`, `tests/integration/` | Overlap filling N+1, SSD DMA N and completion N−1; arbitrary-payload write/read scoreboard; no per-I/O CPU calls | Test model shares the DUT's command/PRP logic |
| W08 | W07 | `rtl/memory/`, `tests/stress/`, `config/profiles/` | PL DDR backend model and service-envelope tests; 1.25 GB/s plus record overhead, bounded stalls, recovery, explicit overflow | Average service ≤ ingest or unspecified burst bound |
| W09 | W07 | `rtl/control/`, `tests/control/`, `docs/contracts/` | AXI-Lite and counters; decoupled AW/W, strobes, atomic start/stop, snapshots, error status | Control change mutates active DMA state |
| W10 | W08,W09 + exact board | `boards/zu7eg/`, `constraints/`, `scripts/vivado/` | Board source/pin review, hard-IP/DDR generation, generator/checker, implementation DRC + constrained setup/hold closure | No routable PL PCIe x4 or unsupported part |
| W11 | W10 + Linux lab | `scripts/lab/`, `docs/hardware/`, `config/ssd/` | Identify + scratch write/read, QD sweep, long preconditioned capture, stall/thermal/error telemetry, Flush/power-cycle tests | Destructive range unassigned or any silent data mismatch |
| W12 | W11 | `rtl/capture/`, `tests/capture/`, docs/examples | Real receiver integration; define record metadata, capacity/end policy; measured lossless envelope | Claimed losslessness outside tested envelope |
| W13 | W12 | packaging, docs, releases | Reusable Vivado IP packaging, supported-device matrix, release evidence and independent review | Simulation-only claim promoted to hardware claim |

W03/W04 can run independently once W02 fixes their interfaces. W05 may proceed
in parallel with them under the same contract. W06/W07 integration must not alter
interfaces without updating their tests and consumers. Each implementation PR
needs `make check`, its package-specific tests and a reviewer; upstream changes
also require `make upstream-test` and a pinned delta/provenance record.

First W01 experiments:

- Compare upstream Migen simulations to generated RTL for host memory and streamer.
- Measure accepted AXIS bytes/cycles, including time waiting on storage; do not
  use request-generator throughput as an external-payload benchmark.
- Check zero/oversize count, early/late TLAST, namespace/LBA limits and reset.
- Independently inspect current RTL init against the 2100AT Identify/CAP profile
  once captured. Make unsupported configurations fail explicitly.
- Decide whether to extend the upstream stream engine or replace only that frontend.
