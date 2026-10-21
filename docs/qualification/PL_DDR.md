# Shared PL-DDR integration: W08-A

The first DDR gate connects a new portable AXI burst writer and the actual
LiteNVMe host-memory responder to one independent AXI RAM model, through the
responder's existing LiteX arbiter. Seeded source bytes are written through the
writer and read back through the responder's public PCIe request/completion
streams. Both clients observe the same byte dictionary.

This is the memory foundation for the capture path. The W03 stager, W04 owner,
NVMe command scheduler and physical DDR controller are not wired together here.
The [interface contract](../contracts/PL_DDR_W08A.md) defines ownership,
visibility and abort/recovery obligations. The writer's one outstanding burst
and the responder's complete-read buffering are correctness choices; measured
bandwidth and hardware resource use remain future gates.

Local evidence on 2026-10-21: all four profiles passed, 44 executed tests with
zero failures/errors/skips, using Icarus 12.0 and cocotb 2.0.1. LiteNVMe was
`ec77a9772e8ef32633acc21b658806c8f63780d0`; LitePCIe was
`bc01a294422f4f7a7ed723863e4304e96b42e2c8`. The explicit simulation-only run
recorded Yosys synthesis as unrun. The fork's focused responder suite separately
passed 38 tests. CI results must be checked for the exact proposed commit before
merging; these local results alone do not establish merge readiness.

## Reproduce

Use Python 3.12 and install `requirements-development.lock` in a dedicated venv.
Icarus (`iverilog` and `vvp`) is required. The default gate also requires Yosys:

```sh
.venv/bin/python scripts/sim_ddr.py
```

On a Mac without Yosys, simulation can be run explicitly:

```sh
.venv/bin/python scripts/sim_ddr.py --simulation-only
```

The runner fetches the immutable fork commits in `config/development.json`.
For existing clean checkouts, add `--litepcie-source /absolute/path/litepcie`
and `--litenvme-source /absolute/path/litenvme`. Wrong revisions or modified
dependency sources fail. Import origins and generator dependencies are checked.

The frozen matrix has four profiles: boundary64, boundary128, boundary256
(19 beats, non-power-of-two slot count, a burst crossing a 4 KiB boundary) and
nominal256 (128 beats per chunk, eight slots). Each executes the same 11 named
tests. These check independent AW/W stalls, concurrent responder reads, B/R
latency, exact bytes and guards, delayed visibility, invalid jobs, framing and
sequence faults, B error/BID mismatch, abort at several phases, fenced recovery,
and a late DDR read error followed by a healthy read.

`reports/ddr/manifest.json` binds source hashes, dependency commits, tool versions,
seeds, commands and results. `reports/ddr/ddr.xml` is the aggregate JUnit result.
Each profile retains generated RTL, JUnit and a fresh FST. The runner rejects
missing, skipped or failed tests and enforces subprocess timeouts. Simulation
alone has status `simulation_passed`; it cannot report a full gate pass.

The full CI gate additionally runs generic Yosys synthesis and checks for
latches and structural problems in the portable writer. The normal development
workflow runs the complete pinned LiteNVMe suite, targeted LitePCIe tests and
generated stream-DMA simulation; the portable workflow retains the unmodified
upstream baseline. These are separate required pieces of evidence.

## Dependency error fix and limits

The pinned fork includes a DDR read-error correction. All R responses and any
unaligned flush must retire before publishing a successful completion. An error
drains the buffered payload and emits one header-only error completion using
LitePCIe's existing unsupported-request mapping. Its separate fork tests check
the actual packetizer, maximum-length healthy/failed reads, backpressure and
recovery. The DDR fixture checks the generated responder at its abstract stream.

This gate uses wholly in-range, fully enabled DWORD requests. Complete-span
validation, PCIe partial byte enables and broader transport error reporting
remain W05 work. Successful B visibility is a model convention, with a separate
`visibility_ready` guard; the real controller must provide an ordering guarantee.
Recovery requires an external transport fence. Cold reset is permitted only
after the memory and transport domain are quiescent.

There is no device timing, SSD throughput, durability or lossless-camera claim.
The first hardware target is the [PZ17 SoM](../hardware/PZ17_TARGET.md). Its
vendor shell, clocks, DDR and PCIe routing must be checked before implementation.
