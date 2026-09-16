# Preferred prototype: owner-provided ZU7EG devkit

Confirmed: the owner has ZU7EG-based development kits, FPGA Drive FMC Gen4, and
Micron 2100AT 1 TB M.2 SSDs. Exact devkit identity, package, speed grade, revision
and FMC connectivity are not yet known.

AMD's [EG device selection table](https://docs.amd.com/api/khub/documents/2rKzwvn3YbmuIyp_BKxBGA/content)
lists ZU7EG PL PCI Express resources. This removes the ZU9EG hard-block obstacle,
but does not prove any particular devkit routes the required quad/refclock to FMC.

Before W10 freeze:

1. Record exact board schematic/revision and Vivado FPGA part.
2. Map one M.2 slot's four lanes to GTH quad and reachable PCIe site.
3. Check lane order/polarity, 100 MHz clock route, reset inversion, VADJ/power.
4. Identify PL DDR capacity/width/rate and run a memory bandwidth/correctness test.
5. Generate the AMD integrated PCIe block in Root Port mode for Gen3 x4; retain
   Tcl and IP settings, not guessed XDC or generated encrypted IP in Git.
6. Reconcile resources for camera processing, capture memory and SSD integration.

Do not substitute ZCU106 pin assignments: its FPGA is ZU7EV, and the owner's
ZU7EG board has not been identified. Until qualification, all simulation models
remain board-independent. No bitstream or device timing claim exists yet.
