# ZCU102 feasibility record

Owner equipment: ZCU102 + Opsero FPGA Drive FMC Gen4 + Micron 2100AT 1 TB M.2.
Board revisions, exact SSD part/firmware and chosen FMC slot remain open.

The owner also has ZU7EG devkits. Those are now the preferred direct-SSD path;
see [the ZU7EG qualification record](../zu7eg/README.md). The ZCU102 discrepancy
does not block portable development or require a new board purchase now.

**Do not generate a `pcie4_uscale_plus` ZU9EG build assuming a PL PCIe hard block.**
AMD's [selection guide, EG table](https://docs.amd.com/api/khub/documents/2rKzwvn3YbmuIyp_BKxBGA/content)
lists ZU9EG without one. Opsero's [compatibility table and note 3](https://docs.opsero.com/op063/datasheet/compatibility/)
lists both ZCU102 FMC slots with that same limitation. Electrical routing of GTH
lanes does not establish PCIe controller availability. The user states that such
hard IP exists; reconcile this against the exact working design before closing
the hardware decision. Needed evidence: actual Vivado part, XCI/IP VLNV and
configuration, constraints, and a synthesized hard-cell/site inventory.

[UG1182](https://docs.amd.com/v/u/en-US/ug1182-zcu102-eval-bd) distinguishes the
PS-GTR Gen2 root-port slot from FMC-connected PL-GTH lanes, and identifies a
separate 16-bit PL DDR4 component. That DDR can support buffer development
independently of the PCIe target, subject to measured controller bandwidth.

Options:

1. Continue all portable simulation and use ZCU102 for generator/DDR capture tests.
2. Qualify another available UltraScale+ board with PL PCIe and routable x4 lanes
   for direct SSD tests. Confirm exact part/slot before selecting it.
3. Evaluate a separate soft PCIe root complex only as an explicitly scoped project.
4. A PS-PCIe backend is a separate relaxed-goal experiment, not success against
   the strict PL-only requirement.

The FMC card's Gen4 label does not force a Gen4 link. MVP link target is Gen3 x4.
Opsero documents 100 MHz reference clocks and active-high FMC `PERST_A/B` control
signals; SSD PERST# is active low. Verify board-level inversion and schematic
revision rather than wiring a signal based on its name. Validate VADJ, power
budget, lane order/polarity, refclock routing, quad and PCIe-site reachability.

The 20-lane camera and an x4 SSD would consume 24 transceiver channels before
other functions; package availability, quad PLL sharing and board routing must
be checked on the production target. Initial testing uses a PL generator, so
it does not assert that all 20 camera channels can be connected to this devkit.
