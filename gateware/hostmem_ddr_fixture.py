#!/usr/bin/env python3
"""Generate a real LiteNVMe host-memory responder plus shared AXI boundary.

The generated fixture exports the pinned LitePCIe slave-user request and
completion streams, a write-only capture-master AXI boundary, and one shared
AXI memory boundary. It contains the real ``LiteNVMeHostMemResponder`` and its
LiteX ``AXIArbiter``; it does not instantiate a PCIe hard IP, a DDR controller,
or a memory model.

The pinned responder accepts only requests whose starting byte address is in
its configured window; it does not check the complete span of a multi-beat
request against that window. Its request layout carries ``first_be`` and
``last_be``, but this responder does not consume them. The fixture is therefore
a focused shared-memory integration boundary, not a complete PCIe transport
qualification. Completion ``tc``, ``attr``, ``channel`` and ``user_id`` are not
assigned by the pinned responder, so this fixture drives those metadata fields
to zero rather than leaving top-level inputs undriven. It derives the exposed
completion byte-enable mask from ``len`` and ``last``; the pinned LitePCIe
packetizer independently derives its TLP byte enables from ``len`` and does not
consume this endpoint ``be`` field.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MODULE_NAME = "hostmem_ddr_fixture"
DEFAULT_BASE = 0x10000000
DEFAULT_SIZE = 65536
ADDRESS_LIMIT = 1 << 32


def _parse_int(value: str) -> int:
    return int(value, 0)


def _validate_config(data_width: int, base: int, size: int) -> None:
    if data_width not in (64, 128, 256):
        raise ValueError("data width must be 64, 128, or 256 bits")
    beat_bytes = data_width // 8
    if base < 0 or base >= ADDRESS_LIMIT:
        raise ValueError("base must fit in the 32-bit PCIe byte-address space")
    if base % beat_bytes:
        raise ValueError(f"base must be aligned to the {beat_bytes}-byte AXI beat")
    if size <= 0:
        raise ValueError("size must be positive")
    if base + size > ADDRESS_LIMIT:
        raise ValueError("base plus size must fit in the 32-bit PCIe byte-address space")


def _build_fixture(data_width: int, base: int, size: int):
    """Build the Migen top and its explicitly named exported IO signals.

    Imports are intentionally local: the caller must establish the pinned
    LiteNVMe/LitePCIe/LiteX PYTHONPATH before generation.
    """
    from migen import Case, If, Module
    from migen.fhdl import verilog
    from types import SimpleNamespace

    from litex.soc.interconnect import axi
    from litenvme.hostmem import LiteNVMeHostMemResponder
    from litepcie.core.common import LitePCIeSlaveInternalPort, LitePCIeSlavePort

    class HostMemDDRFixture(Module):
        def __init__(self):
            self.internal_port = LitePCIeSlaveInternalPort(data_width=data_width)
            self.pcie_port = LitePCIeSlavePort(self.internal_port)

            # The writer is an AXI4 write master. LiteNVMeHostMemResponder's
            # existing arbiter selects it alongside the real PCIe DMA master.
            self.writer_axi = axi.AXIInterface(
                data_width=data_width,
                address_width=32,
                id_width=1,
                mode="w",
                name="wr_axi",
            )

            # This is the one shared AXI memory target. Both real responder DMA
            # and the external capture writer reach it through the responder's
            # built-in AXIArbiter.
            self.mem_axi = axi.AXIInterface(
                data_width=data_width,
                address_width=32,
                id_width=1,
                mode="rw",
                name="mem",
            )
            external_backend = SimpleNamespace(axi=self.mem_axi)

            self.submodules.responder = LiteNVMeHostMemResponder(
                port=self.pcie_port,
                base=base,
                size=size,
                data_width=data_width,
                with_csr=False,
                extra_axi_masters=[self.writer_axi],
                backend=external_backend,
            )

            # The pinned responder leaves these endpoint metadata fields
            # unassigned. This direct fixture has no routing context, so expose
            # defined zero values instead of creating floating top-level inputs.
            self.comb += [
                self.pcie_port.source.tc.eq(0),
                self.pcie_port.source.attr.eq(0),
                self.pcie_port.source.channel.eq(0),
                self.pcie_port.source.user_id.eq(0),
            ]
            beat_dwords = data_width // 32
            beat_bytes = data_width // 8
            full_be = (1 << beat_bytes) - 1
            completion_be_cases = {
                remainder: self.pcie_port.source.be.eq((1 << (4 * remainder)) - 1)
                for remainder in range(1, beat_dwords)
            }
            self.comb += self.pcie_port.source.be.eq(full_be)
            if completion_be_cases:
                self.comb += If(
                    self.pcie_port.source.last,
                    Case(
                        self.pcie_port.source.len[:beat_dwords.bit_length() - 1],
                        completion_be_cases,
                    ),
                )

            # Neither AXI master currently sets these optional AXI4 metadata
            # fields. Drive them to defined values before the arbiter forwards
            # them to the common memory interface.
            self._tie_optional_axi_controls(self.writer_axi)
            self._tie_optional_axi_controls(self.responder.dma.axi)

            # The capture writer boundary is write-only. Do not expose or issue
            # its unused AR channel, and never request R data on that master.
            self.comb += [
                self.writer_axi.ar.valid.eq(0),
                self.writer_axi.ar.addr.eq(0),
                self.writer_axi.ar.burst.eq(0),
                self.writer_axi.ar.len.eq(0),
                self.writer_axi.ar.size.eq(0),
                self.writer_axi.ar.id.eq(0),
                self.writer_axi.r.ready.eq(0),
            ]

        def _tie_optional_axi_controls(self, interface):
            for channel in ("aw", "ar"):
                record = getattr(interface, channel)
                for field in ("lock", "prot", "cache", "qos", "region"):
                    signal = getattr(record, field, None)
                    if signal is not None:
                        self.comb += signal.eq(0)

    top = HostMemDDRFixture()
    ios = []
    _add_endpoint_ios(ios, "req", top.pcie_port.sink)
    _add_endpoint_ios(ios, "cpl", top.pcie_port.source)
    _add_axi_ios(ios, "wr_axi", top.writer_axi, ("aw", "w", "b"), writer=True)
    _add_axi_ios(ios, "mem", top.mem_axi, ("aw", "w", "b", "ar", "r"))

    converted = verilog.convert(top, ios=set(ios), name=MODULE_NAME)
    return converted.main_source


def _add_endpoint_ios(ios, prefix: str, endpoint) -> None:
    for field in ("valid", "ready", "first", "last"):
        signal = getattr(endpoint, field)
        signal.name_override = f"{prefix}_{field}"
        ios.append(signal)
    for field, _width in endpoint.description.payload_layout + endpoint.description.param_layout:
        signal = getattr(endpoint, field)
        signal.name_override = f"{prefix}_{field}"
        ios.append(signal)


def _add_axi_ios(ios, prefix: str, interface, channels, writer: bool = False) -> None:
    if writer:
        fields = {
            "aw": ("valid", "ready", "addr", "len", "size", "burst", "id"),
            "w": ("valid", "ready", "data", "strb", "last"),
            "b": ("valid", "ready", "resp", "id"),
        }
    else:
        fields = {
            channel: ("valid", "ready")
            for channel in channels
        }
        fields.update({
            "aw": fields.get("aw", ()) + ("addr", "len", "size", "burst", "id"),
            "w": fields.get("w", ()) + ("data", "strb", "last"),
            "b": fields.get("b", ()) + ("resp", "id"),
            "ar": fields.get("ar", ()) + ("addr", "len", "size", "burst", "id"),
            "r": fields.get("r", ()) + ("data", "resp", "last", "id"),
        })

    for channel in channels:
        record = getattr(interface, channel)
        for field in fields[channel]:
            signal = getattr(record, field)
            signal.name_override = f"{prefix}_{channel}_{field}"
            ios.append(signal)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="generated Verilog output path")
    parser.add_argument("--data-width", required=True, type=int, choices=(64, 128, 256))
    parser.add_argument("--base", type=_parse_int, default=DEFAULT_BASE,
                        help="PCIe-visible responder base (default: 0x10000000)")
    parser.add_argument("--size", type=_parse_int, default=DEFAULT_SIZE,
                        help="responder window size in bytes (default: 65536)")
    args = parser.parse_args()

    try:
        _validate_config(args.data_width, args.base, args.size)
        source = _build_fixture(args.data_width, args.base, args.size)
    except (ImportError, ValueError, AssertionError) as error:
        parser.error(str(error))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "// Generated from gateware/hostmem_ddr_fixture.py. Do not edit by hand.\n"
        "// PCIe request addresses are translated by the real responder to local offsets.\n"
        "// first_be/last_be are exported but ignored by this pinned responder; only the\n"
        "// request start address is range-checked, not the full transfer span.\n"
        + source,
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
