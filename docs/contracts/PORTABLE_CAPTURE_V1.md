# Portable capture and ownership contract, version 1

W02 freezes the interfaces below for W03/W04 simulation. These are project
interfaces, not a camera wire format, an on-SSD record format, or a claim about
the Micron drives. W01's qualified prototype remains a separate backend.

## Scope and package boundaries

| Package | Owned files | Observable result | Acceptance / stop condition |
|---|---|---|---|
| W02 | This file, `config/profiles/synthetic_capture_v1.json`, `tests/models/`, `tests/test_storage_oracle.py` | Explicit geometry and byte-exact independent storage oracle | Bad geometry/ranges cannot mutate storage; cache, Flush and power loss differ; never infer SSD facts |
| W03 | `rtl/stream/`, `tests/stream/`, dedicated runner/workflow | Non-pausable fixed-chunk staging in synchronous RAM | Complete ordered chunks or explicit loss; stable stalled output; no source backpressure |
| W04 | `rtl/buffer/`, `tests/buffer/`, dedicated runner/workflow | Buffer ownership, watchdog and fenced recovery | Never reuse an owned/quarantined slot; stale handles cannot release a new lease |

W02 also owns `requirements-portable.lock`, `.gitignore`, `scripts/check.py`, `Makefile`,
`.github/workflows/contracts.yml` and trigger filters in the existing CI workflow:
these connect its oracle to automatic checks. Portable-only changes use light
path-filtered jobs; changing the upstream integration still runs its full gate.

Each is a separate branch/PR. No vendor shell, existing LiteNVMe pins, or board
files change. Focused local jobs run serially; full upstream qualification stays
on GitHub CI. Future DDR integration must satisfy the visibility/fencing rules
below before these blocks can be called an end-to-end recorder.

## Synthetic profile

The machine-readable profile explicitly supplies every simulation dimension.
Its 512-byte LBAs and namespace size describe a model, not a discovered SSD.
Changing geometry requires checked configuration; live hardware must supply
Identify/CAP-derived values and be rejected if unsupported.

The first capture profile consumes opaque, full-width beats and groups every
`CHUNK_BEATS` valid source beats into one synthetic chunk. Invalid source cycles
are gaps, not data. No header, timestamp, frame boundary, padding or filesystem
format is invented. Physical camera framing and partial `TKEEP` remain W03/W12
extensions; they must not silently be treated as this synthetic profile.

## W03: `capture_stager`

One synchronous clock; synchronous active-high `rst`. Parameters:
`DATA_WIDTH=256` (positive multiple of 8), `CHUNK_BEATS=128` (positive),
`BANKS=4` (at least 2), `COUNTER_WIDTH=64`, `SEQUENCE_WIDTH=64`
(both at least 2). Sequence width is reduced only in boundary tests.

| Port | Direction | Meaning |
|---|---|---|
| `clk`, `rst` | in | All port handshakes occur on rising edges |
| `arm` | in | Capture interval; assert only after downstream provisioning |
| `s_valid`, `s_data[DATA_WIDTH-1:0]` | in | Full opaque beat; cannot be stalled |
| `s_ready` | out | Always 1 outside reset; no storage backpressure reaches source |
| `m_valid`, `m_ready`, `m_data`, `m_last` | out/in/out/out | Ordered AXI stream of complete admitted chunks |
| `m_sequence[SEQUENCE_WIDTH-1:0]` | out | Source chunk number; unchanged for every beat of its chunk |
| `input_bytes`, `output_bytes`, `dropped_bytes`, `unarmed_bytes` | out | `COUNTER_WIDTH`-bit saturating counters |
| `overflow_chunks`, `aborted_chunks` | out | Saturating counters of admission losses and partial disarms |
| `buffered_bytes[63:0]` | out | Exact current occupancy, including a held output beat; independent of diagnostic counter width |
| `overflow_sticky`, `counter_saturated`, `sequence_exhausted` | out | Sticky status, cleared only by `rst` |

At a chunk's first valid beat, reserve an entire free bank or discard that
entire chunk. Reconsider admission only after `CHUNK_BEATS` valid source beats.
Never publish a partial chunk, overwrite a bank awaiting output, or start
output before its final input beat is stored. Sequence advances for both
admitted and discarded chunks, so output exposes gaps. It never wraps: after
sequence `2^SEQUENCE_WIDTH-1`, subsequent armed input is dropped and exhaustion is sticky.

Output holds data/last/sequence/valid stable under stall. `m_last` accompanies
only the final beat. An always-ready output must sustain one beat per cycle
through queued chunk boundaries. A bank whose final output beat is accepted
may be reused at that same edge; the memory implementation must not depend on
undefined read-during-write behavior. Read the RAM synchronously so the staging
store has a usable FPGA memory inference path; do not build a combinational
whole-chunk mux.

Deasserting `arm` discards an unfinished admitted chunk and counts its stored
bytes as dropped; a rejected chunk's received bytes were already counted.
Completed banks still drain. Re-arming starts a fresh chunk boundary; sequence
does not rewind. Source beats while disarmed count only as `unarmed_bytes`.
This boundary is synthetic and is not camera frame re-synchronization.

Before saturation: `input_bytes = output_bytes + dropped_bytes + buffered_bytes`.
`input_bytes` counts only armed valid beats. Discarding previously buffered
partial data increases dropped bytes without double-counting input. Counter
saturation must not clear sticky status or affect ownership/data movement.
Reset invalidates staging output and clears counters; any downstream consumer
must also cancel its uncommitted partial transfer. This is not a DMA fence.

## W04: `buffer_ownership`

One synchronous clock/reset. Parameters: `SLOTS=8` (positive, non-power-of-two
values supported), `LEASE_WIDTH=64`, `EPOCH_WIDTH=32`, `COUNTER_WIDTH=32`
(each at least 2), `TIMEOUT_CYCLES=256` (at least 2). Slot port width is
`max(1, ceil(log2(SLOTS)))`. These are implementation parameters, not SSD limits.

States are `FREE=0`, `FILLING=1`, `READY=2`, `INFLIGHT=3`, `QUARANTINED=4`.
A handle is `(epoch, slot, lease)`. Leases start at 1, increase on every
allocation and are never reused within an epoch, even when a slot is reused.

| Ports | Meaning |
|---|---|
| `alloc_valid`, `alloc_ready`, `alloc_slot`, `alloc_lease`, `epoch` | Request / combinational grant; handle is sampled only on handshake |
| `commit_valid`, `commit_ready`, `commit_slot`, `commit_lease`, `commit_epoch` | FILLING to READY; writer attests payload visibility |
| `submit_valid`, `submit_ready`, `submit_slot`, `submit_lease`, `submit_epoch` | READY to INFLIGHT; hardware scheduler must handshake before issuing |
| `complete_valid`, `complete_ready`, `complete_slot`, `complete_lease`, `complete_epoch`, `complete_success` | A successful matching INFLIGHT completion frees the slot; ready is always 1 to drain late events |
| `abort_req` | Link loss, memory failure or explicit fault; initiates quarantine |
| `recover_valid`, `recover_ready`, `fence_done` | Recover handshake requires faulted state and independently proven DMA quiescence |
| `faulted`, `error_sticky`, `epoch_exhausted`, `lease_exhausted` | Operational stop and sticky diagnosis |
| `stale_events`, `invalid_events`, `timeout_events`, `fault_events` | Saturating diagnostic counters |
| `slot_states[3*SLOTS-1:0]` | Slot i state at `[3*i +: 3]` |

`faulted` clears on recovery. The other three status flags are sticky history;
in particular, a previous lease exhaustion must not block allocation in a fenced
new epoch. Diagnostic event counters count cycles with one or more events in
that class; `fault_events` counts healthy-to-faulted transitions. Stale events
set `error_sticky` but do not fault healthy current work.

Allocation may choose any FREE slot; consumers must use the granted handle,
not assume an allocation algorithm. Commit/submit/complete events for distinct
slots may occur together. State checks use the state before the edge; no
same-slot commit+submit or submit+complete bypass is supported. Such a conflict
with current handles is a protocol fault. Invalid slot indices are faults and
must never index an array unchecked. Stale handles are consumed/ignored and
counted without changing current ownership. An exact duplicate completion for
an already FREE lease is stale, not permission to free another slot.

While healthy, malformed current commit/submit requests initiate a fault even
though they do not handshake. On any fault edge no allocation, commit or submit
is accepted; all non-FREE slots become QUARANTINED, including unsent data.
Completion errors initiate the same global quarantine. Completions cannot free
quarantine; previously successful/free slots remain FREE. Fault state blocks
new work until recovery. Stale events received while faulted cannot re-enable
allocation or alter quarantine.

The watchdog starts at zero on submit. If no matching completion is accepted
in the next `TIMEOUT_CYCLES` edges, fault once. A matching completion on the
deadline wins over the timeout for that slot; other slots may still time out.
External abort or a protocol fault takes priority over ordinary transitions.

`fence_done` is an attestation from the transport/memory coordinator that old
PCIe accesses, AXI requests and responses cannot reach reused storage. It is
not a software timeout or an NVMe CQE, and must never be tied high in hardware.
Only a recover handshake clears slot ownership, increments epoch and restarts
lease numbering. Error/counter history persists. Epoch never wraps: if already
at its maximum, recovery is refused and exhaustion is sticky. When the next
lease would equal the maximum representable value, refuse that allocation and
fault; a fenced new epoch is required. Small test widths exercise exhaustion.

`rst` is a cold/proven-quiescent reset of this whole ownership domain. It is
unsafe to reset this block alone while an SSD or AXI master can still DMA.
Runtime faults must use abort/fenced recovery, preserving the epoch counter.

The full host handle is **not** an NVMe wire CID. W05/W06 must validate CQ
phase/position, SQID/CID lookup and transport epochs before producing a host
completion. A stale raw CQE must not be relabelled with a new lease. W04 tests
prove the full-handle interface only, not that missing transport adapter.

## Memory visibility and read safety

A future PL DDR writer may commit a slot only after every AXI write response
for its payload has retired successfully and the controller's ordering contract
makes those bytes visible to the independent SSD DMA reader. An accepted W beat
is insufficient. AXI errors abort/quarantine the transfer, never publish it.
Read completion likewise needs both successful NVMe status and payload write
visibility before AXI-stream publication. Different host-memory responders or
AXI IDs require an explicit cross-path visibility barrier; a CQE alone is not it.

Timeout means unknown write outcome. Never automatically retry or claim success.
Recording gaps require caller-owned metadata; no on-SSD recovery format is
chosen here. Reset/recovery does not make cached SSD writes durable.

## Stream descriptors and control semantics

The future descriptor channel is ready/valid, stable while stalled, with fields:
`op[1:0]` (read=0, write=1, flush=2), `nsid[31:0]`, `slba[63:0]`,
`nlb[31:0]` (positive block count), `payload_bytes[63:0]`, `tag[63:0]`,
`epoch[31:0]`, `flags[7:0]` (bit 0 requests FUA; other bits zero).
Read/write byte count must equal `nlb * lba_bytes` without overflow. Flush has
zero nlb/payload and requires capability-checked adapter support. This logical
channel does not define a packed memory descriptor or an on-disk record.

Control is AXI-Lite with independent AW/W, strobes, responses and backpressure.
W09 uses the following 32-bit register offsets; none is implemented by W02:

| Offset | Access | Register |
|---|---|---|
| 0x000 | RO | ABI_VERSION = 0x00010000 |
| 0x004 | WO/W1P | ARM bit0, STOP bit1, ABORT bit2, RECOVER bit3, SNAPSHOT bit4 |
| 0x008 | RO | STATUS: armed bit0, faulted bit1, recover-ready bit2, config-valid bit3 |
| 0x00c | RO | Sticky error categories; cleared only by a separately fenced cold reset in v1 |
| 0x010 | RO | Current epoch |
| 0x014 | RO | Saturating snapshot generation |
| 0x040..0x06c | RW staged | NSID, NSZE lo/hi, LBA bytes, max command bytes, region SLBA lo/hi, region NLB lo/hi, chunk bytes, timeout cycles, flags (in that order) |
| 0x100..0x13c | RO snapshot | Armed input, staging output, dropped, unarmed, NVMe-completed, Flush-durable, staging occupancy, DDR occupancy (64-bit lo/hi pairs) |

Reserved bits read zero and writes with reserved bits set fail without changing
active state. ARM atomically validates/commits staged configuration only when
stopped, no ownership remains, and downstream is ready; otherwise fails. ARM
and STOP in one write is invalid. A snapshot freezes all counter pairs together;
reading a high/low pair must not sample independently changing counters. Ordinary
NVMe completion and confirmed Flush/FUA durability are separate observations.
The first recording profile rejects readback while armed; concurrent reads need
a separately qualified memory/storage arbitration budget.

## Independent namespace oracle

`tests/models/storage_oracle.py` is a sparse, byte-exact logical storage oracle.
Geometry, host command limit and optional controller byte limit are explicit.
Requests are validated completely before mutation. Unwritten bytes are zero
only by definition of the synthetic initial model, not by an SSD guarantee.
Writes update volatile cache, FUA makes that write durable, Flush persists prior
cached writes, and simulated power loss removes volatile-only changes. Model
write atomicity is a test convention, not a power-failure guarantee for drives.
No DUT queue, PRP, CID, TLP encoding or pattern function may be imported.

## Reproduce the contract/oracle gate

```sh
python3 -m venv .venv-portable
.venv-portable/bin/python -m pip install -r requirements-portable.lock
make model-test PYTHON=.venv-portable/bin/python
```

`make check` also includes the oracle alongside the existing tests and canary.
It needs Icarus; the oracle-only command does not. RTL package runners are
documented with W03/W04 and must retain seeds, JUnit, waveforms and source hashes.

## Deferred gates

These contracts do not implement external DDR, AXI error handling, CDC,
TLAST/TKEEP camera records, NVMe adaptation, filesystem/commit metadata or board
timing. W07/W08 must connect and verify the actual DDR and NVMe paths, with
bounded service stalls and independent arbitrary-payload readback. Every lossless
claim remains conditional on available storage and a measured service envelope.
