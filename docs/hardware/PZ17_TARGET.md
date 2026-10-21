# PZ17 hardware target record

Owner confirmed on 2026-10-21:

- First target: custom PZ17 SoM.
- FPGA part as supplied: **XCZU17EG-1FFVC1760E**.
- A Linux workstation with Vivado and Vitis installed is available.

This records the selected target; it is not a qualified vendor shell or a claim
that the existing local pz17 prototype constraints match this board revision.

Before board implementation, inspect the actual schematics, board revision,
Vivado part selection, PCIe/GT sites, lane order, reference clocks, resets,
PL-DDR memory/controller configuration and timing constraints. Exact tool versions
and remote/JTAG access can be collected when simulation reaches the hardware gate.
No pin mapping, clock frequency, DDR capacity or PCIe lane routing is inferred here.

Current W08-A uses a synthetic single-clock AXI RAM. It does not program this SoM
or write a physical SSD. Hardware writes still require a selected drive identity
and an explicitly disposable LBA range.
