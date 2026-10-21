# DDR integration gates after portable capture/ownership

This is a source-grounded integration plan, not a completed DDR backend or a
board design. Source baseline: LiteNVMe
`bea70312f78e1e6f7d5ddec0b89bc831c3aea491`.

## Existing hooks

- `litenvme/core.py` accepts independent `hostmem_backend` and
  `streamer_backend` objects. The stream window has its own PCIe host-memory
  responder; PRP buffers use `streamer_base + slot * slot_bytes`.
- `LiteNVMeHostMemResponder` subtracts its PCIe window base before issuing local
  AXI addresses. The backend object supplies an AXI slave with matching data
  width, a 32-bit address and one-bit ID. External DDR translation belongs to a
  surrounding SoC/interconnect; do not confuse a PCIe PRP address with a DDR
  controller address.
- The library exposes engine request/completion streams when no built-in
  frontend owns them. Request fields are `op, nsid, lba, nlb, buf, tag`;
  completions carry `cid, status, sqhd, tag`. The standalone generator does not
  currently export a custom capture descriptor channel or DDR AXI ports.
- The stream responder arbitrates the SSD DMA path and `streamer.dma.axi`.
  Unlike the main host-memory path, it has no extra capture-master hook. Add a
  reviewed shared-memory wrapper/interconnect or an explicit master hook; do
  not wire two AXI masters directly together.
- The existing streamer waits for its AXI write responses before command
  submission but can backpressure its source. Its current DMA emits single-beat
  AXI transfers. Burst-capable capture is a separate integration deliverable.

The full responder supports 64/128/256-bit paths at this revision. A 512-bit
standalone stream-DMA simulation is not qualification of a 512-bit whole core;
the generator and responder currently disagree about that width.

## Ordered acceptance steps

1. **Shared DDR address/visibility boundary.** Connect a portable capture writer
   and the responder to one independent byte-addressed AXI memory model. Define
   the PCIe-to-local translation and aperture bounds. Exercise separated AW/W,
   delayed B/R, bursts and 4 KiB boundaries. Verify bytes through the independent
   SSD DMA reader, not a private writer-side shadow buffer. Any AXI error must
   prevent commit. No NVMe submission before every payload B response and the
   memory-controller visibility guarantee.
2. **Sealed descriptors.** Connect full chunks to the engine rather than asking
   software to submit each chunk. Assign LBAs only under a defined capture/gap
   policy; no on-disk metadata format is selected by the synthetic profile.
   Retain the full ownership handle behind the engine tag/CID mapping. Reject
   unsupported geometry/MDTS and out-of-aperture addresses before submission.
3. **End-to-end independent SSD model.** Use the existing raw-TLP SSD model to
   fetch actual submission entries and payload from the shared memory. Check
   arbitrary byte-exact writes/readback, repeated ring wrap and reordered CQEs.
   Do not replace this with a model that accepts high-level descriptors alone.
4. **Recovery coordinator.** On timeout, link loss, memory error or failed I/O,
   stop submissions, quarantine ownership and keep source loss explicit. Model
   outstanding reads/writes/responses and PCIe traffic after the fault. Establish
   and test the evidence for `fence_done` before recycling any address. Merely
   disabling the engine, resetting its bookkeeping, expiring a software timer
   or ignoring an old epoch is insufficient: old DMA can still corrupt new data.
5. **Stager/DDR-writer resynchronization.** A failed writer may have consumed a
   prefix of a staged chunk. Cancel that uncommitted destination, drain/discard
   the old chunk through its boundary (or use a coordinated explicit cancel),
   and count the gap. Never mistake its remaining beats for the next chunk.
   Preserve diagnostic history across runtime recovery.
6. **Service envelope.** Model bounded SSD outages and DDR arbitration/refresh
   stalls at the requested ingest rate. Account for capture writes plus SSD
   fetch reads, queues and reserves. Assert loss-free operation only inside the
   tested envelope; outside it require precise loss/overflow reporting and no
   ownership corruption. Readback stays disabled during the first recording
   profile.

Successful NVMe read completion must additionally wait for payload visibility
across the separate queue and payload responders. DDR writes and a CQE can have
different internal paths; do not infer a cross-path barrier from CQE arrival.

The portable controller's cold reset is not an operational recovery mechanism.
Epoch retention and the memory/PCIe quiescence coordinator must remain coherent
across a link reset. CDC and the final vendor shell remain later gates.

## Evidence and limits

Each step needs an owned workpack, independent assertions, failing-case evidence,
source/seed/tool manifests and retained waveforms. Generic Yosys structure checks
can detect latches or loss of intended RAM inference; they cannot certify AMD
timing. None of these simulation steps requires guessing a board pin or asking
for hardware access. The actual SSD, DDR controller and implementation still
need later qualification.
