# Verification and acceptance

The initial repository has a tested tool harness and upstream evaluation runner.
Everything below that involves an NVMe end-to-end design is a future acceptance
requirement, not a test already implemented.

| Layer | Required evidence |
|---|---|
| AXIS | Random bubbles/stalls; stable data and sidebands; exact byte order; final partial beat; early/late TLAST; reset mid-record |
| Buffer | Full/empty/wrap, epoch fencing, no double allocation/free, no reuse while DMA outstanding, occupancy accounting |
| AXI memory | Independent AW/W, delayed/reordered legal responses, bursts/boundaries, strobes, errors; DDR latency and refresh model |
| PCIe | Config/MMIO, split CplD, byte count/lower address, tags, payload/boundary rules, credit stalls, UR/CA/error/timeout; address isolation |
| NVMe | CAP/Identify variants, 512/4096 LBA, unsupported metadata, PRPs, SQ/CQ phase wrap, out-of-order CID completion, SQHD, queue full, SCT/SC/DNR |
| Recovery | Link-down while SSD fetches data, missing/duplicate/stale completions, reset during writes, no false success, bounded reinit |
| Capture | Non-pausable source at configured maximum + burst profile; memory stalls and SSD outage envelope; no silent loss |
| Durability | Distinguish write completion/Flush/FUA; retention and power-cycle tests under documented device behavior |
| Timing | All clocks/I/O constrained, CDC/RDC reviewed, no unjustified false paths, setup/hold closure, DRC and utilization on actual part |

## Independent SSD model

Use a Python byte-addressed namespace separate from RTL. It must read SQEs from
the simulated FPGA host memory, decode opcodes and PRPs independently, fetch
write data in varied legal order/size, commit it to modeled storage, DMA read
data back, and post CQEs. Inject response latency, MPS/MRRS splitting, errors and
completion reorder. Do not simply return success on each doorbell or preload
the expected read buffer: that skips the NVMe data path.

Use cocotbext-pcie for transport components where appropriate; it is not itself
the NVMe oracle. Qualify any root-port wrapper for its actual RQ/RC/CQ/CC or raw
TLP semantics. Simulated link models cannot prove GT signal integrity/equalization.

## Performance acceptance

Record accepted input bytes, stored payload bytes, elapsed cycles, queue occupancy,
memory service, PCIe utilization and completion latency. Count metadata/padding
separately. Simulated service must be a configurable constraint rather than an
infinitely fast RAM that conceals stalls.

Provisional design scenario: 10 Gbit/s input, 100 ms zero service, 256 MiB payload
buffer, 1.75 GB/s service after recovery. This is not a Micron guarantee. Validate
repeated stalls and backlog recovery; explicitly fail an insufficient buffer.
Later target at least 1.5 GB/s sustained payload on hardware if the exact SSD can
support it, giving 20% rate margin over 1.25 GB/s. If it cannot, revise media or
drive count instead of lowering the input requirement.

For hardware record exact board/part, Vivado version, constraints hash, bitstream
hash, SSD model/serial/firmware, negotiated PCIe rate/width, MPS/MRRS, LBA size,
transfer size, QD, drive preconditioning/fill, temperature and test duration.
Use a scratch namespace/range. Verify patterns containing sequence number, LBA,
offset and checksum after readback; counters alone are not integrity evidence.

Finite capacity means a long capture must stop or explicitly overwrite old
records. Report usable recording duration. For writes beyond SSD cache capacity,
measure steady-state throughput and tail stalls, not just peak bursts.
