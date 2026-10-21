# Stream-ring development qualification

Date: 2026-10-21. Scope: portable NVMe host prototype and reproducible feedback.
The immutable source revisions are in `config/development.json` and
`requirements-development.lock`. This is separate from the original upstream
baseline in `config/upstream.json` and `requirements.lock`.

## Implementation under qualification

The LiteNVMe fork contains a chunked stream ring: fill one slot while the SSD
fetches another and earlier commands complete. Slots have stable PCIe addresses;
completion tags match the owning slot; successful reads are drained in LBA order.
Control queues and payload buffers have separate host-memory responders.

External initialization exposes AXI-Lite and has no embedded soft CPU. The C
library enumerates/configures the SSD and starts a multi-chunk transfer. PL logic
handles payload, command submission and completion processing for that transfer.
An unbounded recorder still needs an autonomous descriptor/capture policy.

The existing whole-core test builds the C library against generated headers and
drives real AXI-Lite transactions into the Migen core. An independent raw-TLP SSD
model fetches commands/PRPs and transfers arbitrary payloads to/from its own
namespace state. The test checks initialization, pattern integrity, wrong-seed
detection, range rejection, queue behavior and reinitialization. The simulated
CSR layout is compared to an AMD-PHY build. Hard PCIe IP and transceivers are
represented by a model; FPGA implementation is not part of that test.

## Main-thread review findings addressed

1. **Failed read data exposure.** The ring previously treated an error completion
   as valid payload and drained the slot. A failed command can return no data,
   so this exposed stale memory. The corrected path emits only the successful
   prefix before the failed slot, suppresses that slot and later payload, drains
   already outstanding commands, then reports failed completion. It does not
   fabricate a successful TLAST. Consumers must observe completion/error status.
2. **AXI transfer-size encoding.** The pinned LiteX `AXSIZE` table encodes 32 and
   64 bytes as 6 and 7 rather than 5 and 6. The NVMe DMA/bridge/responder paths
   now derive size directly as `log2(bytes_per_beat)`. The first independent
   256-bit Icarus test reproduced `AWSIZE=6`, where 32 bytes requires 5.
3. **Qualification could import stale packages.** The runner verifies clean
   immutable source checkouts, checks effective package origins and generator
   dependency provenance, enforces ordinary Python version pins, removes stale
   JUnit files, and verifies sources again after the run. A pre-existing installed
   upstream package is never silently accepted as the stream-ring implementation.
4. **An errored write tainted an idle read checker.** Integrated checker abort
   now requires an active check as well as errored streamer retirement. The
   regression covers idle write failure and active truncated-read restart.

## Reproduce

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
make check
make upstream-test
make qualify
```

An isolated development installation can instead use
`requirements-development.lock`. To test existing sibling source checkouts use
`make qualify QUALIFY_ARGS='--source-root ..'`; their committed SHAs must match
the manifest. The runner does not reset or overwrite existing checkouts.

Evidence is written to `reports/development/`: manifest, per-command output,
JUnit and generated-RTL metadata. Icarus waveforms and generated Verilog are in
`build/qualification/`. Local results and CI links are recorded in BASELINE.md
after the corresponding checks finish.

Local manifests record project edits; they do not bind those edits to an exact
commit. Clean CI is the commit-bound evidence. Python versions and Git revisions
are enforced, but package artifact hashes and simulator binaries are not pinned.

The transport stage explicitly selects seven TLP/header/configuration test files;
it is not a claim that every LitePCIe board generator or endpoint feature was
qualified. The NVMe stage runs its full fork test directory. The generated RTL
stage uses an independent byte-addressed AXI RAM with separate AW/W stalls,
delayed B/R responses, output backpressure and repeated transfers, including
zero/one/odd/large beat counts at four data widths.

## Remaining gates

- The payload ring is BRAM-backed in the current default build. A PL DDR backend
  needs bandwidth, latency, burst and controller-error qualification.
- The source can be backpressured; an unpausable camera needs a capture ring,
  explicit overflow reporting and a bounded service envelope.
- Missing completions have no RTL timeout/abort recovery. A software timeout
  does not prove hardware DMA has stopped or buffers can be reused.
- Reset epochs, stale/duplicate completion fencing, general PCIe completion
  splitting, byte-enable edge cases, AXI error-response propagation, TLAST/length
  validation and production durability behavior need additional work packages.
- Board pin/clock/reset data, Vivado DRC/CDC/setup/hold closure and real SSD
  preconditioned write performance are unverified.

The decision is to continue with this fork as the simulated prototype. These
results do not qualify it as a lossless camera recorder or production NVMe IP.
