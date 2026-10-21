# W08-A: shared PL-DDR write/visibility boundary

This bounded workpack brings the memory foundation forward from W08 before the
W07 scheduler. It depends on merged W02, W03-A and W04 contracts plus the pinned
W01 host-memory responder. It does not complete W07/W08, instantiate a DDR
controller, or claim a CPU-free end-to-end recorder or AMD timing.

## Ownership and acceptance

- Main: this contract, `scripts/sim_ddr.py`, `.github/workflows/ddr.yml`,
  `tests/memory/ddr_tb.sv`, README/qualification evidence, `docs/hardware/PZ17_TARGET.md` and integration review.
- Writer implementer: `rtl/memory/capture_ddr_writer.sv` only.
- Model/test implementer: `tests/memory/axi_ram.py`,
  `tests/memory/test_capture_ddr.py` only; no DUT-private-state imports.
- Responder implementer: `gateware/hostmem_ddr_fixture.py` only; export the
  pinned real responder and existing LiteX AXI arbitration, never hand-edit RTL.

All work shares one branch/PR. Preserve others' edits. Local RTL jobs run one at
a time. Full upstream regression stays in CI. Stop on an interface contradiction,
unsupported responder feature, dirty/mismatched upstream checkout, or a need to
invent vendor memory ordering/fencing guarantees.

Acceptance: arbitrary bytes written by the portable writer must be read back
through the actual LiteNVMe host-memory responder from the same independent AXI
RAM. AW/W backpressure and B latency/errors, 4 KiB burst splits, stream framing,
range checks, abort/drain and held commit all need directed independent checks.
No commit before all successful B responses and `visibility_ready`. No private
writer-side shadow memory can stand in for that readback path.

## Writer interface

Single synchronous clock, active-high synchronous cold/quiescent-only `rst`.
Parameters: `DATA_WIDTH=256` (64/128/256), `ADDR_WIDTH=32` (fixed in v1),
`CHUNK_BEATS=128` (positive), `SLOTS=8` (positive incl non-power-of-two),
`SLOT_STRIDE_BYTES=4096` (positive, beat aligned, at least chunk bytes),
`REGION_BASE=0` (32-bit beat-aligned), `APERTURE_BYTES=65536` (positive),
`MAX_BURST_BEATS=16` (1..16), `LEASE_WIDTH=64`, `EPOCH_WIDTH=32`,
`SEQUENCE_WIDTH=64` (each at least 2). Slot width is max(1,clog2(SLOTS)).
Use wide arithmetic for all address/range checks. The final byte of every slot
must be below APERTURE_BYTES and within the 32-bit address space; reject invalid
parameter configurations at time zero. APERTURE_BYTES is an exclusive local
address limit, not a span added to REGION_BASE.

Job input: `job_valid`, `job_ready`, `job_slot`, `job_lease`, `job_epoch`,
`job_sequence`. Accept one chunk job at a time, only healthy and idle. Derive
local destination `REGION_BASE + job_slot * SLOT_STRIDE_BYTES`; no arbitrary
address input. A valid request with an out-of-range slot or zero lease faults
without handshaking or issuing AXI. Other metadata is opaque and copied exactly.

Payload input: `s_valid`, `s_ready`, `s_data[DATA_WIDTH-1:0]`, `s_last`,
`s_sequence[SEQUENCE_WIDTH-1:0]`. Consume only after a job. Full-width beats only;
exactly CHUNK_BEATS per chunk. Sequence must match the job on every beat; TLAST
must be true exactly on the final beat. Early/late TLAST or mismatched sequence
faults; no commit. Invalid cycles are gaps. This is the backpressurable output of
W03-A, never the physical camera interface.

AXI master write ports: `axi_aw_valid/ready`, `axi_aw_addr[31:0]`,
`axi_aw_len[7:0]`, `axi_aw_size[2:0]`, `axi_aw_burst[1:0]`, `axi_aw_id[0:0]`;
`axi_w_valid/ready`, `axi_w_data`, `axi_w_strb[DATA_WIDTH/8-1:0]`, `axi_w_last`;
`axi_b_valid/ready`, `axi_b_resp[1:0]`, `axi_b_id[0:0]`.
Only INCR, aligned full beats, ID zero, all strobes enabled. Split at
MAX_BURST_BEATS, the end of the chunk, and 4 KiB boundaries. AW and W progress
independently, stable under stall; B is accepted only after AW and all W beats
retire. One burst outstanding. Buffer a complete burst before advertising AW/W,
so abort can finish already-advertised AXI traffic without depending on new
source data. The bounded burst buffer may use registers/small muxes; the large
W03 payload staging RAM remains synchronous. No new burst after a fault.

Output commit: `commit_valid`, `commit_ready`, `commit_slot`, `commit_lease`,
`commit_epoch`, `commit_sequence`. Assert only after the final successful B and
`visibility_ready=1`; hold handle/valid while stalled. `visibility_ready` is an
external memory-ordering attestation, not inferred from W acceptance or CQE.
The platform must establish it across writer/responder paths. A deasserted
attestation delays publication even after all Bs. Once publication is pending,
valid stays asserted until handshake unless explicit abort cancels it.

Control: `abort_req`, `faulted`, `error_code[3:0]`, `axi_quiescent`,
`stream_drained`, `recover_valid`, `recover_ready`, `fence_done`.
Error codes: 0 none, 1 bad job, 2 framing/sequence, 3 AXI B response or BID,
4 explicit abort. First error wins until recovery. Abort takes priority over
new job/payload/commit acceptance; it cancels any unpublished commit, but never
withdraws an already asserted stalled AW/W. Finish that advertised burst, accept
its B, and discard the remaining source chunk through TLAST. A burst collected
but not yet advertised can be discarded. On B error, no later burst is issued.
No source residue can become a new job's prefix. In fault state source ready may
consume/discard through the boundary, independently of draining issued AXI.

`axi_quiescent` means no AW/W/B obligation for this writer; it does not establish
transport-wide quiescence. `stream_drained` means no active source chunk remains.
Recovery requires faulted state, both outputs true, `abort_req=0`, and
independently attested `fence_done`. Its handshake clears the local fault and returns idle; no job is
accepted on that recovery edge. The owner retains epoch/history outside this
block and must supply a new full handle. Never tie fence_done high in hardware.
Cold rst cancels state only after the whole memory/transport domain is quiescent.

The writer does not own slots, synthesize NVMe commands, assign LBAs, retry unknown
writes, prove a transport fence, or directly count camera loss. The coordinator
must quarantine W04 ownership on writer fault and account the discarded chunk.

## Shared responder fixture and evidence

Export the actual pinned LiteNVMeHostMemResponder using its backend AXI master.
Arbitrate its read/write path and the capture writer into ONE slave AXI port
using the existing LiteX interconnect; no directly joined master signals.
PCIe addresses subtract the configured responder window base; local offsets are
used by both masters. The independent RAM checks all burst addresses and byte
strobes and exposes one byte dictionary, with no access to DUT internals.

Test data is arbitrary seeded bytes. Issue legal responder requests for different
offsets/order after commit and compare completion payloads with source bytes.
Exercise writes and unrelated DMA reads concurrently, delayed independent AW/W,
delayed B/R, and propagated responder memory errors. The first gate models a
controller whose successful B makes bytes visible, then independently withholds
visibility_ready to verify the extra publication guard. No actual controller's
ordering guarantee is assumed from this synthetic convention.

The runner must bind source/dependency commits, generated RTL hash, seed, tool
versions and exact expected executed test names. Fail on missing tools/results,
failed or skipped cases, changed sources, missing fresh FST/logs or wrong pins.
CI additionally checks portable writer synthesis for latches/undriven wires.
No hardware access or destructive SSD writes are required for W08-A.

## Current responder limitations

This gate uses fully enabled DWORD requests wholly inside the aperture. The real
responder checks the starting address only and ignores request first_be/last_be;
full-span validation and partial-DWORD PCIe byte enables remain a separate W05
transport gate. The fixture supplies zero routing metadata and derives unused
completion byte enables for deterministic output pins. None of those fixture
conventions certifies a complete hardware PCIe transport.

AXI read errors require a header-only error completion after the entire memory
read is checked; a late memory error cannot be added to a successful header
already sent. This generated-RTL fixture checks the abstract completion stream.
Separate tests in the pinned LiteNVMe fork, `test/test_hostmem_ddr_errors.py`,
check the fix through the real LitePCIe packetizer. Those tests run in the fork's
focused workflow and in the project's complete development regression; the DDR
fixture itself does not include a packetizer or physical PCIe interface.
