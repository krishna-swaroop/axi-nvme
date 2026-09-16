# Timing analysis boundary

No active XDC is supplied yet: exact ZU7EG board, part/package/speed grade, FMC
slot and clock wiring are unconfirmed. Do not invent pin or PCIe-site constraints.

The 256-bit/125 MHz datapath is a planning point (4 GB/s raw), not achieved Fmax.
On macOS we can measure cycles/throughput, lint, prove portable properties and
inspect structural synthesis. Yosys/OpenSTA with generic/ASIC cells is not
UltraScale+ setup/hold or routing signoff. No timing analysis result is claimed
by the bootstrap simulation fixture.

Once Linux/Vivado is available, W10 must generate reproducible Tcl for IP creation,
out-of-context synthesis and full implementation. Reports must include:

- primary/generated clocks and I/O delays; clocks missing from analysis;
- setup/hold WNS/TNS and failing endpoints after routing;
- `check_timing`, clock interactions, CDC and methodology checks;
- DRC, unconstrained paths, utilization, clock/GT/PCIe sites;
- reviewed asynchronous exceptions and synchronizer constraints;
- actual tool version, part, source SHA and constraints checksum.

Require no unexplained unconstrained paths, no unwaived critical DRC/CDC findings,
and nonnegative setup/hold slack for required corners. Generate a machine-readable
pass/fail manifest. Do not accept a log line saying a command finished as timing
closure. Vendor black boxes must be resolved in the signoff build.
